"""FastAPI dependencies: database session, settings, authenticated actor, CSRF, rate limits."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.middleware import SlidingWindowRateLimiter, client_ip
from app.core.config import Settings
from app.core.errors import AuthenticationRequired, CsrfFailed, RateLimited
from app.db.session import Database
from app.services.auth_service import ResolvedSession, SessionService
from app.services.authz import Actor
from app.services.identity import actor_from_user

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

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
    """In-process sliding-window limit. Buckets are keyed by the *verified* user (valid session) or by client
    IP — never by an unverified cookie value, which a client could rotate to get fresh buckets. Sign-in
    routes use `per="ip"` so failed or repeated logins are throttled per client regardless of session."""

    async def _check(request: Request, settings: SettingsDep, session: SessionDep) -> None:
        if not settings.rate_limit_enabled:
            return
        limiter: SlidingWindowRateLimiter = request.app.state.rate_limiter
        identity = f"ip:{client_ip(request) or 'unknown'}"
        token = request.cookies.get(settings.session_cookie_name)
        if per == "user" and token:
            resolved = await SessionService(settings).resolve(session, token)
            if resolved is not None:
                identity = f"user:{resolved.user.id}"
        if not limiter.allow(f"{name}:{identity}", limit, window_seconds):
            raise RateLimited()

    return _check
