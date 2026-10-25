"""`rate_limit()` with both backends: 429 + Retry-After, bucket keys, the database-failure policy, session isolation."""

from __future__ import annotations

import secrets
from typing import Any, Literal

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import func, select, text

from app.api import deps, middleware
from app.api.deps import UNAVAILABLE_RETRY_AFTER_SECONDS, SessionDep, rate_limit
from app.api.middleware import SlidingWindowRateLimiter
from app.core.config import Settings
from app.db.models import RateLimitCounter, Tenant, User
from app.db.session import Database
from app.domain.enums import UserRole
from app.services.rate_limit import DatabaseRateLimiter
from tests.conftest import UserFactory
from tests.support.rate_limit import FrozenClock, RecordingLog
from tests.unit.api.conftest import ClientFactory, issue_session

Backend = Literal["memory", "database"]


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()  # 10 s into a window


@pytest.fixture
def limited_app(bare_app: FastAPI, settings: Settings) -> FastAPI:
    bare_app.state.settings = settings.model_copy(update={"rate_limit_enabled": True})

    @bare_app.post("/per-user", dependencies=[Depends(rate_limit("unit-user", 2, 60))])
    async def _per_user() -> dict[str, bool]:
        return {"ok": True}

    @bare_app.post("/sign-in", dependencies=[Depends(rate_limit("unit-sign-in", 2, 60, per="ip"))])
    async def _sign_in() -> dict[str, bool]:
        return {"ok": True}

    @bare_app.get("/request-session", dependencies=[Depends(rate_limit("unit-session", 5, 60))])
    async def _request_session(session: SessionDep) -> dict[str, bool]:
        return {"in_transaction": session.in_transaction()}

    return bare_app


def use_backend(app: FastAPI, db: Database, backend: Backend, clock: FrozenClock) -> None:
    app.state.rate_limiter = (
        DatabaseRateLimiter(db.engine, clock=clock) if backend == "database" else SlidingWindowRateLimiter()
    )


@pytest.fixture
async def member(session: Any, tenant: Tenant, make_user: UserFactory) -> User:
    return await make_user(tenant, UserRole.MEMBER)


async def signed_in(
    make_client: ClientFactory, app: FastAPI, session: Any, settings: Settings, user: User, ip: str
) -> httpx.AsyncClient:
    issued = await issue_session(session, settings, user)
    client = make_client(app, client_ip=ip)
    client.cookies.set(settings.session_cookie_name, issued.token)
    client.headers["X-CSRF-Token"] = issued.csrf_token
    return client


async def statuses(client: httpx.AsyncClient, path: str, n: int) -> list[int]:
    return [(await client.post(path)).status_code for _ in range(n)]


# --------------------------------------------------------------------------- 429 envelope + Retry-After


