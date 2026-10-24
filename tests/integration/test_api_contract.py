"""API contract: every endpoint in docs/api.md — response models, status codes, pagination, filters, errors.

Responses are parsed with the Pydantic models of `app/schemas/api.py` (the frontend contract), so any field
that disappears, changes type or violates an enum fails here.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import TypeAdapter
from sqlalchemy import select, update

from app.core.timeutil import utcnow
from app.db.models import ConnectedAccount, EmailMessage
from app.db.session import Database
from app.domain.enums import (
    ActionStatus,
    ConnectionProvider,
    EmailAnalysisStatus,
    UserRole,
)
from app.main import create_app
from app.schemas.api import (
    ActionDetail,
    ActionOut,
    AdoConnectionTestOut,
    AdoProjectsOut,
    AdoValidateOut,
    AdoWorkItemTypesOut,
    AnalysisOut,
    AuditEventOut,
    AuthConfigOut,
    DashboardSummaryOut,
    EmailDetail,
    EmailListItem,
    ExecuteAccepted,
    ExecutionOut,
    FollowUpTaskOut,
    HealthOut,
    IntegrationsOut,
    JobAccepted,
    MeOut,
    Page,
    ReplyDraftOut,
    SettingsOut,
    StartAnalysisOut,
    WorkflowDetail,
    WorkflowEventOut,
    WorkflowRunOut,
)
from app.services.authz import ROLE_PERMISSIONS
from tests.conftest import make_settings
from tests.integration.conftest import (
    API,
    DEMO_MESSAGE_COUNT,
    Harness,
    HarnessFactory,
    Persona,
    analyze,
    approve_and_execute,
    assert_error,
    find_action,
    review_body,
    sync_all,
)

DOCS = Path(__file__).resolve().parents[2] / "docs" / "api.md"
INCIDENT = "URGENT: Production login failures"
FOLLOW_UP = "Invoice sync between billing and ERP"
NEWSLETTER = "Engineering Newsletter"

ActionList = TypeAdapter(list[ActionOut])
EventList = TypeAdapter(list[WorkflowEventOut])
ExecutionList = TypeAdapter(list[ExecutionOut])
AuditList = TypeAdapter(list[AuditEventOut])


def documented_endpoints() -> set[tuple[str, str]]:
    """(METHOD, normalized path) for every row of the endpoint tables in docs/api.md."""
    rows = re.findall(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`", DOCS.read_text(), flags=re.M)
    assert len(rows) > 30, "docs/api.md endpoint tables not found"
    return {(m, _normalize(p.split("?")[0])) for m, p in rows}


def _normalize(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


DOCUMENTED = sorted(documented_endpoints())
PUBLIC = {
    ("GET", "/health/live"),
    ("GET", "/health/ready"),
    ("GET", "/auth/config"),
    ("POST", "/auth/demo-login"),
    ("GET", "/auth/login"),
    ("GET", "/auth/callback"),
}


def _concrete(path: str) -> str:
    return path.replace("{}", str(uuid.uuid4()) if "projects/{}" not in path else "NUVEXA-Demo")


async def _member_world(h: Harness) -> tuple[Persona, list[dict[str, Any]]]:
    member = await h.persona("member")
    emails = await sync_all(h, member)
    return member, emails


def _email(emails: list[dict[str, Any]], subject: str) -> dict[str, Any]:
    return next(e for e in emails if subject.lower() in e["subject"].lower())


# ---------------------------------------------------------------------------------------------------
# Endpoint inventory and generic error contract
# ---------------------------------------------------------------------------------------------------


async def test_every_documented_endpoint_is_routed_and_vice_versa(harness: Harness) -> None:
    spec = (await harness.get("/openapi.json")).json()
    routed = {
        (method.upper(), _normalize(path.removeprefix(API)))
        for path, ops in spec["paths"].items()
        for method in ops
        if method in ("get", "post", "put", "patch", "delete")
    }
    assert set(DOCUMENTED) - routed == set(), "documented in docs/api.md but not routed"
    assert routed - set(DOCUMENTED) == set(), "routed but missing from docs/api.md (document new endpoints)"


@pytest.mark.parametrize(("method", "path"), [pytest.param(*e, id=" ".join(e)) for e in DOCUMENTED if e not in PUBLIC])
async def test_protected_endpoints_require_a_session(harness: Harness, method: str, path: str) -> None:
    resp = await harness.client.request(method, f"{API}{_concrete(path)}", headers={"X-Request-ID": "req-401-check"})
    err = assert_error(resp, 401, "authentication_required")
    assert err["correlation_id"] == "req-401-check"


@pytest.mark.parametrize(
    ("method", "path"), [pytest.param(*e, id=" ".join(e)) for e in DOCUMENTED if e not in PUBLIC and e[0] != "GET"]
)
async def test_unsafe_endpoints_require_the_csrf_header(harness: Harness, method: str, path: str) -> None:
    member = await harness.persona("member")
    resp = await member.client.request(method, f"{API}{_concrete(path)}", json={})
    assert_error(resp, 403, "csrf_failed")
    resp = await member.client.request(method, f"{API}{_concrete(path)}", json={}, headers={"X-CSRF-Token": "x" * 43})
    assert_error(resp, 403, "csrf_failed")


async def test_generic_http_errors_use_the_envelope(harness: Harness) -> None:
    assert_error(await harness.get("/no-such-route"), 404, "not_found")
    assert_error(await harness.client.delete(f"{API}/health/live"), 405, "method_not_allowed")
    member = await harness.persona("member")
    assert_error(await member.get("/emails/not-a-uuid"), 422, "validation_failed")
    big = b"x" * (harness.settings.max_request_bytes + 1)
    resp = await member.client.post(
        f"{API}/emails/sync", content=big, headers={**member.headers, "content-type": "application/json"}
    )
    assert_error(resp, 413, "request_too_large")


async def test_correlation_id_is_echoed_or_replaced_when_unsafe(harness: Harness) -> None:
    resp = await harness.get("/health/live", headers={"X-Request-ID": "trace-abc-12345"})
    assert resp.headers["x-request-id"] == "trace-abc-12345"
    resp = await harness.get("/health/live", headers={"X-Request-ID": "bad id with spaces"})
    assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["x-request-id"])
    resp = await harness.get("/emails")  # no header → a fresh id, also inside the error body
    err = assert_error(resp, 401)
    assert re.fullmatch(r"[0-9a-f]{32}", err["correlation_id"])
    for header in ("x-content-type-options", "x-frame-options", "referrer-policy", "cache-control"):
        assert header in resp.headers


async def test_validation_errors_list_field_details(harness: Harness) -> None:
    member = await harness.persona("member")
    err = assert_error(await member.get("/emails", params={"limit": 0}), 422, "validation_failed")
    assert err["details"]
    assert err["details"][0]["loc"] == ["query", "limit"]
    assert {"loc", "msg", "type"} <= set(err["details"][0])
    err = assert_error(await member.post("/actions/" + str(uuid.uuid4()) + "/reject", json={}), 422)
    assert any(d["loc"] == ["body", "reason"] for d in err["details"])


async def test_rate_limit_returns_429_envelope(make_harness: HarnessFactory) -> None:
    h = await make_harness(rate_limit_enabled=True)
    statuses = []
    for _ in range(21):
        h.client.cookies.clear()  # anonymous caller → limited per client IP
        resp = await h.client.post(f"{API}/auth/demo-login", json={"persona": "viewer"})
        statuses.append(resp.status_code)
    assert statuses[:20] == [200] * 20
    assert_error(resp, 429, "rate_limited")


# ---------------------------------------------------------------------------------------------------
# Health + auth
# ---------------------------------------------------------------------------------------------------


async def test_health_endpoints(harness: Harness) -> None:
    live = HealthOut.model_validate((await harness.get("/health/live")).json())
    assert live.status == "ok"
    assert live.checks == {}
    ready = await harness.get("/health/ready")
    assert ready.status_code == 200
    assert HealthOut.model_validate(ready.json()).checks == {"database": "ok"}


async def test_health_ready_is_503_when_the_database_is_unreachable(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'missing-dir' / 'nope.db'}"
    settings = make_settings(url)
    app = create_app(settings, Database(settings))
    async with (
        LifespanManager(app) as manager,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=manager.app), base_url="http://t") as c,
    ):
        resp = await c.get(f"{API}/health/ready")
        assert (await c.get(f"{API}/health/live")).status_code == 200
    assert resp.status_code == 503
    body = HealthOut.model_validate(resp.json())
    assert body.status == "error"
    assert body.checks == {"database": "unavailable"}


async def test_auth_config_and_me_contract(harness: Harness) -> None:
    cfg = AuthConfigOut.model_validate((await harness.get("/auth/config")).json())
    assert cfg.environment == "test"
    assert (cfg.demo_mode, cfg.demo_login_enabled, cfg.entra_login_enabled) == (True, True, False)
    for persona, role in (("admin", "admin"), ("approver", "approver"), ("member", "member"), ("viewer", "viewer")):
        p = await harness.persona(persona)
        me = MeOut.model_validate((await p.get("/auth/me")).json())
        assert me.user.role.value == role
        assert me.user.tenant_name == "Elarxsys Demo"
        assert me.permissions == sorted(ROLE_PERMISSIONS[UserRole(role)])


async def test_demo_login_body_validation_and_disabled_mode(make_harness: HarnessFactory) -> None:
    h = await make_harness()
    assert_error(await h.client.post(f"{API}/auth/demo-login", json={"persona": "root"}), 422, "validation_failed")
    assert_error(await h.client.post(f"{API}/auth/demo-login", json={"persona": "member", "role": "admin"}), 422)
    off = await make_harness(demo_mode=False)
    assert AuthConfigOut.model_validate((await off.get("/auth/config")).json()).demo_login_enabled is False
    assert_error(await off.client.post(f"{API}/auth/demo-login", json={"persona": "member"}), 403, "permission_denied")


async def test_logout_contract(harness: Harness) -> None:
    member = await harness.persona("member")
    resp = await member.post("/auth/logout")
    assert resp.status_code == 204
    assert resp.content == b""
    assert_error(await member.get("/auth/me"), 401, "authentication_required")


# ---------------------------------------------------------------------------------------------------
# Integrations (mock Azure DevOps, demo mailbox)
# ---------------------------------------------------------------------------------------------------


async def test_integrations_overview_contract(harness: Harness) -> None:
    member = await harness.persona("member")
    before = IntegrationsOut.model_validate((await member.get("/integrations")).json())
    assert before.demo_mode is True
    assert before.microsoft.mode == "demo"
    assert before.microsoft.entra_configured is False
    assert before.microsoft.required_scopes == ["Mail.Read", "offline_access"]
    [conn] = before.microsoft.connections
    assert conn.provider is ConnectionProvider.DEMO_MAILBOX
    assert conn.sync is None
    ado = before.azure_devops
    assert (ado.mode, ado.organization, ado.configured, ado.requires_user_connection, ado.user_connected) == (
        "mock",
        "mock",
        True,
        False,
        True,
    )
    assert (ado.default_project, ado.default_work_item_type, ado.api_version) == ("NUVEXA-Demo", "Task", "7.1")
    assert (before.llm.provider, before.llm.structured_outputs, before.llm.observability_enabled) == (
        "heuristic",
        False,
        False,
    )
    await sync_all(harness, member)
    after = IntegrationsOut.model_validate((await member.get("/integrations")).json())
    sync = after.microsoft.connections[0].sync
    assert sync is not None
    assert sync.last_status.value == "succeeded"
    assert sync.messages_ingested_total == DEMO_MESSAGE_COUNT
    assert sync.last_sync_completed_at is not None
    viewer = await harness.persona("viewer")  # viewers get no mailbox
    assert IntegrationsOut.model_validate((await viewer.get("/integrations")).json()).microsoft.connections == []


async def test_connect_endpoints_without_entra_configuration(harness: Harness) -> None:
    member = await harness.persona("member")
    err = assert_error(await member.get("/integrations/microsoft/connect"), 500, "configuration_error")
    assert "not configured" in err["message"]
    assert_error(await harness.get("/auth/login"), 500, "configuration_error")
    assert_error(await member.get("/integrations/azure-devops/connect"), 409, "conflict")


async def test_disconnect_rules(harness: Harness) -> None:
    member = await harness.persona("member")
    conn = (await member.get("/integrations")).json()["microsoft"]["connections"][0]
    err = assert_error(await member.delete(f"/integrations/connections/{conn['id']}"), 409, "conflict")
    assert "demo mailbox" in err["message"]
    assert_error(await member.delete("/integrations/connections/not-a-uuid"), 422)


async def test_disconnect_unknown_connection_is_404(harness: Harness) -> None:
    """Regression (fixed in auth_service.disconnect_account): unknown ids were 409 instead of 404."""
    member = await harness.persona("member")
    assert_error(await member.delete(f"/integrations/connections/{uuid.uuid4()}"), 404, "not_found")


async def test_azure_devops_metadata_endpoints_in_mock_mode(harness: Harness) -> None:
    member = await harness.persona("member")
    check = AdoConnectionTestOut.model_validate((await member.get("/integrations/azure-devops/test")).json())
    assert (check.ok, check.mode, check.organization) == (True, "mock", "nuvexa-demo-org")
    projects = AdoProjectsOut.model_validate((await member.get("/integrations/azure-devops/projects")).json())
    assert [p.name for p in projects.items] == ["NUVEXA-Demo", "Customer-Portal"]
    types = AdoWorkItemTypesOut.model_validate(
        (await member.get("/integrations/azure-devops/projects/NUVEXA-Demo/work-item-types")).json()
    )
    assert types.project == "NUVEXA-Demo"
    assert "Bug" in [t.name for t in types.items]
    err = assert_error(
        await member.get("/integrations/azure-devops/projects/Ghost/work-item-types"), 502, "integration_error"
    )
    assert "not found" in err["message"]


async def test_azure_devops_validate_contract(harness: Harness) -> None:
    member = await harness.persona("member")
    base = {"project": "NUVEXA-Demo", "work_item_type": "Bug", "title": "Login fails", "priority": 2}

    async def validate(payload: dict[str, Any], server_side: bool = True) -> AdoValidateOut:
        resp = await member.post(
            "/integrations/azure-devops/validate", json={"payload": payload, "server_side": server_side}
        )
        assert resp.status_code == 200, resp.text
        return AdoValidateOut.model_validate(resp.json())

    ok = await validate(base)
    assert (ok.valid, ok.server_validated, ok.schema_issues, ok.policy_issues, ok.server_issues) == (
        True,
        True,
        [],
        [],
        [],
    )
    local_only = await validate(base, server_side=False)
    assert (local_only.valid, local_only.server_validated) == (True, False)
    schema = await validate({k: v for k, v in base.items() if k != "title"})
    assert schema.valid is False
    assert [i.field for i in schema.schema_issues] == ["title"]
    policy = await validate({**base, "work_item_type": "Spike"})
    assert policy.valid is False
    assert policy.server_validated is False  # never sent to the server when policy already fails
    assert any("Spike" in p for p in policy.policy_issues)
    server = await validate({**base, "project": "Ghost"})
    assert (server.valid, server.server_validated) == (False, True)
    assert server.server_issues[0].field == "System.TeamProject"
    with_source = await validate(
        {**base, "source_email": {"email_id": str(uuid.uuid4()), "subject": "s", "sender": "a@example.com"}}
    )
    assert with_source.valid is True
    assert_error(await member.post("/integrations/azure-devops/validate", json={"server_side": True}), 422)
    assert_error(await member.post("/integrations/azure-devops/validate", json={"payload": base, "extra": 1}), 422)


async def test_validate_with_a_reserved_field_allowed_by_settings_reports_an_issue(harness: Harness) -> None:
    """Regression (fixed in ActionPolicy.policy_issues): an allow-listed reserved field made /validate return 500."""
    admin = await harness.persona("admin")
    current = (await admin.get("/settings")).json()
    ado = {**current["settings"]["azure_devops"], "allowed_additional_fields": ["System.AssignedTo"]}
    resp = await admin.put(
        "/settings",
        json={"expected_version": current["version"], "settings": {**current["settings"], "azure_devops": ado}},
    )
    assert resp.status_code == 200, resp.text
    payload = {
        "project": "NUVEXA-Demo",
        "work_item_type": "Bug",
        "title": "x",
        "priority": 2,
        "additional_fields": {"System.AssignedTo": "someone@example.com"},
    }
    resp = await admin.post("/integrations/azure-devops/validate", json={"payload": payload})
    assert resp.status_code == 200, resp.text
    out = AdoValidateOut.model_validate(resp.json())
    assert out.valid is False
    assert out.server_validated is False
    assert any("System.AssignedTo" in issue for issue in out.policy_issues)


# ---------------------------------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------------------------------


async def test_email_list_detail_and_pagination(harness: Harness) -> None:
    member, _ = await _member_world(harness)
    page = Page[EmailListItem].model_validate((await member.get("/emails")).json())
    assert (page.total, page.limit, page.offset, len(page.items)) == (DEMO_MESSAGE_COUNT, 25, 0, DEMO_MESSAGE_COUNT)
    received = [e.received_at for e in page.items]
    assert received == sorted(received, reverse=True)

    seen: list[uuid.UUID] = []
    for offset in range(0, DEMO_MESSAGE_COUNT, 4):
        chunk = Page[EmailListItem].model_validate(
            (await member.get("/emails", params={"limit": 4, "offset": offset})).json()
        )
        assert (chunk.total, chunk.limit, chunk.offset) == (DEMO_MESSAGE_COUNT, 4, offset)
        seen.extend(e.id for e in chunk.items)
    assert seen == [e.id for e in page.items]
    beyond = Page[EmailListItem].model_validate((await member.get("/emails", params={"offset": 500})).json())
    assert (beyond.items, beyond.total) == ([], DEMO_MESSAGE_COUNT)
    assert len((await member.get("/emails", params={"limit": 100})).json()["items"]) == DEMO_MESSAGE_COUNT
    for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "x"}):
        assert_error(await member.get("/emails", params=bad), 422, "validation_failed")

    detail = EmailDetail.model_validate((await member.get(f"/emails/{page.items[0].id}")).json())
    assert detail.body_text
    assert detail.body_purged is False
    assert detail.internet_message_id
    assert detail.internet_message_id.endswith("@demo.example.com>")
    assert detail.to_recipients
    assert detail.to_recipients[0].address == "support-desk@example.com"
    with_attachment = next(e for e in page.items if e.has_attachments)
    detail = EmailDetail.model_validate((await member.get(f"/emails/{with_attachment.id}")).json())
    assert detail.attachments
    assert detail.attachments[0].name
    assert_error(await member.get(f"/emails/{uuid.uuid4()}"), 404, "not_found")


async def test_email_filters(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    incident = _email(emails, INCIDENT)
    newsletter = _email(emails, NEWSLETTER)
    await analyze(harness, member, incident["id"])
    await analyze(harness, member, newsletter["id"])

    async def ids(**params: Any) -> set[str]:
        resp = await member.get("/emails", params={"limit": 100, **params})
        assert resp.status_code == 200, resp.text
        page = Page[EmailListItem].model_validate(resp.json())
        assert page.total == len(page.items)
        return {str(e.id) for e in page.items}

    assert await ids(category="incident") == {incident["id"]}
    assert await ids(category="information_only") == {newsletter["id"]}
    assert await ids(priority="low") == {newsletter["id"]}
    assert await ids(analysis_status="awaiting_review") == {incident["id"], newsletter["id"]}
    assert len(await ids(analysis_status="not_analyzed")) == DEMO_MESSAGE_COUNT - 2
    assert await ids(analysis_status="failed") == set()
    account_id = incident["connected_account_id"]
    assert len(await ids(connected_account_id=account_id)) == DEMO_MESSAGE_COUNT
    assert await ids(connected_account_id=str(uuid.uuid4())) == set()
    assert await ids(category="incident", priority="low") == set()
    assert incident["id"] in await ids(q="urgent")  # subject, case-insensitive
    assert incident["id"] in await ids(q=incident["sender_address"].upper())  # sender address
    assert incident["id"] in await ids(q=incident["sender_name"])  # sender name
    for bad in ({"category": "nope"}, {"priority": "urgent"}, {"analysis_status": "x"}, {"q": "x" * 201}):
        assert_error(await member.get("/emails", params=bad), 422, "validation_failed")
    assert_error(await member.get("/emails", params={"connected_account_id": "nope"}), 422)


async def test_email_search_escapes_like_wildcards(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    template = emails[0]
    subjects = {
        "pct": "Budget 100% approved",
        "no_pct": "Budget 1000 approved",
        "under": "rename snake_case keys",
        "no_under": "rename snakeXcase keys",
        "slash": r"path C:\temp\new",
    }
    created: dict[str, str] = {}
    async with harness.db.session() as s:
        for key, subject in subjects.items():
            row = EmailMessage(
                tenant_id=member.tenant_id,
                owner_user_id=member.user_id,
                connected_account_id=uuid.UUID(template["connected_account_id"]),
                provider_message_id=f"like-{key}",
                subject=subject,
                sender_address="like@example.com",
                received_at=utcnow(),
                content_hash="0" * 64,
            )
            s.add(row)
            await s.flush()
            created[key] = str(row.id)
        await s.commit()

    async def search(q: str) -> set[str]:
        page = (await member.get("/emails", params={"q": q, "limit": 100})).json()
        return {e["id"] for e in page["items"]}

    assert await search("100%") == {created["pct"]}
    assert await search("%") == {created["pct"]}
    assert await search("snake_case") == {created["under"]}
    assert await search("_") == {created["under"]}
    assert await search("C:\\temp") == {created["slash"]}
    assert await search("%_%") == set()


async def test_deleted_emails_are_hidden_from_the_list(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    async with harness.db.session() as s:
        await s.execute(
            update(EmailMessage).where(EmailMessage.id == uuid.UUID(emails[0]["id"])).values(deleted_at=utcnow())
        )
        await s.commit()
    page = (await member.get("/emails", params={"limit": 100})).json()
    assert page["total"] == DEMO_MESSAGE_COUNT - 1
    assert emails[0]["id"] not in {e["id"] for e in page["items"]}
    err = assert_error(await member.post(f"/emails/{emails[0]['id']}/analyze"), 409, "conflict")
    assert "removed" in err["message"]


async def test_sync_contract(harness: Harness) -> None:
    member = await harness.persona("member")
    resp = await member.post("/emails/sync", json={})
    assert resp.status_code == 202
    accepted = JobAccepted.model_validate(resp.json())
    assert len(accepted.job_ids) == 1
    assert "1 account" in accepted.message
    account_id = (await member.get("/integrations")).json()["microsoft"]["connections"][0]["id"]
    again = JobAccepted.model_validate(
        (await member.post("/emails/sync", json={"connected_account_id": account_id})).json()
    )
    assert again.job_ids == accepted.job_ids  # deduplicated while the first job is still queued
    await harness.drain()
    assert_error(await member.post("/emails/sync", json={"connected_account_id": str(uuid.uuid4())}), 409, "conflict")
    assert_error(await member.post("/emails/sync", json={"connected_account_id": "x"}), 422)
    assert_error(await member.post("/emails/sync", json={"folder": "inbox"}), 422)
    assert_error(await member.post("/emails/sync"), 422)


async def test_analyze_contract(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    email = _email(emails, INCIDENT)
    first = await member.post(f"/emails/{email['id']}/analyze")
    assert first.status_code == 202
    started = StartAnalysisOut.model_validate(first.json())
    assert started.created is True
    assert started.job_id is not None
    assert started.workflow_run.status.value == "received"
    assert started.workflow_run.email_subject == email["subject"]
    second = await member.post(f"/emails/{email['id']}/analyze")
    assert second.status_code == 200
    existing = StartAnalysisOut.model_validate(second.json())
    assert (existing.created, existing.job_id, existing.workflow_run.id) == (False, None, started.workflow_run.id)
    listed = (await member.get("/emails", params={"q": "URGENT: Production"})).json()["items"][0]
    assert listed["analysis_status"] == "queued"
    assert listed["latest_workflow_run_id"] == str(started.workflow_run.id)
    assert_error(await member.post(f"/emails/{uuid.uuid4()}/analyze"), 404, "not_found")


# ---------------------------------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------------------------------


async def test_workflow_endpoints_contract(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    incident = _email(emails, INCIDENT)
    resp = await member.post(f"/emails/{incident['id']}/analyze")
    run_id = resp.json()["workflow_run"]["id"]
    # Before the worker runs: the run exists, analysis does not.
    assert_error(await member.get(f"/workflows/{run_id}/analysis"), 404, "not_found")
    pending = WorkflowDetail.model_validate((await member.get(f"/workflows/{run_id}")).json())
    assert (pending.analysis, pending.actions, pending.events) == (None, [], [])
    err = assert_error(await member.post(f"/workflows/{run_id}/reanalyze"), 409, "conflict")
    assert "busy" in err["message"]
    await harness.drain()

    detail = WorkflowDetail.model_validate((await member.get(f"/workflows/{run_id}")).json())
    assert detail.run.status.value == "pending_approval"
    assert detail.run.email_subject == incident["subject"]
    assert detail.run.model_provider == "heuristic"
    assert detail.run.metrics["llm_calls"] == 3
    assert detail.run.awaiting_review_since is not None
    analysis = AnalysisOut.model_validate((await member.get(f"/workflows/{run_id}/analysis")).json())
    assert analysis == detail.analysis
    assert analysis.attempt == 1
    actions = ActionList.validate_python((await member.get(f"/workflows/{run_id}/actions")).json())
    assert [a.id for a in actions] == [a.id for a in detail.actions]
    events = EventList.validate_python((await member.get(f"/workflows/{run_id}/events")).json())
    assert [e.id for e in events] == [e.id for e in detail.events]
    created = [e.created_at for e in events]
    assert created == sorted(created)
    for sub in ("", "/analysis", "/actions", "/events"):
        assert_error(await member.get(f"/workflows/{uuid.uuid4()}{sub}"), 404, "not_found")
    for verb in ("reanalyze", "cancel"):
        assert_error(await member.post(f"/workflows/{uuid.uuid4()}/{verb}"), 404, "not_found")

    accepted = await member.post(f"/workflows/{run_id}/reanalyze")
    assert accepted.status_code == 202
    assert len(JobAccepted.model_validate(accepted.json()).job_ids) == 1
    await harness.drain()
    reanalyzed = AnalysisOut.model_validate((await member.get(f"/workflows/{run_id}/analysis")).json())
    assert reanalyzed.attempt == 2

    cancelled = await member.post(f"/workflows/{run_id}/cancel")
    assert cancelled.status_code == 200
    run = WorkflowRunOut.model_validate(cancelled.json())
    assert run.status.value == "cancelled"
    assert run.email_subject == incident["subject"]
    assert_error(await member.post(f"/workflows/{run_id}/cancel"), 409, "invalid_state_transition")
    assert_error(await member.post(f"/workflows/{run_id}/reanalyze"), 409, "conflict")


async def test_workflow_list_filters_and_pagination(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    runs = [await analyze(harness, member, e["id"]) for e in emails[:5]]
    cancelled_id = runs[0]["run"]["id"]
    assert (await member.post(f"/workflows/{cancelled_id}/cancel")).status_code == 200
    await harness.drain()

    page = Page[WorkflowRunOut].model_validate((await member.get("/workflows")).json())
    assert page.total == 5
    assert all(r.email_subject for r in page.items)
    assert [r.created_at for r in page.items] == sorted((r.created_at for r in page.items), reverse=True)
    pending = Page[WorkflowRunOut].model_validate(
        (await member.get("/workflows", params={"status": "pending_approval"})).json()
    )
    assert pending.total == 4
    assert {r.status.value for r in pending.items} == {"pending_approval"}
    cancelled = (await member.get("/workflows", params={"status": "cancelled"})).json()
    assert [r["id"] for r in cancelled["items"]] == [cancelled_id]
    by_email = (await member.get("/workflows", params={"email_id": emails[1]["id"]})).json()
    assert [r["email_message_id"] for r in by_email["items"]] == [emails[1]["id"]]
    assert (await member.get("/workflows", params={"email_id": str(uuid.uuid4())})).json()["total"] == 0
    second = Page[WorkflowRunOut].model_validate(
        (await member.get("/workflows", params={"limit": 2, "offset": 2})).json()
    )
    assert (second.total, second.limit, second.offset, [r.id for r in second.items]) == (
        5,
        2,
        2,
        [r.id for r in page.items[2:4]],
    )
    for bad in ({"status": "done"}, {"limit": 101}, {"offset": -5}, {"email_id": "1"}):
        assert_error(await member.get("/workflows", params=bad), 422, "validation_failed")


# ---------------------------------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------------------------------


async def test_action_list_filters_and_pagination(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    incident = await analyze(harness, member, _email(emails, INCIDENT)["id"])
    follow = await analyze(harness, member, _email(emails, FOLLOW_UP)["id"])
    task = find_action(follow, "create_follow_up_task")
    done = await approve_and_execute(harness, member, task)
    assert done["status"] == "completed"
    reply = find_action(incident, "propose_email_reply")
    assert (await member.post(f"/actions/{reply['id']}/reject", json={"reason": "no"})).status_code == 200

    async def listing(params: list[tuple[str, str]] | dict[str, Any]) -> Page[ActionOut]:
        resp = await member.get("/actions", params=params)
        assert resp.status_code == 200, resp.text
        return Page[ActionOut].model_validate(resp.json())

    everything = await listing({})
    assert everything.total == 3
    assert all(a.permissions is not None and a.email_subject for a in everything.items)
    assert [a.created_at for a in everything.items] == sorted((a.created_at for a in everything.items), reverse=True)
    pending = await listing({"status": "pending_approval"})
    assert [a.action_type.value for a in pending.items] == ["create_azure_devops_work_item"]
    multi = await listing([("status", "pending_approval"), ("status", "completed")])
    assert {a.status.value for a in multi.items} == {"pending_approval", "completed"}
    assert multi.total == 2
    by_type = await listing({"action_type": "create_follow_up_task"})
    assert [a.id for a in by_type.items] == [uuid.UUID(task["id"])]
    by_run = await listing({"workflow_run_id": incident["run"]["id"]})
    assert by_run.total == 2
    combined = await listing({"workflow_run_id": incident["run"]["id"], "status": "rejected"})
    assert [a.id for a in combined.items] == [uuid.UUID(reply["id"])]
    paged = await listing({"limit": 1, "offset": 1})
    assert (paged.total, paged.limit, paged.offset, [a.id for a in paged.items]) == (3, 1, 1, [everything.items[1].id])
    for bad in ({"status": "open"}, {"action_type": "send_email"}, {"workflow_run_id": "x"}, {"limit": 1000}):
        assert_error(await member.get("/actions", params=bad), 422, "validation_failed")


async def test_action_detail_review_and_execution_contract(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    detail = await analyze(harness, member, _email(emails, INCIDENT)["id"])
    wi = find_action(detail, "create_azure_devops_work_item")
    aid = wi["id"]

    action = ActionDetail.model_validate((await member.get(f"/actions/{aid}")).json())
    assert (action.version, action.status.value, action.approvals, action.executions) == (1, "pending_approval", [], [])
    assert action.target_integration.value == "azure_devops"
    assert action.required_permissions
    assert len(action.payload_hash) == 64
    for sub in ("", "/executions", "/history"):
        assert_error(await member.get(f"/actions/{uuid.uuid4()}{sub}"), 404, "not_found")

    # PATCH: validation, staleness, success.
    edited_payload = {**wi["payload"], "title": "Login failures after the 2.4 deployment"}
    assert_error(await member.patch(f"/actions/{aid}", json={"expected_version": 1}), 422, "validation_failed")
    assert_error(await member.patch(f"/actions/{aid}", json={"expected_version": 0, "payload": {}}), 422)
    err = assert_error(
        await member.patch(
            f"/actions/{aid}", json={"expected_version": 1, "payload": {**wi["payload"], "priority": 9}}
        ),
        422,
        "validation_failed",
    )
    assert err["details"]
    assert_error(
        await member.patch(f"/actions/{aid}", json={"expected_version": 2, "payload": edited_payload}),
        409,
        "stale_version",
    )
    resp = await member.patch(f"/actions/{aid}", json={"expected_version": 1, "payload": edited_payload})
    assert resp.status_code == 200
    edited = ActionDetail.model_validate(resp.json())
    assert (edited.version, edited.source, edited.payload["title"]) == (2, "user_edited", edited_payload["title"])
    same = await member.patch(f"/actions/{aid}", json={"expected_version": 2, "payload": edited_payload})
    assert ActionDetail.model_validate(same.json()).version == 2  # no-op edit keeps the version

    # Approve: body validation, staleness, success, idempotent repeat.
    body = review_body(edited.model_dump(mode="json"))
    assert_error(await member.post(f"/actions/{aid}/approve", json={"version": 2, "payload_hash": "ab"}), 422)
    assert_error(await member.post(f"/actions/{aid}/approve", json={**body, "comment": "x" * 1001}), 422)
    assert_error(await member.post(f"/actions/{aid}/approve", json={**body, "extra": True}), 422)
    assert_error(await member.post(f"/actions/{aid}/approve", json=review_body(wi)), 409, "stale_version")
    err = assert_error(await member.post(f"/actions/{aid}/execute", json=body), 403, "permission_denied")
    assert "not been approved" in err["message"]
    approved = ActionDetail.model_validate(
        (await member.post(f"/actions/{aid}/approve", json={**body, "comment": "ok"})).json()
    )
    assert approved.status.value == "approved"
    assert approved.current_approval is not None
    assert approved.current_approval.reason == "ok"
    assert approved.current_approval.decided_by_name == "Morgan Member"
    assert approved.permissions is not None
    assert approved.permissions.can_execute is True
    repeat = ActionDetail.model_validate((await member.post(f"/actions/{aid}/approve", json=body)).json())
    assert len(repeat.approvals) == 1

    # Execute: staleness, acceptance, result.
    assert_error(await member.post(f"/actions/{aid}/execute", json=review_body(wi)), 409, "stale_version")
    assert_error(await member.post(f"/actions/{aid}/execute", json={"version": 2}), 422)
    resp = await member.post(f"/actions/{aid}/execute", json=body)
    assert resp.status_code == 202
    accepted = ExecuteAccepted.model_validate(resp.json())
    assert accepted.action.id == uuid.UUID(aid)
    await harness.drain()
    executions = ExecutionList.validate_python((await member.get(f"/actions/{aid}/executions")).json())
    assert [(e.attempt_number, e.status.value, e.action_version) for e in executions] == [(1, "succeeded", 2)]
    assert executions[0].external_url
    assert executions[0].external_url.startswith("mock://")
    history = AuditList.validate_python((await member.get(f"/actions/{aid}/history")).json())
    assert [e.created_at for e in history] == sorted(e.created_at for e in history)
    assert {e.action_id for e in history} == {uuid.UUID(aid)}
    assert_error(await member.post(f"/actions/{aid}/reject", json={"reason": "late"}), 409, "conflict")
    assert_error(await member.post(f"/actions/{aid}/cancel", json={}), 409, "invalid_state_transition")
    assert_error(
        await member.patch(f"/actions/{aid}", json={"expected_version": 2, "payload": edited_payload}), 409, "conflict"
    )
    err = assert_error(await member.post(f"/actions/{aid}/reconcile"), 409, "invalid_state_transition")
    assert "uncertain" in err["message"]
    assert_error(await member.post(f"/actions/{aid}/execute", json=body), 409, "invalid_state_transition")


async def test_reject_and_cancel_contract(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    detail = await analyze(harness, member, _email(emails, INCIDENT)["id"])
    wi = find_action(detail, "create_azure_devops_work_item")
    reply = find_action(detail, "propose_email_reply")
    assert_error(await member.post(f"/actions/{wi['id']}/reject", json={"reason": ""}), 422)
    assert_error(await member.post(f"/actions/{wi['id']}/reject", json={"reason": "x" * 1001}), 422)
    assert_error(await member.post(f"/actions/{uuid.uuid4()}/reject", json={"reason": "x"}), 404)
    rejected = ActionDetail.model_validate(
        (await member.post(f"/actions/{wi['id']}/reject", json={"reason": "duplicate"})).json()
    )
    assert rejected.status.value == "rejected"
    assert [(a.decision.value, a.reason) for a in rejected.approvals] == [("rejected", "duplicate")]
    assert_error(await member.post(f"/actions/{reply['id']}/cancel", json={"reason": "x" * 1001}), 422)
    cancelled = ActionDetail.model_validate((await member.post(f"/actions/{reply['id']}/cancel", json={})).json())
    assert cancelled.status.value == "cancelled"
    assert_error(await member.post(f"/actions/{reply['id']}/cancel", json={"reason": "again"}), 409)
    assert_error(await member.post(f"/actions/{uuid.uuid4()}/cancel", json={}), 404, "not_found")


async def test_reconcile_contract_for_uncertain_actions(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    detail = await analyze(harness, member, _email(emails, INCIDENT)["id"])
    wi = find_action(detail, "create_azure_devops_work_item")
    harness.ado.fail_mode = "lookup_unavailable_after_timeout"
    uncertain = await approve_and_execute(harness, member, wi)
    assert uncertain["status"] == "uncertain"
    harness.ado.fail_mode = "none"
    resp = await member.post(f"/actions/{wi['id']}/reconcile")
    assert resp.status_code == 202
    accepted = ExecuteAccepted.model_validate(resp.json())
    assert accepted.action.status is ActionStatus.UNCERTAIN
    await harness.drain()
    assert (await member.get(f"/actions/{wi['id']}")).json()["status"] == "completed"
    assert_error(await member.post(f"/actions/{uuid.uuid4()}/reconcile"), 404, "not_found")


# ---------------------------------------------------------------------------------------------------
# Tasks, drafts, audit, settings, dashboard
# ---------------------------------------------------------------------------------------------------


async def test_tasks_and_reply_drafts_contract(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    assert Page[FollowUpTaskOut].model_validate((await member.get("/tasks")).json()).total == 0
    follow = await analyze(harness, member, _email(emails, FOLLOW_UP)["id"])
    task_action = find_action(follow, "create_follow_up_task")
    await approve_and_execute(harness, member, task_action)
    incident = await analyze(harness, member, _email(emails, INCIDENT)["id"])
    reply_action = find_action(incident, "propose_email_reply")
    await approve_and_execute(harness, member, reply_action)

    tasks = Page[FollowUpTaskOut].model_validate((await member.get("/tasks")).json())
    assert (tasks.total, tasks.limit, tasks.offset) == (1, 25, 0)
    task = tasks.items[0]
    assert (task.action_id, task.status.value, task.completed_at) == (uuid.UUID(task_action["id"]), "open", None)
    assert task.email_message_id == uuid.UUID(task_action["email_message_id"])
    resp = await member.patch(f"/tasks/{task.id}", json={"status": "done"})
    assert resp.status_code == 200, resp.text
    done = FollowUpTaskOut.model_validate(resp.json())
    assert (done.status.value, done.completed_at is not None) == ("done", True)
    assert (await member.get("/tasks", params={"status": "open"})).json()["total"] == 0
    assert (await member.get("/tasks", params={"status": "done"})).json()["total"] == 1
    reopened = FollowUpTaskOut.model_validate((await member.patch(f"/tasks/{task.id}", json={"status": "open"})).json())
    assert (reopened.status.value, reopened.completed_at) == ("open", None)
    assert_error(await member.patch(f"/tasks/{task.id}", json={"status": "archived"}), 422)
    assert_error(await member.patch(f"/tasks/{task.id}", json={"status": "done", "title": "x"}), 422)
    assert_error(await member.patch(f"/tasks/{uuid.uuid4()}", json={"status": "done"}), 404, "not_found")
    assert_error(await member.get("/tasks", params={"status": "closed"}), 422)
    assert_error(await member.get("/tasks", params={"limit": 0}), 422)

    drafts = Page[ReplyDraftOut].model_validate((await member.get("/reply-drafts")).json())
    assert drafts.total == 1
    draft = drafts.items[0]
    assert draft.action_id == uuid.UUID(reply_action["id"])
    assert draft.to_recipients == reply_action["payload"]["to"]
    assert draft.subject == reply_action["payload"]["subject"]
    assert (await member.get("/reply-drafts", params={"offset": 1})).json()["items"] == []
    assert_error(await member.get("/reply-drafts", params={"limit": 101}), 422)


async def test_audit_events_contract_and_filters(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    detail = await analyze(harness, member, _email(emails, INCIDENT)["id"])
    wi = find_action(detail, "create_azure_devops_work_item")
    await approve_and_execute(harness, member, wi)
    viewer = await harness.persona("viewer")
    assert (await viewer.post("/emails/sync", json={})).status_code == 403
    admin = await harness.persona("admin")

    async def audit(**params: Any) -> Page[AuditEventOut]:
        resp = await admin.get("/audit-events", params={"limit": 100, **params})
        assert resp.status_code == 200, resp.text
        return Page[AuditEventOut].model_validate(resp.json())

    default = Page[AuditEventOut].model_validate((await admin.get("/audit-events")).json())
    assert default.limit == 50
    assert [e.created_at for e in default.items] == sorted((e.created_at for e in default.items), reverse=True)
    approved = await audit(event_type="action_approved")
    assert [(e.action_id, e.actor_name, e.actor_type.value) for e in approved.items] == [
        (uuid.UUID(wi["id"]), "Morgan Member", "user")
    ]
    assert (await audit(action_id=wi["id"])).total == len((await member.get(f"/actions/{wi['id']}/history")).json())
    assert {e.resource_type for e in (await audit(resource_type="action")).items} == {"action"}
    by_resource = await audit(resource_id=wi["id"])
    assert by_resource.total >= 4
    assert {e.resource_id for e in by_resource.items} == {wi["id"]}
    denied = await audit(outcome="denied")
    assert [(e.event_type, e.actor_name) for e in denied.items] == [("permission_denied", "Vic Viewer")]
    assert (await audit(event_type="no_such_event")).total == 0
    proposed = (await audit(event_type="actions_proposed")).items[0]
    assert (proposed.actor_type.value, proposed.actor_user_id, proposed.actor_name) == ("agent", None, None)
    paged = await audit(limit=2, offset=1)
    assert (paged.limit, paged.offset, len(paged.items)) == (2, 1, 2)
    for bad in (
        {"outcome": "maybe"},
        {"action_id": "x"},
        {"event_type": "e" * 65},
        {"resource_type": "r" * 65},
        {"resource_id": "i" * 65},
        {"limit": 101},
    ):
        assert_error(await admin.get("/audit-events", params=bad), 422, "validation_failed")


async def test_settings_contract(harness: Harness) -> None:
    admin = await harness.persona("admin")
    current = SettingsOut.model_validate((await admin.get("/settings")).json())
    assert current.version == 1
    assert current.settings.azure_devops.project == "NUVEXA-Demo"
    body = current.model_dump(mode="json")
    new = {**body["settings"], "auto_analyze_new_emails": True}
    assert_error(await admin.put("/settings", json={"expected_version": 1, "settings": {**new, "nope": 1}}), 422)
    assert_error(
        await admin.put(
            "/settings", json={"expected_version": 1, "settings": {**new, "attachment_processing_enabled": True}}
        ),
        422,
    )
    assert_error(await admin.put("/settings", json={"expected_version": 0, "settings": new}), 422)
    assert_error(await admin.put("/settings", json={"settings": new}), 422)
    resp = await admin.put("/settings", json={"expected_version": 1, "settings": new})
    assert resp.status_code == 200
    updated = SettingsOut.model_validate(resp.json())
    assert (updated.version, updated.settings.auto_analyze_new_emails) == (2, True)
    assert updated.updated_at is not None
    assert_error(await admin.put("/settings", json={"expected_version": 1, "settings": new}), 409, "stale_version")
    assert SettingsOut.model_validate((await admin.get("/settings")).json()).version == 2


async def test_dashboard_summary_contract(harness: Harness) -> None:
    member, emails = await _member_world(harness)
    empty = DashboardSummaryOut.model_validate((await member.get("/dashboard/summary")).json())
    assert (empty.emails_total, empty.emails_analyzed, empty.actions_proposed) == (DEMO_MESSAGE_COUNT, 0, 0)
    assert (empty.avg_approval_wait_seconds, empty.llm.total_tokens, empty.llm.avg_latency_ms) == (None, 0, None)
    assert empty.llm.estimated_cost_usd is None  # the heuristic agent reports no cost
    detail = await analyze(harness, member, _email(emails, INCIDENT)["id"])
    await approve_and_execute(harness, member, find_action(detail, "create_azure_devops_work_item"))
    summary = DashboardSummaryOut.model_validate((await member.get("/dashboard/summary")).json())
    assert (summary.emails_analyzed, summary.actions_proposed, summary.pending_approvals) == (1, 2, 1)
    assert summary.completed_actions == 1
    assert summary.categories == {"incident": 1}
    assert summary.avg_approval_wait_seconds is not None
    assert summary.avg_approval_wait_seconds >= 0
    assert summary.llm.avg_latency_ms is not None
    assert summary.workflows_by_status == {"pending_approval": 1}
    assert 0 < len(summary.recent_activity) <= 10


async def test_dashboard_counts_ignore_emails_removed_from_the_mailbox(harness: Harness) -> None:
    """Regression: analyzed/category counters included deleted emails, so analyzed could exceed total."""
    member, emails = await _member_world(harness)
    incident = _email(emails, INCIDENT)
    await analyze(harness, member, incident["id"])
    async with harness.db.session() as s:
        await s.execute(
            update(EmailMessage).where(EmailMessage.id == uuid.UUID(incident["id"])).values(deleted_at=utcnow())
        )
        await s.commit()
    summary = DashboardSummaryOut.model_validate((await member.get("/dashboard/summary")).json())
    listed = (await member.get("/emails", params={"limit": 100})).json()
    assert summary.emails_total == listed["total"] == DEMO_MESSAGE_COUNT - 1
    analyzed_listed = [
        e
        for e in listed["items"]
        if e["analysis_status"] in (EmailAnalysisStatus.AWAITING_REVIEW, EmailAnalysisStatus.DONE)
    ]
    assert summary.emails_analyzed == len(analyzed_listed) == 0
    assert summary.categories == {}


async def test_connection_sync_state_reflects_failures(harness: Harness) -> None:
    member = await harness.persona("member")
    async with harness.db.session() as s:
        account = (
            await s.execute(select(ConnectedAccount).where(ConnectedAccount.user_id == member.user_id))
        ).scalar_one()
        account_id = account.id
    await sync_all(harness, member)
    from app.db.models import EmailSyncCheckpoint

    async with harness.db.session() as s:
        await s.execute(
            update(EmailSyncCheckpoint)
            .where(EmailSyncCheckpoint.connected_account_id == account_id)
            .values(delta_link="demo:cursor:9999", last_sync_completed_at=utcnow() - timedelta(hours=1))
        )
        await s.commit()
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await harness.drain()  # invalid cursor → delta_expired → full resync succeeds without duplicates
    out = IntegrationsOut.model_validate((await member.get("/integrations")).json())
    sync = out.microsoft.connections[0].sync
    assert sync is not None
    assert sync.last_status.value == "succeeded"
    assert (await member.get("/emails")).json()["total"] == DEMO_MESSAGE_COUNT
