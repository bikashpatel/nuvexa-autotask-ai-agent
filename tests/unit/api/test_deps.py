"""FastAPI dependencies: session resolution, CSRF, actor, settings/db/session, rate limiting."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi import Depends, FastAPI, Request
from sqlalchemy import text, update

from app.api.deps import (
    ActorDep,
    ResolvedSessionDep,
    SessionDep,
    SettingsDep,
    get_database,
    get_optional_resolved_session,
    rate_limit,
)
from app.core.config import Settings
from app.core.timeutil import utcnow
from app.db.models import Tenant, User, UserSession
from app.db.session import Database
from app.domain.enums import UserRole
from app.services.auth_service import ResolvedSession
from tests.conftest import UserFactory
from tests.unit.api.conftest import ClientFactory, issue_session

UNSAFE = ["POST", "PUT", "PATCH", "DELETE"]


@pytest.fixture
def deps_app(bare_app: FastAPI) -> FastAPI:
    async def _who(request: Request, resolved: ResolvedSessionDep) -> dict[str, Any]:
        return {"user": str(resolved.user.id), "state_user": request.state.user_id, "tenant": resolved.tenant.name}

    for method in ["GET", *UNSAFE]:
        bare_app.add_api_route("/who", _who, methods=[method], name=f"who-{method}")

    @bare_app.get("/actor")
    async def _actor(actor: ActorDep) -> dict[str, Any]:
        return {"user": str(actor.user_id), "role": actor.role.value, "ip": actor.ip_address}

    @bare_app.get("/optional")
    async def _optional(
        resolved: ResolvedSession | None = Depends(get_optional_resolved_session),
    ) -> dict[str, Any]:
        return {"user": str(resolved.user.id) if resolved else None}

    @bare_app.get("/plumbing")
    async def _plumbing(
        settings: SettingsDep, session: SessionDep, db: Database = Depends(get_database)
    ) -> dict[str, Any]:
        return {
            "settings_is_app_state": settings is bare_app.state.settings,
            "db_is_app_state": db is bare_app.state.db,
            "select_1": (await session.execute(text("SELECT 1"))).scalar_one(),
        }

    @bare_app.post("/limited", dependencies=[Depends(rate_limit("unit", 2, 60))])
    async def _limited() -> dict[str, bool]:
        return {"ok": True}

    return bare_app


@pytest.fixture
async def member(session: Any, tenant: Tenant, make_user: UserFactory) -> User:
    return await make_user(tenant, UserRole.MEMBER)


async def _login(client: Any, session: Any, settings: Settings, user: User) -> str:
    issued = await issue_session(session, settings, user)
    client.cookies.set(settings.session_cookie_name, issued.token)
    return issued.csrf_token


async def test_missing_session_is_401(deps_app: FastAPI, make_client: ClientFactory) -> None:
    client = make_client(deps_app)
    for method in ["GET", *UNSAFE]:
        resp = await client.request(method, "/who")
        assert resp.status_code == 401, method
        assert resp.json()["error"]["code"] == "authentication_required"


@pytest.mark.parametrize("token", ["not-a-real-token", "x" * 201, ""])
async def test_invalid_session_is_401(
    deps_app: FastAPI, make_client: ClientFactory, settings: Settings, token: str
) -> None:
    client = make_client(deps_app)
    client.cookies.set(settings.session_cookie_name, token)
    assert (await client.get("/who")).status_code == 401


async def test_revoked_and_expired_sessions_are_401(
    deps_app: FastAPI, make_client: ClientFactory, session: Any, settings: Settings, member: User, db: Database
) -> None:
    client = make_client(deps_app)
    await _login(client, session, settings, member)
    assert (await client.get("/who")).status_code == 200
    async with db.session() as s:
        await s.execute(update(UserSession).values(expires_at=utcnow() - timedelta(seconds=1)))
        await s.commit()
    assert (await client.get("/who")).status_code == 401


async def test_valid_session_get_needs_no_csrf(
    deps_app: FastAPI, make_client: ClientFactory, session: Any, settings: Settings, member: User, tenant: Tenant
) -> None:
    client = make_client(deps_app)
    await _login(client, session, settings, member)
    resp = await client.get("/who")
    assert resp.status_code == 200
    assert resp.json() == {"user": str(member.id), "state_user": str(member.id), "tenant": tenant.name}


@pytest.mark.parametrize("method", UNSAFE)
async def test_unsafe_methods_require_matching_csrf(
    deps_app: FastAPI, make_client: ClientFactory, session: Any, settings: Settings, member: User, method: str
) -> None:
    client = make_client(deps_app)
    csrf = await _login(client, session, settings, member)
    missing = await client.request(method, "/who")
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "csrf_failed"
    wrong = await client.request(method, "/who", headers={"x-csrf-token": csrf + "x"})
    assert wrong.status_code == 403
    empty = await client.request(method, "/who", headers={"x-csrf-token": ""})
    assert empty.status_code == 403
    ok = await client.request(method, "/who", headers={"x-csrf-token": csrf})
    assert ok.status_code == 200


async def test_csrf_token_of_another_session_is_rejected(
    deps_app: FastAPI, make_client: ClientFactory, session: Any, settings: Settings, member: User
) -> None:
    client = make_client(deps_app)
    await _login(client, session, settings, member)
    other = await issue_session(session, settings, member)
    resp = await client.post("/who", headers={"x-csrf-token": other.csrf_token})
    assert resp.status_code == 403


async def test_actor_carries_role_and_client_ip(
    deps_app: FastAPI, make_client: ClientFactory, session: Any, settings: Settings, member: User
) -> None:
    client = make_client(deps_app, client_ip="198.51.100.9")
    await _login(client, session, settings, member)
    assert (await client.get("/actor")).json() == {"user": str(member.id), "role": "member", "ip": "198.51.100.9"}
    assert (await make_client(deps_app).get("/actor")).status_code == 401


async def test_optional_session(
    deps_app: FastAPI, make_client: ClientFactory, session: Any, settings: Settings, member: User
) -> None:
    client = make_client(deps_app)
    assert (await client.get("/optional")).json() == {"user": None}
    await _login(client, session, settings, member)
    assert (await client.get("/optional")).json() == {"user": str(member.id)}


async def test_settings_database_and_session_dependencies(deps_app: FastAPI, make_client: ClientFactory) -> None:
    resp = await make_client(deps_app).get("/plumbing")
    assert resp.json() == {"settings_is_app_state": True, "db_is_app_state": True, "select_1": 1}


async def test_rate_limit_disabled_is_a_noop(deps_app: FastAPI, make_client: ClientFactory, settings: Settings) -> None:
    assert settings.rate_limit_enabled is False  # test default
    client = make_client(deps_app)
    for _ in range(5):
        assert (await client.post("/limited")).status_code == 200
    assert not deps_app.state.rate_limiter._hits


async def test_rate_limit_enabled_limits_each_anonymous_client(
    deps_app: FastAPI, make_client: ClientFactory, settings: Settings
) -> None:
    deps_app.state.settings = settings.model_copy(update={"rate_limit_enabled": True})
    first = make_client(deps_app, client_ip="192.0.2.1")
    assert [(await first.post("/limited")).status_code for _ in range(3)] == [200, 200, 429]
    limited = await first.post("/limited")
    assert limited.json()["error"]["code"] == "rate_limited"
    second = make_client(deps_app, client_ip="192.0.2.2")
    assert (await second.post("/limited")).status_code == 200  # a different client has its own budget