async def test_the_in_process_backend_answers_429_with_retry_after(
    limited_app: FastAPI, make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(middleware.time, "monotonic", lambda: now[0])
    client = make_client(limited_app)
    assert await statuses(client, "/sign-in", 2) == [200, 200]
    now[0] += 0.5

    limited = await client.post("/sign-in")

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert limited.headers["retry-after"] == "60"  # the first hit is more than 60 s old in 59.5 s → 60 whole seconds


async def test_the_database_backend_answers_429_with_retry_after(
    limited_app: FastAPI, db: Database, make_client: ClientFactory, clock: FrozenClock
) -> None:
    use_backend(limited_app, db, "database", clock)
    client = make_client(limited_app)
    assert await statuses(client, "/sign-in", 2) == [200, 200]

    limited = await client.post("/sign-in")

    assert limited.status_code == 429
    assert limited.json()["error"] == {
        "code": "rate_limited",
        "message": "Too many requests. Try again later.",
        "correlation_id": limited.headers["x-request-id"],
        "details": [],
    }
    assert limited.headers["retry-after"] == "51"
    assert "retry-after" not in (await make_client(limited_app, client_ip="192.0.2.99").post("/sign-in")).headers


# --------------------------------------------------------------------------- bucket keys


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_buckets_are_keyed_by_verified_user_or_client_ip(
    limited_app: FastAPI,
    db: Database,
    session: Any,
    settings: Settings,
    member: User,
    make_client: ClientFactory,
    clock: FrozenClock,
    backend: Backend,
) -> None:
    use_backend(limited_app, db, backend, clock)
    anonymous = make_client(limited_app, client_ip="192.0.2.1")
    assert await statuses(anonymous, "/per-user", 3) == [200, 200, 429]  # keyed ip:192.0.2.1

    # The same IP with a verified session has the user's own budget …
    at_office = await signed_in(make_client, limited_app, session, settings, member, "192.0.2.1")
    assert await statuses(at_office, "/per-user", 1) == [200]
    # … which follows the user to another IP and another session.
    at_home = await signed_in(make_client, limited_app, session, settings, member, "198.51.100.7")
    assert await statuses(at_home, "/per-user", 2) == [200, 429]

    # Sign-in routes count per IP whatever the session says.
    assert await statuses(at_office, "/sign-in", 2) == [200, 200]
    assert await statuses(anonymous, "/sign-in", 1) == [429]
    assert await statuses(at_home, "/sign-in", 1) == [200]


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_an_unverified_session_cookie_counts_against_the_client_ip(
    limited_app: FastAPI, db: Database, make_client: ClientFactory, clock: FrozenClock, backend: Backend
) -> None:
    use_backend(limited_app, db, backend, clock)
    observed = []
    for _ in range(3):
        client = make_client(limited_app, client_ip="192.0.2.5")
        client.cookies.set("nuvexa_session", secrets.token_urlsafe(32))
        observed.append((await client.post("/per-user")).status_code)
    assert observed == [200, 200, 429]


# --------------------------------------------------------------------------- database failures


async def test_sign_in_routes_fail_closed_and_authenticated_routes_fail_open_when_the_store_is_down(
    limited_app: FastAPI,
    db: Database,
    session: Any,
    settings: Settings,
    member: User,
    make_client: ClientFactory,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_backend(limited_app, db, "database", clock)
    log = RecordingLog()
    monkeypatch.setattr(deps, "log", log)
    user_client = await signed_in(make_client, limited_app, session, settings, member, "192.0.2.1")
    async with db.engine.begin() as conn:
        await conn.execute(text("DROP TABLE rate_limit_counters"))

    sign_in = await make_client(limited_app, client_ip="192.0.2.2").post("/sign-in")
    authenticated = await user_client.post("/per-user")

    # Sign-in (anonymous, brute-force target): no budget can be verified → refuse, and tell the client when to retry.
    assert sign_in.status_code == 429
    assert sign_in.json()["error"]["code"] == "rate_limited"
    assert sign_in.headers["retry-after"] == str(UNAVAILABLE_RETRY_AFTER_SECONDS)
    # Authenticated (verified user, RBAC + idempotency downstream): serve the request.
    assert authenticated.status_code == 200
    failures = log.named("rate_limit_backend_unavailable")
    assert [(e["limit_name"], e["per"], e["outcome"], e["log_level"]) for e in failures] == [
        ("unit-sign-in", "ip", "denied", "error"),
        ("unit-user", "user", "allowed", "error"),
    ]
    assert all(e["error_type"] in {"OperationalError", "ProgrammingError"} for e in failures)
    assert "192.0.2" not in repr(failures)  # neither the key nor the client IP is logged


# --------------------------------------------------------------------------- isolation from the request


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_the_limiter_never_uses_the_request_session(
    limited_app: FastAPI,
    db: Database,
    session: Any,
    settings: Settings,
    member: User,
    make_client: ClientFactory,
    clock: FrozenClock,
    backend: Backend,
) -> None:
    # The identity is resolved in a short session of its own: the request's session holds no connection and no
    # transaction while the limiter works (a held connection plus the limiter's own one could exhaust the pool).
    use_backend(limited_app, db, backend, clock)
    client = await signed_in(make_client, limited_app, session, settings, member, "192.0.2.1")

    resp = await client.get("/request-session")

    assert resp.json() == {"in_transaction": False}


async def test_disabled_rate_limiting_never_touches_the_store(
    limited_app: FastAPI, db: Database, settings: Settings, make_client: ClientFactory, clock: FrozenClock
) -> None:
    use_backend(limited_app, db, "database", clock)
    limited_app.state.settings = settings  # RATE_LIMIT_ENABLED=false (test default)

    assert await statuses(make_client(limited_app), "/sign-in", 5) == [200] * 5
    async with db.session() as s:
        assert (await s.execute(select(func.count()).select_from(RateLimitCounter))).scalar_one() == 0


@pytest.mark.parametrize(("limit", "window"), [(0, 60), (-1, 60), (1, 0), (1, -5)])
def test_rate_limit_rejects_limits_that_could_never_admit(limit: int, window: float) -> None:
    with pytest.raises(ValueError, match="rate limit"):
        rate_limit("x", limit, window)
