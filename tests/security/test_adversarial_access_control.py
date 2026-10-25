"""Adversarial access-control QA: authentication, authorization, sessions, OAuth and CORS.

An independent pass against CLAUDE.md invariants 5 (tenant isolation, 404 for invisible resources), 7 (secrets)
and 11 (verified identity), docs/security-model.md and docs/api.md. It builds on, and deliberately does not
repeat, tests/integration/{test_rbac_matrix,test_auth_flows,test_api_contract,test_security}.py and
tests/security/{test_cors,test_session_and_attribution,test_hardening_regressions}.py.

Routes are discovered from the application object (tests/security/_adversarial_access_helpers.py), so the
authentication, CSRF and permission sweeps cover endpoints that nobody remembered to list.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from pydantic import ValidationError
from sqlalchemy import func, select, update

from app.api.deps import get_resolved_session
from app.api.routes.auth import OAUTH_BINDING_COOKIE
from app.core.config import Settings
from app.core.timeutil import utcnow
from app.db.models import (
    AuditEvent,
    ConnectedAccount,
    CredentialRecord,
    Job,
    OAuthState,
    User,
    UserSession,
)
from app.db.session import Database
from app.domain.enums import (
    ActorType,
    AuditOutcome,
    ConnectionProvider,
    ConnectionStatus,
    JobStatus,
    JobType,
    UserRole,
)
from app.integrations.http import RetryPolicy
from app.integrations.microsoft_graph.id_token import HttpJwksFetcher, IdTokenValidator
from app.integrations.microsoft_graph.oauth import EntraOAuthClient
from app.main import create_app
from app.services.authz import ROLE_PERMISSIONS
from tests.conftest import TEST_FERNET_KEY, make_settings
from tests.integration.conftest import (
    API,
    Harness,
    HarnessFactory,
    Persona,
    analyze,
    approve_and_execute,
    assert_error,
    find_action,
    review_body,
)
from tests.integration.fakes import FAKE_TENANT_ID, FakeEntra, FakeIdentity, _FakeMsalApp
from tests.integration.test_adapters_end_to_end import (
    GRAPH,
    FakeGraph,
    _connection,
    _graph_user,
    _sync,
    graph_message,
)
from tests.integration.test_auth_flows import (
    BASE,
    ENTRA,
    _begin,
    _callback,
    _connect,
    _error_of,
    cookie_attrs,
    entra_login,
    jar_value,
)
from tests.integration.test_rbac_matrix import FOLLOW_UP, World, build_world
from tests.security._adversarial_access_helpers import RouteInfo, discover_routes, openapi_operations

# ---------------------------------------------------------------------------------------------------
# Route inventory: every route discovered from the app must be classified here
# ---------------------------------------------------------------------------------------------------

P = API
PUBLIC_ROUTES = frozenset(
    {
        ("GET", f"{P}/health/live"),
        ("GET", f"{P}/health/ready"),
        ("GET", f"{P}/auth/config"),
        ("POST", f"{P}/auth/demo-login"),
        ("GET", f"{P}/auth/login"),
        ("GET", f"{P}/auth/callback"),
    }
)

# Protected route → the permission it must enforce (None: any signed-in user; data is tenant/owner scoped).
ROUTE_PERMISSION: dict[tuple[str, str], str | None] = {
    ("POST", f"{P}/auth/logout"): None,
    ("GET", f"{P}/auth/me"): None,
    ("GET", f"{P}/integrations"): None,
    # Connecting stores delegated tokens: only roles that may use them (viewers are read-only, docs/api.md).
    ("GET", f"{P}/integrations/microsoft/connect"): "email:sync",
    ("GET", f"{P}/integrations/azure-devops/connect"): "action:execute",
    ("DELETE", f"{P}/integrations/connections/{{connection_id}}"): None,  # own connections only
    ("GET", f"{P}/integrations/azure-devops/test"): "action:edit",
    ("GET", f"{P}/integrations/azure-devops/projects"): "action:edit",
    ("GET", f"{P}/integrations/azure-devops/projects/{{project}}/work-item-types"): "action:edit",
    ("POST", f"{P}/integrations/azure-devops/validate"): "action:edit",
    ("GET", f"{P}/emails"): "email:read",
    ("GET", f"{P}/emails/{{email_id}}"): "email:read",
    ("POST", f"{P}/emails/sync"): "email:sync",
    ("POST", f"{P}/emails/{{email_id}}/analyze"): "workflow:run",
    ("GET", f"{P}/workflows"): None,
    ("GET", f"{P}/workflows/{{run_id}}"): None,
    ("GET", f"{P}/workflows/{{run_id}}/analysis"): None,
    ("GET", f"{P}/workflows/{{run_id}}/actions"): None,
    ("GET", f"{P}/workflows/{{run_id}}/events"): None,
    ("POST", f"{P}/workflows/{{run_id}}/reanalyze"): "workflow:run",
    ("POST", f"{P}/workflows/{{run_id}}/cancel"): "action:edit",
    ("GET", f"{P}/actions"): None,
    ("GET", f"{P}/actions/{{action_id}}"): None,
    ("PATCH", f"{P}/actions/{{action_id}}"): "action:edit",
    ("POST", f"{P}/actions/{{action_id}}/approve"): "action:approve",
    ("POST", f"{P}/actions/{{action_id}}/reject"): "action:approve",
    ("POST", f"{P}/actions/{{action_id}}/cancel"): "action:edit",
    ("POST", f"{P}/actions/{{action_id}}/execute"): "action:execute",
    ("POST", f"{P}/actions/{{action_id}}/reconcile"): "action:execute",
    ("GET", f"{P}/actions/{{action_id}}/executions"): None,
    ("GET", f"{P}/actions/{{action_id}}/history"): None,
    ("GET", f"{P}/tasks"): None,
    ("PATCH", f"{P}/tasks/{{task_id}}"): "action:edit",
    ("GET", f"{P}/reply-drafts"): None,
    ("GET", f"{P}/audit-events/integrity"): "audit:read_all",
    ("GET", f"{P}/audit-events"): None,
    ("GET", f"{P}/settings"): None,
    ("PUT", f"{P}/settings"): "settings:write",
    ("GET", f"{P}/dashboard/summary"): None,
}

# Paginated list endpoints (paths relative to /api/v1).
LISTS = ("/emails", "/workflows", "/actions", "/tasks", "/reply-drafts", "/audit-events")


def _app(h: Harness) -> Any:
    """A FastAPI app with the harness configuration (route discovery only; requests go through the harness)."""
    return create_app(h.settings, h.db)


def _routes(h: Harness) -> list[RouteInfo]:
    return discover_routes(_app(h))


def _protected(h: Harness) -> list[RouteInfo]:
    return [r for r in _routes(h) if r.key not in PUBLIC_ROUTES]


def _random_ids() -> dict[str, str]:
    return {
        "email_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "action_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "connection_id": str(uuid.uuid4()),
        "project": "NUVEXA-Demo",
    }


def _body(route: RouteInfo, action: dict[str, Any] | None = None, settings_doc: dict[str, Any] | None = None) -> Any:
    """A schema-valid body for each request model (so a 422 never hides the check under test)."""
    action = action or {"version": 1, "payload_hash": "0" * 64, "payload": {"title": "x"}}
    model = route.body_model
    bodies: dict[str | None, Any] = {
        None: None,
        "SyncRequest": {},
        "AdoValidateRequest": {"payload": {"project": "NUVEXA-Demo", "title": "adversarial"}, "server_side": False},
        "ActionUpdateRequest": {"expected_version": action["version"], "payload": action["payload"]},
        "ApproveRequest": review_body(action),
        "RejectRequest": {"reason": "adversarial"},
        "CancelRequest": {},
        "ExecuteRequest": review_body(action),
        "TaskUpdateRequest": {"status": "done"},
        "SettingsUpdateRequest": settings_doc or {"expected_version": 1, "settings": {}},
        "DemoLoginRequest": {"persona": "member"},
    }
    assert model in bodies, f"{route}: add a valid body for request model {model}"
    return bodies[model]


async def _request(
    client: httpx.AsyncClient, route: RouteInfo, ids: dict[str, str], *, headers: dict[str, str] | None, body: Any
) -> httpx.Response:
    kwargs: dict[str, Any] = {"headers": headers or {}}
    if body is not None:
        kwargs["json"] = body
    return await client.request(route.method, route.concrete(ids), **kwargs)


async def test_every_discovered_route_is_classified(harness: Harness) -> None:
    routes = _routes(harness)
    discovered = {r.key for r in routes}
    # The walker must see at least what OpenAPI sees (it also sees include_in_schema=False routes).
    assert openapi_operations(_app(harness)) <= discovered
    unclassified = discovered - PUBLIC_ROUTES - set(ROUTE_PERMISSION)
    assert unclassified == set(), f"new routes need an access-control classification: {sorted(unclassified)}"
    assert set(ROUTE_PERMISSION) | PUBLIC_ROUTES == discovered, "classified routes that no longer exist"
    for perm in {p for p in ROUTE_PERMISSION.values() if p}:
        assert any(perm in perms for perms in ROLE_PERMISSIONS.values()), perm


async def test_every_protected_route_depends_on_the_session_and_public_ones_do_not(harness: Harness) -> None:
    for route in _routes(harness):
        if route.key in PUBLIC_ROUTES:
            assert not route.depends_on(get_resolved_session), f"{route} is documented public"
        else:
            assert route.depends_on(get_resolved_session), f"{route} does not require a session"


@dataclass
class _DeadSessions:
    clients: dict[str, httpx.AsyncClient] = field(default_factory=dict)
    csrf: dict[str, str] = field(default_factory=dict)


async def _dead_sessions(h: Harness) -> _DeadSessions:
    """Browsers whose session is anonymous, garbage, logged out, expired, idle or of a deactivated user."""
    out = _DeadSessions()
    out.clients["anonymous"] = h.tracked_client()
    garbage = h.tracked_client()
    garbage.cookies.set(h.settings.session_cookie_name, "x" * 43)
    garbage.cookies.set(h.settings.csrf_cookie_name, "y" * 43)
    out.clients["garbage"] = garbage
    out.csrf["garbage"] = "y" * 43

    async def session_client(name: str, persona: str) -> tuple[httpx.AsyncClient, str, Persona]:
        p = await h.persona(persona)
        token = jar_value(p.client, h.settings.session_cookie_name)
        assert token
        client = h.tracked_client()
        client.cookies.set(h.settings.session_cookie_name, token)
        out.clients[name] = client
        out.csrf[name] = p.headers["X-CSRF-Token"]
        return client, token, p

    _, _, logged_out = await session_client("logged_out", "member")
    assert (await logged_out.post("/auth/logout")).status_code == 204
    _, expired_token, _ = await session_client("expired", "approver")
    _, idle_token, _ = await session_client("idle", "admin")
    _, _, deactivated = await session_client("deactivated", "viewer")
    idle = timedelta(minutes=h.settings.session_idle_minutes + 1)
    async with h.db.session() as s:
        await s.execute(
            update(UserSession)
            .where(UserSession.token_hash == _hash(expired_token))
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await s.execute(
            update(UserSession).where(UserSession.token_hash == _hash(idle_token)).values(last_seen_at=utcnow() - idle)
        )
        await s.execute(update(User).where(User.id == deactivated.user_id).values(is_active=False))
        await s.commit()
    return out


def _hash(token: str) -> str:
    from app.core.security import hash_token

    return hash_token(token)


async def test_every_protected_route_rejects_dead_sessions_with_401(harness: Harness) -> None:
    dead = await _dead_sessions(harness)
    ids = _random_ids()
    failures = []
    for route in _protected(harness):
        for name, client in dead.clients.items():
            headers = {"X-CSRF-Token": dead.csrf[name]} if name in dead.csrf else {}
            resp = await _request(client, route, ids, headers=headers, body=_body(route))
            if resp.status_code != 401 or resp.json()["error"]["code"] != "authentication_required":
                failures.append((str(route), name, resp.status_code))
    assert failures == []


async def test_every_unsafe_route_requires_the_sessions_own_csrf_token(harness: Harness) -> None:
    member = await harness.persona("member")
    other = await harness.persona("approver")
    session_token = jar_value(member.client, harness.settings.session_cookie_name)
    assert session_token
    ids = _random_ids()
    attempts: dict[str, dict[str, str]] = {
        "missing": {},
        "empty": {"X-CSRF-Token": ""},
        "other_sessions_token": other.headers,
        "session_cookie_value": {"X-CSRF-Token": session_token},
        "padded": {"X-CSRF-Token": f" {member.headers['X-CSRF-Token']} "},
    }
    failures = []
    for route in (r for r in _protected(harness) if r.unsafe):
        for name, headers in attempts.items():
            resp = await _request(member.client, route, ids, headers=headers, body=_body(route))
            if resp.status_code != 403 or resp.json()["error"]["code"] != "csrf_failed":
                failures.append((str(route), name, resp.status_code))
    # Cookie tossing: an attacker who can plant a `nuvexa_csrf` cookie still cannot forge the header.
    member.client.cookies.set(harness.settings.csrf_cookie_name, "attacker-chosen-value")
    for route in (r for r in _protected(harness) if r.unsafe):
        resp = await _request(
            member.client, route, ids, headers={"X-CSRF-Token": "attacker-chosen-value"}, body=_body(route)
        )
        if resp.status_code != 403:
            failures.append((str(route), "tossed_cookie", resp.status_code))
    assert failures == []
    assert (await member.post("/emails/sync", json={})).status_code == 202  # positive control: the real token works


async def test_session_is_only_accepted_from_its_cookie(harness: Harness) -> None:
    member = await harness.persona("member")
    token = jar_value(member.client, harness.settings.session_cookie_name)
    assert token
    anon = harness.tracked_client()
    for kwargs in (
        {"params": {harness.settings.session_cookie_name: token}},
        {"params": {"session": token, "token": token}},
        {"headers": {"Authorization": f"Bearer {token}"}},
        {"headers": {"X-Session-Token": token}},
    ):
        assert_error(await anon.get(f"{API}/auth/me", **kwargs), 401, "authentication_required")


# ---------------------------------------------------------------------------------------------------
# Permission enforcement: every permissioned route, every role that lacks the permission
# ---------------------------------------------------------------------------------------------------


@dataclass
class _Owned:
    """Real resource ids owned by one persona (so a 404 cannot hide a missing permission check)."""

    ids: dict[str, str]
    action: dict[str, Any]


async def _rich_world(h: Harness) -> tuple[World, dict[str, _Owned]]:
    world = await build_world(h, viewer_data=True)
    assert world.viewer_run is not None
    # Viewer: its follow-up proposal is executed by the approver, so the viewer also owns a task.
    viewer_action = find_action(world.viewer_run, "create_follow_up_task")
    assert (await approve_and_execute(h, world["approver"], viewer_action))["status"] == "completed"
    [viewer_task] = (await world["viewer"].get("/tasks")).json()["items"]
    viewer_conn = (await world["viewer"].get("/integrations")).json()["microsoft"]["connections"][0]["id"]
    # Member: a task and a reply draft of its own, plus the still-pending incident work item.
    emails = (await world["member"].get("/emails", params={"limit": 100})).json()["items"]
    follow = next(e for e in emails if FOLLOW_UP.lower() in e["subject"].lower())
    follow_run = await analyze(h, world["member"], follow["id"])
    task_action = find_action(follow_run, "create_follow_up_task")
    assert (await approve_and_execute(h, world["member"], task_action))["status"] == "completed"
    assert (await approve_and_execute(h, world["member"], world.reply))["status"] == "completed"
    [member_task] = (await world["member"].get("/tasks")).json()["items"]
    member_conn = (await world["member"].get("/integrations")).json()["microsoft"]["connections"][0]["id"]
    owned = {
        "viewer": _Owned(
            {
                "email_id": world.viewer_run["run"]["email_message_id"],
                "run_id": world.viewer_run["run"]["id"],
                "action_id": viewer_action["id"],
                "task_id": viewer_task["id"],
                "connection_id": viewer_conn,
                "project": "NUVEXA-Demo",
            },
            viewer_action,
        ),
        "member": _Owned(
            {
                "email_id": world.email_id,
                "run_id": world.run["run"]["id"],
                "action_id": world.work_item["id"],
                "task_id": member_task["id"],
                "connection_id": member_conn,
                "project": "NUVEXA-Demo",
            },
            world.work_item,
        ),
    }
    return world, owned


async def test_every_permissioned_route_denies_every_role_that_lacks_the_permission(harness: Harness) -> None:
    world, owned = await _rich_world(harness)
    settings_doc = (await world["admin"].get("/settings")).json()
    put_body = {"expected_version": settings_doc["version"], "settings": settings_doc["settings"]}
    failures = []
    checked = 0
    for role in ("viewer", "member", "approver"):
        p = world[role]
        mine = owned.get(role, owned["member"])  # approvers see the member's resources tenant-wide
        for route in _protected(harness):
            perm = ROUTE_PERMISSION[route.key]
            if perm is None or perm in ROLE_PERMISSIONS[UserRole(role)]:
                continue
            checked += 1
            before = len(await harness.audit_events(event_type="permission_denied", actor_user_id=p.user_id))
            resp = await _request(
                p.client,
                route,
                mine.ids,
                headers=p.headers if route.unsafe else None,
                body=_body(route, mine.action, put_body),
            )
            denials = await harness.audit_events(event_type="permission_denied", actor_user_id=p.user_id)
            if resp.status_code != 403 or resp.json()["error"]["code"] != "permission_denied":
                failures.append((role, str(route), resp.status_code, resp.text[:120]))
            elif len(denials) != before + 1 or denials[-1].details.get("permission") != perm:
                failures.append((role, str(route), "denial not audited with the permission"))
    assert checked >= 20  # viewer 17 + member 2 + approver 1 at the time of writing
    assert failures == []
    # Nothing changed for the viewer although it attempted every write on its own items.
    assert (await world["viewer"].get("/tasks")).json()["items"][0]["status"] == "open"
    assert (await world["admin"].get("/settings")).json()["version"] == settings_doc["version"]


async def test_viewer_gets_no_success_from_any_unsafe_route_except_logout(harness: Harness) -> None:
    world, owned = await _rich_world(harness)
    viewer = world["viewer"]
    successes = []
    for route in (r for r in _protected(harness) if r.unsafe and r.key != ("POST", f"{P}/auth/logout")):
        resp = await _request(
            viewer.client, route, owned["viewer"].ids, headers=viewer.headers, body=_body(route, owned["viewer"].action)
        )
        if resp.status_code < 300:
            successes.append((str(route), resp.status_code))
    assert successes == []


# ---------------------------------------------------------------------------------------------------
# Invisible resources: 404, indistinguishable from ids that do not exist, never audited as denials
# ---------------------------------------------------------------------------------------------------


async def test_foreign_ids_are_indistinguishable_from_unknown_ids_on_every_route(harness: Harness) -> None:
    world, owned = await _rich_world(harness)
    member = owned["member"]
    routes = [r for r in _protected(harness) if set(r.path_params) - {"project"}]
    assert len(routes) >= 19
    for route in routes:  # positive control: the ids are real and readable by their owner
        if not route.unsafe:
            resp = await _request(world["member"].client, route, member.ids, headers=None, body=None)
            assert resp.status_code == 200, (str(route), resp.text)
    failures = []
    for persona in ("viewer", "member2", "outsider"):
        p = world[persona]
        for route in routes:
            headers = p.headers if route.unsafe else None
            body = _body(route, member.action)
            foreign = await _request(p.client, route, member.ids, headers=headers, body=body)
            unknown = await _request(p.client, route, _random_ids(), headers=headers, body=body)
            if foreign.status_code != 404 or unknown.status_code != 404:
                failures.append((persona, str(route), foreign.status_code, unknown.status_code))
                continue
            f_err, u_err = foreign.json()["error"], unknown.json()["error"]
            if (f_err["code"], f_err["message"], f_err["details"]) != (
                u_err["code"],
                u_err["message"],
                u_err["details"],
            ):
                failures.append((persona, str(route), f_err["message"], u_err["message"]))
            if (set(foreign.headers) ^ set(unknown.headers)) - {"content-length"}:
                failures.append((persona, str(route), "headers differ"))
        assert await harness.audit_events(event_type="permission_denied", actor_user_id=p.user_id) == []
    assert failures == []
    # The member's resources were not touched by any of the attempts.
    detail = (await world["member"].get(f"/actions/{member.ids['action_id']}")).json()
    assert (detail["status"], detail["version"], detail["approvals"]) == ("pending_approval", 1, [])
    assert (await world["member"].get("/tasks")).json()["items"][0]["status"] == "open"


async def test_list_endpoints_cannot_be_widened_by_injected_or_wildcard_parameters(harness: Harness) -> None:
    world, owned = await _rich_world(harness)
    m = owned["member"]
    member = world["member"]
    tenant = str(member.tenant_id)
    injected = {
        "tenant_id": tenant,
        "owner_user_id": str(member.user_id),
        "user_id": str(member.user_id),
        "owner": str(member.user_id),
        "all": "true",
        "scope": "tenant",
    }
    filters = {
        "/emails": [
            {"connected_account_id": m.ids["connection_id"]},
            *({"q": q} for q in ("%", "_", "\\", "%%", "*", "' OR '1'='1", "Production")),
        ],
        "/workflows": [{"email_id": m.ids["email_id"]}],
        "/actions": [{"workflow_run_id": m.ids["run_id"]}],
        "/audit-events": [
            {"resource_id": m.ids["email_id"]},
            {"action_id": m.ids["action_id"]},
            {"resource_type": "email"},
            {"event_type": "login"},
        ],
    }
    for persona in ("member2", "outsider"):
        p = world[persona]
        baseline = {path: (await p.get(path, params={"limit": 100})).json() for path in LISTS}
        for path in LISTS:
            for params in [injected, *filters.get(path, [])]:
                resp = await p.get(path, params={**params, **injected, "limit": 100})
                if resp.status_code == 422:
                    continue  # rejected input is fine; widening is not
                page = resp.json()
                foreign = [i for i in page["items"] if i.get("owner_user_id") not in (None, str(p.user_id))]
                assert foreign == [], (persona, path, params)
                assert page["total"] <= baseline[path]["total"], (persona, path, params)
            if path != "/audit-events":
                assert baseline[path]["total"] == 0, (persona, path)
            else:
                actors = {e["actor_user_id"] for e in baseline[path]["items"]}
                assert actors <= {str(p.user_id)}, persona
        summary = (await p.get("/dashboard/summary", params=injected)).json()
        assert (summary["emails_total"], summary["actions_proposed"], summary["workflows_by_status"]) == (0, 0, {})
    # Positive control: injected parameters neither widen nor narrow the owner's own view.
    own = (await member.get("/emails", params={"limit": 100})).json()["total"]
    assert own > 0
    assert (await member.get("/emails", params={**injected, "owner_user_id": str(world["member2"].user_id)})).json()[
        "total"
    ] == own


async def test_pagination_parameters_are_bounded(harness: Harness) -> None:
    member = await harness.persona("member")
    for path in LISTS:
        for params in ({"limit": 0}, {"limit": -1}, {"limit": 101}, {"limit": 10**6}, {"offset": -1}, {"limit": "1e3"}):
            assert_error(await member.get(path, params=params), 422, "validation_failed")
        page = (await member.get(path, params={"limit": 100, "offset": 10**6})).json()
        assert page["items"] == []


# Regression (Prompt 5): `offset` is the shared bounded `Offset` alias (le=1_000_000), so huge values are a 422.
async def test_huge_offsets_are_rejected_or_empty_never_a_server_error(harness: Harness) -> None:
    member = await harness.persona("member")
    app = harness.client._transport.app  # type: ignore[attr-defined]
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)  # observe the 500 instead of re-raising
    failures = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test", cookies=member.client.cookies) as raw:
        for path in LISTS:
            for offset in (2**63, 10**30):
                resp = await raw.get(f"{API}{path}", params={"offset": offset})
                if resp.status_code not in (200, 422) or (resp.status_code == 200 and resp.json()["items"]):
                    failures.append((path, offset, resp.status_code))
    assert failures == []


async def test_ids_in_request_bodies_cannot_redirect_a_write(harness: Harness) -> None:
    world, owned = await _rich_world(harness)
    member, member2, outsider = world["member"], world["member2"], world["outsider"]
    assert (await member2.post("/emails/sync", json={})).status_code == 202
    await harness.drain()
    foreign_email = (await member2.get("/emails", params={"limit": 1})).json()["items"][0]["id"]
    wi = world.work_item

    # Provenance cannot be re-pointed at another user's email.
    forged = {**wi["payload"], "title": "Edited", "source_email": {"email_id": foreign_email, "subject": "forged"}}
    resp = await member.patch(f"/actions/{wi['id']}", json={"expected_version": wi["version"], "payload": forged})
    assert resp.status_code == 200, resp.text
    assert resp.json()["payload"]["source_email"] == wi["payload"]["source_email"]
    current = resp.json()
    # Ownership/tenant fields smuggled into the payload are rejected, and ownership never changes.
    for extra in ({"owner_user_id": str(member2.user_id)}, {"tenant_id": str(outsider.tenant_id)}):
        body = {"expected_version": current["version"], "payload": {**current["payload"], **extra}}
        assert_error(await member.patch(f"/actions/{wi['id']}", json=body), 422, "validation_failed")
    assert (await member.get(f"/actions/{wi['id']}")).json()["owner_user_id"] == str(member.user_id)

    # Syncing someone else's mailbox (same tenant, other tenant, unknown id) looks the same and queues nothing.
    # Regression (Prompt 5): all three are an identical 404 (invariant 5), not a 409.
    outsider_conn = (await outsider.get("/integrations")).json()["microsoft"]["connections"][0]["id"]
    messages = set()
    for account in (owned["member"].ids["connection_id"], outsider_conn, str(uuid.uuid4())):
        messages.add(
            assert_error(await member2.post("/emails/sync", json={"connected_account_id": account}), 404)["message"]
        )
    assert len(messages) == 1
    async with harness.db.session() as s:
        jobs = (await s.execute(select(Job).where(Job.job_type == JobType.SYNC_MAILBOX))).scalars().all()
    assert {
        j.payload["connected_account_id"] for j in jobs if j.payload.get("requested_by") == str(member2.user_id)
    } <= {(await member2.get("/integrations")).json()["microsoft"]["connections"][0]["id"]}


async def test_audit_integrity_report_and_audit_log_are_tenant_scoped(harness: Harness) -> None:
    world = await build_world(harness)
    demo_tenant, other_tenant = world["member"].tenant_id, world["outsider"].tenant_id
    async with harness.db.session() as s:  # a forged row inserted around record_audit (no valid HMAC)
        forged = AuditEvent(
            id=uuid.uuid4(),
            tenant_id=demo_tenant,
            actor_type=ActorType.SYSTEM,
            event_type="forged_event",
            resource_type="test",
            outcome=AuditOutcome.SUCCESS,
            details={},
            created_at=utcnow(),
            integrity_hash="0" * 64,
        )
        s.add(forged)
        await s.commit()
        counts = {
            t: (
                await s.execute(select(func.count()).select_from(AuditEvent).where(AuditEvent.tenant_id == t))
            ).scalar_one()
            for t in (demo_tenant, other_tenant)
        }
    admin = (await world["admin"].get("/audit-events/integrity")).json()
    outsider = (await world["outsider"].get("/audit-events/integrity")).json()
    assert (admin["checked"], admin["tampered"]) == (counts[demo_tenant], [str(forged.id)])
    assert (outsider["checked"], outsider["tampered"], outsider["unsigned"]) == (counts[other_tenant], [], [])
    assert (await world["outsider"].get("/audit-events", params={"event_type": "forged_event"})).json()["total"] == 0
    assert (await world["admin"].get("/audit-events", params={"event_type": "forged_event"})).json()["total"] == 1
    for persona in ("member", "viewer", "member2"):
        assert_error(await world[persona].get("/audit-events/integrity"), 403, "permission_denied")


# ---------------------------------------------------------------------------------------------------
# Sessions: revocation, role changes, secrets hygiene, cookie flags in production
# ---------------------------------------------------------------------------------------------------


async def test_role_changes_apply_to_existing_sessions_immediately(harness: Harness) -> None:
    admin = await harness.persona("admin")
    tenant_id = await harness.demo_tenant_id()
    other = await harness.user_persona(await harness.create_user(tenant_id, UserRole.MEMBER), "other")
    assert (await other.post("/emails/sync", json={})).status_code == 202
    await harness.drain()
    member = await harness.persona("member")
    current = (await admin.get("/settings")).json()
    body = {"expected_version": current["version"], "settings": current["settings"]}

    async with harness.db.session() as s:
        await s.execute(update(User).where(User.id == admin.user_id).values(role=UserRole.VIEWER))
        await s.execute(update(User).where(User.id == member.user_id).values(role=UserRole.APPROVER))
        await s.commit()

    me = (await admin.get("/auth/me")).json()
    assert (me["user"]["role"], me["permissions"]) == ("viewer", sorted(ROLE_PERMISSIONS[UserRole.VIEWER]))
    assert_error(await admin.put("/settings", json=body), 403, "permission_denied")
    assert_error(await admin.get("/audit-events/integrity"), 403, "permission_denied")
    assert (await admin.get("/emails")).json()["total"] == 0  # no longer tenant-wide
    # A promotion widens the same session's view at once (no stale cached role).
    assert (await member.get("/emails")).json()["total"] > 0
    assert (await member.get("/auth/me")).json()["user"]["role"] == "approver"


async def test_session_secrets_never_reach_logs_urls_or_response_bodies(
    make_harness: HarnessFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    h = await make_harness(log_format="json")  # logging is configured now, i.e. onto the captured stdout
    capsys.readouterr()
    login = await h.client.post(f"{API}/auth/demo-login", json={"persona": "member"})
    token = jar_value(h.client, h.settings.session_cookie_name)
    csrf = jar_value(h.client, h.settings.csrf_cookie_name)
    assert token
    assert csrf
    headers = {"X-CSRF-Token": csrf}
    responses = [
        login,
        await h.client.get(f"{API}/auth/me"),
        await h.client.post(f"{API}/emails/sync", json={}, headers=headers),
        await h.client.post(f"{API}/emails/sync", json={}),  # csrf_failed
        await h.client.get(f"{API}/emails/{uuid.uuid4()}"),  # not_found
        await h.client.get(f"{API}/emails", params={"limit": 0}),  # validation_failed
    ]
    await h.drain()
    responses.append(await h.client.post(f"{API}/auth/logout", headers=headers))
    responses.append(await h.client.get(f"{API}/auth/me"))  # 401 after logout
    out = capsys.readouterr()
    logs = out.out + out.err
    assert "http_request" in logs  # the request log really was captured
    for secret in (token, csrf):
        assert secret not in logs
        for resp in responses:
            assert secret not in resp.text
            assert secret not in resp.headers.get("location", "")
            assert secret not in str(resp.request.url)


async def test_production_sign_in_cookies_are_secure_and_demo_login_is_off(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**ENTRA, app_env="production", demo_mode=False, audit_hmac_key="p" * 48)
    async with httpx.AsyncClient(transport=h.client._transport, base_url="https://test") as browser:
        begin = await browser.get(f"{API}/auth/login", params={"redirect_to": "/inbox"})
        assert begin.status_code == 302
        binding = cookie_attrs(begin, OAUTH_BINDING_COOKIE)
        assert {"secure", "httponly"} <= binding.keys()
        assert binding["samesite"].lower() == "lax"
        state = entra.flows_started[-1]["state"]
        done = await browser.get(f"{API}/auth/callback", params={"code": "c", "state": state})
        assert done.headers["location"] == f"{BASE}/inbox"
        session = cookie_attrs(done, h.settings.session_cookie_name)
        csrf = cookie_attrs(done, h.settings.csrf_cookie_name)
        assert {"secure", "httponly"} <= session.keys()
        assert "secure" in csrf
        assert "httponly" not in csrf
        assert session["samesite"].lower() == csrf["samesite"].lower() == "lax"
        assert session["value"] not in done.headers["location"]
        assert (await browser.get(f"{API}/auth/me")).status_code == 200
        assert_error(await browser.post(f"{API}/auth/demo-login", json={"persona": "admin"}), 403, "permission_denied")


@pytest.mark.parametrize(
    "content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"]
)
async def test_cross_site_form_posts_cannot_switch_a_browser_to_another_demo_persona(
    harness: Harness, content_type: str
) -> None:
    # Demo login is CSRF-exempt by design; only a JSON body (which needs a CORS preflight) may reach it.
    victim = await harness.persona("member")
    resp = await victim.client.post(
        f"{API}/auth/demo-login", content=b'{"persona": "admin"}', headers={"content-type": content_type}
    )
    assert_error(resp, 422, "validation_failed")
    assert "set-cookie" not in resp.headers
    assert (await victim.get("/auth/me")).json()["user"]["role"] == "member"  # neither replaced nor revoked


# ---------------------------------------------------------------------------------------------------
# Microsoft Entra ID: identity binding, OAuth state/redirects, outages
# ---------------------------------------------------------------------------------------------------

VICTIM_OID = "aaaaaaaa-0000-4000-8000-00000000000a"
ATTACKER_OID = "bbbbbbbb-0000-4000-8000-00000000000b"


# Regression (Prompt 5): the e-mail claim links only a not-yet-bound account; one bound to another oid is refused.
async def test_another_entra_identity_cannot_take_over_an_account_through_the_email_claim(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**ENTRA)
    entra.identity = FakeIdentity(oid=VICTIM_OID, email="dana.admin@contoso.example", name="Dana Admin")
    victim = h.tracked_client()
    await entra_login(victim, entra)
    victim_id = (await victim.get(f"{API}/auth/me")).json()["user"]["id"]
    async with h.db.session() as s:
        await s.execute(update(User).where(User.id == uuid.UUID(victim_id)).values(role=UserRole.ADMIN))
        await s.commit()

    # A different principal of the same Entra tenant presents the same (mutable, unverified) e-mail claim.
    entra.identity = FakeIdentity(oid=ATTACKER_OID, email="dana.admin@contoso.example", name="Mallory")
    attacker = h.tracked_client()
    await entra_login(attacker, entra)

    me = await attacker.get(f"{API}/auth/me")
    assert me.status_code == 401 or me.json()["user"]["id"] != victim_id, "the attacker was signed in as the victim"
    async with h.db.session() as s:
        row = await s.get(User, uuid.UUID(victim_id))
    assert row is not None
    assert (row.entra_object_id, row.display_name) == (VICTIM_OID, "Dana Admin")


VICTIM_EMAIL = "dana.admin@contoso.example"


def _as(entra: FakeEntra, oid: str, email: str = VICTIM_EMAIL, name: str = "Dana Admin") -> FakeEntra:
    entra.identity = FakeIdentity(oid=oid, email=email, name=name)
    return entra


async def test_a_refused_identity_takeover_is_audited_and_the_owner_keeps_access(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**ENTRA, bootstrap_admin_emails=[VICTIM_EMAIL])
    victim = h.tracked_client()
    await entra_login(victim, _as(entra, VICTIM_OID))
    victim_id = (await victim.get(f"{API}/auth/me")).json()["user"]["id"]

    attacker = h.tracked_client()
    resp = await entra_login(attacker, _as(entra, ATTACKER_OID, name="Mallory"))
    assert _error_of(resp) == "permission_denied"
    assert h.settings.session_cookie_name not in resp.headers.get("set-cookie", "")
    assert_error(await attacker.get(f"{API}/auth/me"), 401, "authentication_required")
    [denied] = await h.audit_events(event_type="login", outcome=AuditOutcome.DENIED)
    assert (denied.actor_user_id, denied.resource_type, denied.resource_id) == (None, "user", victim_id)
    assert (denied.details["reason"], denied.details["object_id"]) == ("identity_mismatch", ATTACKER_OID)
    async with h.db.session() as s:
        users = (await s.execute(select(User))).scalars().all()
    assert [(str(u.id), u.entra_object_id) for u in users] == [(victim_id, VICTIM_OID)]  # nothing created or re-bound

    owner = h.tracked_client()  # the real owner is unaffected
    await entra_login(owner, _as(entra, VICTIM_OID))
    assert (await owner.get(f"{API}/auth/me")).json()["user"] == (await victim.get(f"{API}/auth/me")).json()["user"]


async def test_the_bootstrap_admin_e_mail_claim_never_re_elevates_or_re_binds_an_account(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**ENTRA, bootstrap_admin_emails=[VICTIM_EMAIL, "erin.invited@contoso.example"])
    first = h.tracked_client()
    await entra_login(first, _as(entra, VICTIM_OID))
    me = (await first.get(f"{API}/auth/me")).json()["user"]
    assert me["role"] == "admin"  # the documented first-admin bootstrap still works for a new account
    async with h.db.session() as s:  # an administrator demotes the account
        await s.execute(update(User).where(User.id == uuid.UUID(me["id"])).values(role=UserRole.MEMBER))
        await s.commit()
    again = h.tracked_client()
    await entra_login(again, _as(entra, VICTIM_OID))
    assert (await again.get(f"{API}/auth/me")).json()["user"]["role"] == "member"  # the e-mail claim restores nothing

    # A pre-provisioned (invited) account is bound to the first verified identity, then never to another one.
    invited = await h.create_user(
        uuid.UUID(me["tenant_id"]), UserRole.VIEWER, email="erin.invited@contoso.example", demo_mailbox=False
    )
    erin = h.tracked_client()
    await entra_login(erin, _as(entra, "cccccccc-0000-4000-8000-00000000000c", "erin.invited@contoso.example", "Erin"))
    erin_me = (await erin.get(f"{API}/auth/me")).json()["user"]
    assert (erin_me["id"], erin_me["role"]) == (str(invited.id), "admin")  # bootstrap applies at first binding only
    mallory = h.tracked_client()
    resp = await entra_login(mallory, _as(entra, ATTACKER_OID, "erin.invited@contoso.example", "Mallory"))
    assert _error_of(resp) == "permission_denied"
    async with h.db.session() as s:
        row = await s.get(User, invited.id)
    assert row is not None
    assert (row.entra_object_id, row.display_name) == ("cccccccc-0000-4000-8000-00000000000c", "Erin")


async def test_a_deactivated_demo_persona_cannot_sign_in(harness: Harness) -> None:
    member = await harness.persona("member")
    viewer = await harness.persona("viewer")
    async with harness.db.session() as s:
        await s.execute(update(User).where(User.id == member.user_id).values(is_active=False))
        await s.commit()
    browser = harness.tracked_client()
    resp = await browser.post(f"{API}/auth/demo-login", json={"persona": "member"})
    assert_error(resp, 403, "permission_denied")
    assert "set-cookie" not in resp.headers
    assert_error(await browser.get(f"{API}/auth/me"), 401, "authentication_required")
    events = await harness.audit_events(event_type="login", actor_user_id=member.user_id)
    assert [e.outcome for e in events] == [AuditOutcome.SUCCESS, AuditOutcome.DENIED]
    assert events[-1].details == {"method": "demo", "persona": "member", "reason": "deactivated"}
    # A refused attempt from a browser that holds another session neither replaces nor revokes it.
    assert_error(await viewer.client.post(f"{API}/auth/demo-login", json={"persona": "member"}), 403)
    assert (await viewer.get("/auth/me")).json()["user"]["role"] == "viewer"


async def test_a_failed_sign_in_logs_its_error_code_unredacted(
    make_harness: HarnessFactory, entra: FakeEntra, capsys: pytest.CaptureFixture[str]
) -> None:
    h = await make_harness(**ENTRA, log_format="json")
    await _begin(h.client, entra)
    capsys.readouterr()
    assert _error_of(await _callback(h.client, code="c", state="forged-state")) == "permission_denied"
    out = capsys.readouterr()
    [line] = [json.loads(raw) for raw in (out.out + out.err).splitlines() if '"oauth_callback_failed"' in raw]
    assert line["error_code"] == "permission_denied"
    assert "code" not in line


async def test_connect_flows_are_bound_to_the_initiating_session_and_same_site(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**ENTRA, mail_mode="graph")
    member = await h.persona("member")
    # 1. A browser without the member's session cannot complete the member's flow, even holding its binding.
    state = await _begin(member.client, entra, path="/integrations/microsoft/connect")
    binding = jar_value(member.client, OAUTH_BINDING_COOKIE)
    assert binding
    anonymous = h.tracked_client()
    anonymous.cookies.set(OAUTH_BINDING_COOKIE, binding)
    assert _error_of(await _callback(anonymous, code="c", state=state)) == "permission_denied"
    assert _error_of(await _callback(member.client, code="c", state=state)) == "permission_denied"  # burnt
    # 2. Off-site redirect targets collapse to "/", and a completed state cannot be replayed.
    for target in ("https://evil.example/x", "//evil.example", "/\\evil.example", "/ok\r\nSet-Cookie: x=1"):
        state = await _begin(member.client, entra, path="/integrations/microsoft/connect", redirect_to=target)
        done = await _callback(member.client, code="c", state=state)
        assert done.headers["location"] == f"{BASE}/", target
        assert _error_of(await _callback(member.client, code="c", state=state)) == "permission_denied"
    conns = (await member.get("/integrations")).json()["microsoft"]["connections"]
    assert [c["provider"] for c in conns].count("microsoft_graph") == 1


@pytest.mark.parametrize("failure", ["connection_error", "timeout", "temporarily_unavailable", "server_error"])
async def test_entra_outage_while_redeeming_the_code_is_a_sanitized_login_error(
    make_harness: HarnessFactory, entra: FakeEntra, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    h = await make_harness(**ENTRA)
    if failure in ("connection_error", "timeout"):
        exc: OSError = (
            ConnectionError("connect to 10.20.30.40:443 refused")
            if failure == "connection_error"
            else TimeoutError("read timed out")
        )

        def boom(self: Any, *_: Any, **__: Any) -> Any:
            raise exc

        monkeypatch.setattr(_FakeMsalApp, "acquire_token_by_auth_code_flow", boom)
    else:
        entra.redeem_error = failure  # MSAL error dict with an AADSTS description that must not leak
    resp = await entra_login(h.client, entra)
    assert resp.headers["location"] == f"{BASE}/login?error=integration_error"
    assert h.settings.session_cookie_name not in resp.headers.get("set-cookie", "")
    assert_error(await h.get("/auth/me"), 401, "authentication_required")
    async with h.db.session() as s:
        assert (await s.execute(select(func.count()).select_from(User))).scalar_one() == 0
        [row] = (await s.execute(select(OAuthState))).scalars().all()
    assert row.consumed_at is not None  # single use even when the exchange fails


@dataclass
class _HttpJwksEntra(FakeEntra):
    """FakeEntra whose ID tokens are verified by the production JWKS fetcher (endpoint mocked with respx)."""

    def oauth_client(self, settings: Settings) -> EntraOAuthClient:
        self.client_id = settings.entra_client_id or ""
        self.authority_host = settings.entra_authority_host
        validator = IdTokenValidator(
            client_id=self.client_id,
            authority_host=self.authority_host,
            allowed_tenant_ids=settings.entra_allowed_tenant_ids,
            jwks_fetcher=HttpJwksFetcher(self.authority_host, timeout_seconds=1, cache={}),
        )
        return EntraOAuthClient(settings, app_factory=self.app_factory, id_token_validator=validator)


@pytest.mark.parametrize("outage", ["http_503", "connect_timeout", "not_json"])
async def test_signing_key_outage_during_sign_in_fails_closed_with_a_sanitized_error(
    make_harness: HarnessFactory, monkeypatch: pytest.MonkeyPatch, outage: str
) -> None:
    entra = _HttpJwksEntra().install(monkeypatch)
    h = await make_harness(**ENTRA)
    keys_url = f"{h.settings.entra_authority_host}/{FAKE_TENANT_ID}/discovery/v2.0/keys"
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(keys_url)
        if outage == "connect_timeout":
            route.mock(side_effect=httpx.ConnectTimeout("timed out"))
        else:
            route.mock(return_value=httpx.Response(503 if outage == "http_503" else 200, text="<html>proxy</html>"))
        resp = await entra_login(h.client, entra)
        assert route.called
    assert resp.headers["location"] == f"{BASE}/login?error=integration_error"
    assert_error(await h.get("/auth/me"), 401, "authentication_required")


async def test_entra_outage_when_starting_sign_in_or_connect_is_a_sanitized_error(
    make_harness: HarnessFactory, entra: FakeEntra, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await make_harness(**ENTRA)
    member = await h.persona("member")

    def boom(self: Any, *_: Any, **__: Any) -> Any:
        raise ConnectionError("discovery at 10.20.30.40 unreachable")

    monkeypatch.setattr(_FakeMsalApp, "initiate_auth_code_flow", boom)
    for client, path in ((h.tracked_client(), "/auth/login"), (member.client, "/integrations/microsoft/connect")):
        resp = await client.get(f"{API}{path}")
        err = assert_error(resp, 502, "integration_error")
        assert "10.20.30.40" not in err["message"]
        assert OAUTH_BINDING_COOKIE not in resp.headers.get("set-cookie", "")
    async with h.db.session() as s:
        assert (await s.execute(select(func.count()).select_from(OAuthState))).scalar_one() == 0


@pytest.fixture
def fake_graph() -> Iterator[FakeGraph]:
    """Microsoft Graph delta endpoint (same fake as tests/integration/test_adapters_end_to_end.py)."""
    fake = FakeGraph()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get(url__regex=rf"^{GRAPH}/me/mailFolders/inbox/messages/delta").mock(side_effect=fake)
        yield fake


@pytest.fixture
def instant_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RetryPolicy, "compute_delay", lambda self, retry_index, retry_after=None: 0.0)


async def _sync_job(h: Harness) -> Job:
    async with h.db.session() as s:
        [job] = (await s.execute(select(Job).where(Job.job_type == JobType.SYNC_MAILBOX))).scalars().all()
    return job


async def test_graph_token_without_mail_read_is_classified_and_requires_reconnect(
    make_harness: HarnessFactory, entra: FakeEntra, fake_graph: FakeGraph, instant_retries: None
) -> None:
    h, p, account_id = await _graph_user(make_harness, entra)
    denied = {"error": {"code": "ErrorAccessDenied", "message": "Access is denied. internal-detail-7f3a"}}
    fake_graph.queue("initial", httpx.Response(403, json=denied))
    await _sync(h, p, account_id)
    conn = await _connection(p)
    assert (conn["status"], conn["sync"]["last_status"]) == ("needs_reauth", "failed")
    assert "Reconnect" in conn["last_error"]
    assert "HTTP 403, ErrorAccessDenied" in conn["sync"]["last_error"]
    assert "internal-detail-7f3a" not in (await p.get("/integrations")).text
    job = await _sync_job(h)
    assert (job.status, job.attempts) == (JobStatus.FAILED, 1)  # not retried
    [failure] = await h.audit_events(event_type="mailbox_sync_failed")
    assert failure.details == {"category": "authorization"}
    assert len(fake_graph.requests) == 1
    assert (await p.get("/emails")).status_code == 200
    assert_error(await p.post("/emails/sync", json={"connected_account_id": account_id}), 409, "conflict")


@pytest.mark.parametrize("outage", ["http_503", "http_500", "connect_timeout", "read_timeout"])
async def test_graph_outage_during_sync_is_transient_and_keeps_the_connection(
    make_harness: HarnessFactory, entra: FakeEntra, fake_graph: FakeGraph, instant_retries: None, outage: str
) -> None:
    h, p, account_id = await _graph_user(make_harness, entra)
    body = {"error": {"code": "ServiceUnavailable", "message": "backend node db-7 down"}}
    item: httpx.Response | Exception = {
        "http_503": httpx.Response(503, json=body),
        "http_500": httpx.Response(500, json=body),
        "connect_timeout": httpx.ConnectTimeout("connect timed out"),
        "read_timeout": httpx.ReadTimeout("read timed out"),
    }[outage]
    fake_graph.queue("initial", *([item] * 10))
    await _sync(h, p, account_id)
    conn = await _connection(p)
    assert (conn["status"], conn["last_error"]) == ("active", None)  # an outage is not a revoked grant
    assert conn["sync"]["last_status"] == "failed"
    assert "db-7" not in conn["sync"]["last_error"]
    job = await _sync_job(h)
    assert job.status is JobStatus.QUEUED  # retried later with backoff
    assert job.run_after > utcnow()
    assert (await p.get("/emails")).json()["total"] == 0


@pytest.mark.parametrize("failure", ["connection_error", "temporarily_unavailable"])
async def test_entra_outage_during_token_refresh_is_transient_not_reauth(
    make_harness: HarnessFactory,
    entra: FakeEntra,
    fake_graph: FakeGraph,
    monkeypatch: pytest.MonkeyPatch,
    instant_retries: None,
    failure: str,
) -> None:
    h, p, account_id = await _graph_user(make_harness, entra)
    if failure == "connection_error":

        def boom(self: Any, *_: Any, **__: Any) -> Any:
            raise ConnectionError("token endpoint unreachable")

        monkeypatch.setattr(_FakeMsalApp, "acquire_token_silent_with_error", boom)
    else:
        entra.silent_error = failure
    await _sync(h, p, account_id)
    conn = await _connection(p)
    assert (conn["status"], conn["sync"]["last_status"]) == ("active", "failed")
    assert fake_graph.requests == []
    assert (await _sync_job(h)).status is JobStatus.QUEUED


async def test_revoked_refresh_token_requires_reconnect_and_reconnecting_recovers(
    make_harness: HarnessFactory, entra: FakeEntra, fake_graph: FakeGraph
) -> None:
    h, p, account_id = await _graph_user(make_harness, entra)
    entra.silent_error = "invalid_grant"
    await _sync(h, p, account_id)
    assert (await _connection(p))["status"] == "needs_reauth"
    for path in ("/integrations", "/emails", "/dashboard/summary", "/auth/me"):
        assert (await p.get(path)).status_code == 200, path

    entra.silent_error = None
    state = await _begin(p.client, entra, path="/integrations/microsoft/connect")
    assert (await _callback(p.client, code="c2", state=state)).headers["location"] == f"{BASE}/integrations"
    conn = await _connection(p)
    assert (conn["id"], conn["status"], conn["last_error"]) == (account_id, "active", None)
    fake_graph.page("initial", [graph_message("AAMk-1", "Back online")], delta="r1")
    await _sync(h, p, account_id)
    assert (await _connection(p))["sync"]["last_status"] == "succeeded"


# ---------------------------------------------------------------------------------------------------
# Azure DevOps delegated connection: revocable by its owner
# ---------------------------------------------------------------------------------------------------


async def _ado_connected_member(h: Harness, entra: FakeEntra) -> tuple[Persona, uuid.UUID]:
    member = await h.persona("member")
    await _connect(h, member.client, entra, "/integrations/azure-devops/connect")
    async with h.db.session() as s:
        ado_id = (
            await s.execute(
                select(ConnectedAccount.id).where(ConnectedAccount.provider == ConnectionProvider.AZURE_DEVOPS)
            )
        ).scalar_one()
    return member, ado_id


ADO_DELEGATED = {**ENTRA, "azure_devops_mode": "entra_delegated", "azure_devops_organization": "contoso"}


# Regression (Prompt 5): GET /integrations reports the user's own Azure DevOps grant as `azure_devops.connection`.
async def test_the_azure_devops_delegated_connection_id_is_discoverable(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**ADO_DELEGATED)
    member, ado_id = await _ado_connected_member(h, entra)
    overview = await member.get("/integrations")
    assert overview.json()["azure_devops"]["user_connected"] is True
    assert str(ado_id) in overview.text, "no id to disconnect the Azure DevOps connection with"


async def test_the_azure_devops_delegated_connection_can_be_revoked_by_its_owner_only(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**ADO_DELEGATED)
    member, ado_id = await _ado_connected_member(h, entra)
    for persona in ("approver", "admin"):
        other = await h.persona(persona)
        assert_error(await other.delete(f"/integrations/connections/{ado_id}"), 404, "not_found")
    assert (await member.delete(f"/integrations/connections/{ado_id}")).status_code == 204
    assert (await member.get("/integrations")).json()["azure_devops"]["user_connected"] is False
    async with h.db.session() as s:
        stored = (
            await s.execute(select(CredentialRecord).where(CredentialRecord.connected_account_id == ado_id))
        ).all()
    assert stored == []


async def test_the_azure_devops_connection_is_reported_to_its_owner_only_until_revoked(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**ADO_DELEGATED)
    member, ado_id = await _ado_connected_member(h, entra)
    overview = (await member.get("/integrations")).json()
    conn = overview["azure_devops"]["connection"]
    assert (conn["id"], conn["provider"], conn["status"], conn["sync"]) == (str(ado_id), "azure_devops", "active", None)
    assert all(c["provider"] != "azure_devops" for c in overview["microsoft"]["connections"])
    assert entra.access_token not in json.dumps(overview)  # an id and metadata, never the stored tokens
    for persona in ("approver", "admin"):  # tenant-wide roles do not see another user's grant
        other = await (await h.persona(persona)).get("/integrations")
        assert other.json()["azure_devops"]["connection"] is None
        assert str(ado_id) not in other.text

    # A grant that needs re-consent still holds stored tokens, so it stays revocable.
    async with h.db.session() as s:
        await s.execute(
            update(ConnectedAccount).where(ConnectedAccount.id == ado_id).values(status=ConnectionStatus.NEEDS_REAUTH)
        )
        await s.commit()
    ado = (await member.get("/integrations")).json()["azure_devops"]
    assert (ado["user_connected"], ado["connection"]["status"]) == (False, "needs_reauth")
    assert (await member.delete(f"/integrations/connections/{ado_id}")).status_code == 204
    ado = (await member.get("/integrations")).json()["azure_devops"]
    assert (ado["user_connected"], ado["connection"]) == (False, None)


@pytest.mark.parametrize(
    ("path", "permission"),
    [("/integrations/microsoft/connect", "email:sync"), ("/integrations/azure-devops/connect", "action:execute")],
)
async def test_viewers_cannot_store_delegated_mailbox_or_azure_devops_tokens(
    make_harness: HarnessFactory, entra: FakeEntra, path: str, permission: str
) -> None:
    h = await make_harness(**ADO_DELEGATED, mail_mode="graph")
    viewer = await h.persona("viewer")
    resp = await viewer.get(path)
    assert_error(resp, 403, "permission_denied")
    assert OAUTH_BINDING_COOKIE not in resp.headers.get("set-cookie", "")
    assert entra.flows_started == []  # no consent prompt is even started
    [denial] = await h.audit_events(event_type="permission_denied", actor_user_id=viewer.user_id)
    assert denial.details["permission"] == permission
    async with h.db.session() as s:
        assert (await s.execute(select(func.count()).select_from(OAuthState))).scalar_one() == 0
    member = await h.persona("member")  # positive control: roles that use the tokens may connect
    assert (await member.get(path)).status_code == 302


# ---------------------------------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------------------------------

ALLOWED_ORIGIN = "https://app.example.com"
LOOKALIKE_ORIGINS = (
    "null",
    "https://app.example.com.evil.example",
    "https://evilapp.example.com",
    "https://sub.app.example.com",
    "https://app.example.co",
    "http://app.example.com",
    "https://app.example.com:8443",
    "https://APP.EXAMPLE.COM",
    "https://app.example.com/",
    "file://",
)


@pytest.fixture
async def cors_client(database_url: str) -> AsyncIterator[httpx.AsyncClient]:
    settings = make_settings(database_url, cors_allowed_origins=ALLOWED_ORIGIN)
    app = create_app(settings, Database(settings))
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _preflight(origin: str, method: str = "POST", headers: str = "content-type,x-csrf-token") -> dict[str, str]:
    return {"Origin": origin, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": headers}


@pytest.mark.parametrize("origin", LOOKALIKE_ORIGINS)
async def test_cors_never_trusts_look_alike_or_null_origins(cors_client: httpx.AsyncClient, origin: str) -> None:
    pre = await cors_client.options(f"{API}/emails/sync", headers=_preflight(origin))
    assert pre.status_code == 400
    assert "access-control-allow-origin" not in pre.headers
    simple = await cors_client.get(f"{API}/auth/config", headers={"Origin": origin, "Cookie": "nuvexa_session=x"})
    # Starlette always adds `Access-Control-Allow-Credentials: true`; without a matching ACAO it grants nothing.
    assert "access-control-allow-origin" not in simple.headers


async def test_cors_allowed_origin_cannot_preflight_unlisted_methods_or_headers(cors_client: httpx.AsyncClient) -> None:
    for preflight in (_preflight(ALLOWED_ORIGIN, method="TRACE"), _preflight(ALLOWED_ORIGIN, headers="authorization")):
        assert (await cors_client.options(f"{API}/emails/sync", headers=preflight)).status_code == 400
    ok = await cors_client.get(f"{API}/auth/config", headers={"Origin": ALLOWED_ORIGIN})
    assert ok.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "origin" in ok.headers.get("vary", "").lower()  # caches must not serve one origin's answer to another


@pytest.mark.parametrize("origin", ["https://app.example.com\n", "http://localhost:5173\n", "https://[::1]:8443\n"])
def test_cors_origins_with_a_trailing_newline_are_rejected(origin: str) -> None:
    # `$` also matches before a final newline (same bug class as safe_redirect_path); a list is not stripped.
    with pytest.raises(ValidationError, match="must be an exact origin"):
        make_settings("sqlite+aiosqlite://", cors_allowed_origins=[origin])


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173", "http://[::1]:5173"])
def test_production_refuses_plain_http_cors_origins_even_for_loopback(origin: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(
            "sqlite+aiosqlite://",
            app_env="production",
            demo_mode=False,
            token_encryption_keys=TEST_FERNET_KEY,
            audit_hmac_key="a" * 64,
            cors_allowed_origins=origin,
        )
