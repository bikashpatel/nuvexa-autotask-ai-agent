"""FastAPI dependencies: database session, settings, authenticated actor, CSRF, rate limits."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.middleware import client_ip
from app.core.config import Settings
from app.core.errors import AuthenticationRequired, CsrfFailed, RateLimited
from app.db.session import Database
from app.observability.logging import get_logger
from app.services.auth_service import ResolvedSession, SessionService
from app.services.authz import Actor
from app.services.identity import actor_from_user
from app.services.rate_limit import RateLimiter, RateLimiterUnavailable

log = get_logger(__name__)

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Retry-After for sign-in routes refused because the shared rate-limit store is unavailable (fail closed).
UNAVAILABLE_RETRY_AFTER_SECONDS = 5

# Pagination offset shared by every list endpoint. Bounded so that huge values are a 422, never a database
# driver overflow (offsets >= 2**63 do not fit a 64-bit integer) answered with a 500.
MAX_OFFSET = 1_000_000
Offset = Annotated[int, Query(ge=0, le=MAX_OFFSET)]


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_database(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


async def get_session(db: Annotated[Database, Depends(get_database)]) -> AsyncIterator[AsyncSession]:
    async with db.session() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DatabaseDep = Annotated[Database, Depends(get_database)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_resolved_session(request: Request, session: SessionDep, settings: SettingsDep) -> ResolvedSession:
    token = request.cookies.get(settings.session_cookie_name)
    resolved = await SessionService(settings).resolve(session, token)
    if resolved is None:
        raise AuthenticationRequired()
    if request.method in _UNSAFE_METHODS and not SessionService.csrf_valid(
        resolved, request.headers.get("x-csrf-token")
    ):
        raise CsrfFailed()
    request.state.user_id = str(resolved.user.id)
    return resolved


async def get_optional_resolved_session(
    request: Request, session: SessionDep, settings: SettingsDep
) -> ResolvedSession | None:
    return await SessionService(settings).resolve(session, request.cookies.get(settings.session_cookie_name))


ResolvedSessionDep = Annotated[ResolvedSession, Depends(get_resolved_session)]


async def get_actor(request: Request, resolved: ResolvedSessionDep) -> Actor:
    return actor_from_user(resolved.user, client_ip(request))


ActorDep = Annotated[Actor, Depends(get_actor)]


def rate_limit(
    name: str, limit: int, window_seconds: float = 60, *, per: Literal["user", "ip"] = "user"
) -> Callable[..., Coroutine[Any, Any, None]]:
    """Sliding-window limit, per API process or shared by every replica (RATE_LIMIT_BACKEND=database).

    Buckets are keyed by the *verified* user (valid session) or by client IP — never by an unverified cookie
    value, which a client could rotate to get fresh buckets. Sign-in routes use `per="ip"` so failed or repeated
    logins are throttled per client regardless of session. A refused request gets 429 with `Retry-After`.

    If the shared store fails (database error), sign-in routes (`per="ip"`) fail closed: their callers are
    anonymous, they are the brute-force target, and they need the database anyway. Authenticated routes fail open:
    the session was verified a moment earlier, RBAC, approvals and idempotency still apply, and a fault confined to
    the counters (a lock or statement timeout on a hot row, a missing table) must not stop approved work. Both
    outcomes are logged."""
    if limit < 1 or window_seconds <= 0:
        raise ValueError(f"rate limit {name!r} needs limit >= 1 and window_seconds > 0")

    async def _check(request: Request, settings: SettingsDep, db: DatabaseDep) -> None:
        if not settings.rate_limit_enabled:
            return
        limiter: RateLimiter = request.app.state.rate_limiter
        key = f"{name}:{await _limit_identity(request, settings, db, per)}"
        try:
            decision = await limiter.acquire(key, limit, window_seconds)
        except RateLimiterUnavailable as exc:
            fail_closed = per == "ip"
            log.error(
                "rate_limit_backend_unavailable",
                limit_name=name,
                per=per,
                outcome="denied" if fail_closed else "allowed",
                error_type=str(exc),
            )
            if fail_closed:
                raise RateLimited(retry_after=UNAVAILABLE_RETRY_AFTER_SECONDS) from exc
            return
        if not decision.allowed:
            raise RateLimited(retry_after=decision.retry_after)

    return _check


async def _limit_identity(request: Request, settings: Settings, db: Database, per: str) -> str:
    token = request.cookies.get(settings.session_cookie_name)
    if per == "user" and token:
        # A short session of its own, closed before the limiter runs: the request's session holds no connection or
        # transaction meanwhile (two pooled connections per request could exhaust the pool under load), and nothing
        # the limiter does can join the request's transaction.
        async with db.session() as session:
            resolved = await SessionService(settings).resolve(session, token)
        if resolved is not None:
            return ":".join(("user", str(resolved.user.id)))
    return ":".join(("ip", client_ip(request) or "unknown"))
