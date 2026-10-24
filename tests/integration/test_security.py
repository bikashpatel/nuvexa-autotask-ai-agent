"""Security invariants: authentication, CSRF, RBAC, tenant isolation, approval bypass attempts."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from app.core.errors import Conflict, InvalidStateTransition, NotFound
from app.db.models import AuditEvent, ProposedAction, Tenant, User
from app.domain.enums import ActionStatus, UserRole
from app.services.authz import AuditedPermissionDenied
from app.services.identity import actor_from_user
from tests.integration.conftest import Harness, find_action, sync_and_analyze

SPEC_SUBJECT = "Production login failures"


async def test_requires_authentication(harness: Harness) -> None:
    for path in ("/emails", "/actions", "/workflows", "/audit-events", "/settings", "/dashboard/summary", "/auth/me"):
        resp = await harness.get(path)
        assert resp.status_code == 401, path
        assert resp.json()["error"]["code"] == "authentication_required"


async def test_csrf_required_for_unsafe_methods(harness: Harness) -> None:
    await harness.login("member")
    resp = await harness.client.post("/api/v1/emails/sync", json={})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"
    resp = await harness.client.post("/api/v1/emails/sync", json={}, headers={"X-CSRF-Token": "forged"})
    assert resp.status_code == 403


async def test_error_schema_has_correlation_id(harness: Harness) -> None:
    resp = await harness.client.get("/api/v1/emails", headers={"X-Request-ID": "test-correlation-123"})
    assert resp.headers["x-request-id"] == "test-correlation-123"
    assert resp.json()["error"]["correlation_id"] == "test-correlation-123"
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_viewer_cannot_approve_or_sync(harness: Harness) -> None:
    member_headers = await harness.login("member")
    detail = await sync_and_analyze(harness, member_headers, SPEC_SUBJECT)
    wi = find_action(detail, "create_azure_devops_work_item")
    async with harness.new_client() as viewer_client:
        resp = await viewer_client.post("/api/v1/auth/demo-login", json={"persona": "viewer"})
        assert resp.status_code == 200
        headers = {"X-CSRF-Token": viewer_client.cookies.get(harness.settings.csrf_cookie_name) or ""}
        # A viewer does not see another member's actions at all.
        assert (await viewer_client.get(f"/api/v1/actions/{wi['id']}")).status_code == 404
        resp = await viewer_client.post("/api/v1/emails/sync", json={}, headers=headers)
        assert resp.status_code == 403


async def test_member_cannot_see_or_approve_other_members_action_but_approver_can(harness: Harness) -> None:
    member_headers = await harness.login("member")
    detail = await sync_and_analyze(harness, member_headers, SPEC_SUBJECT)
    wi = find_action(detail, "create_azure_devops_work_item")
    body = {"version": wi["version"], "payload_hash": wi["payload_hash"]}

    async with harness.db.session() as s:
        tenant_id = (await s.execute(select(User.tenant_id).where(User.email.like("%member%")))).scalars().first()
        other = User(tenant_id=tenant_id, email="other-member@example.com", display_name="Other", role=UserRole.MEMBER)
        s.add(other)
        await s.commit()
        other_actor = actor_from_user(other)
        with pytest.raises(NotFound):
            await harness.execution.execute(s, other_actor, uuid.UUID(wi["id"]))

    async with harness.new_client() as approver_client:
        await approver_client.post("/api/v1/auth/demo-login", json={"persona": "approver"})
        headers = {"X-CSRF-Token": approver_client.cookies.get(harness.settings.csrf_cookie_name) or ""}
        resp = await approver_client.post(f"/api/v1/actions/{wi['id']}/approve", json=body, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["current_approval"]["decided_by_name"]


async def test_cross_tenant_access_is_invisible(harness: Harness) -> None:
    headers = await harness.login("member")
    detail = await sync_and_analyze(harness, headers, SPEC_SUBJECT)
    wi = find_action(detail, "create_azure_devops_work_item")
    async with harness.db.session() as s:
        other_tenant = Tenant(name="Other Co", slug="other-co")
        s.add(other_tenant)
        await s.flush()
        admin = User(
            tenant_id=other_tenant.id, email="admin@other.example", display_name="Other Admin", role=UserRole.ADMIN
        )
        s.add(admin)
        await s.commit()
        actor = actor_from_user(admin)
        with pytest.raises(NotFound):
            await harness.execution.execute(s, actor, uuid.UUID(wi["id"]))


async def test_direct_service_execution_without_approval_is_denied_and_audited(harness: Harness) -> None:
    headers = await harness.login("member")
    detail = await sync_and_analyze(harness, headers, SPEC_SUBJECT)
    wi = find_action(detail, "create_azure_devops_work_item")
    async with harness.db.session() as s:
        user = (await s.execute(select(User).where(User.id == uuid.UUID(wi["owner_user_id"])))).scalar_one()
        # Even if someone flips the status directly, there is no approval row for this version/hash.
        await s.execute(
            update(ProposedAction).where(ProposedAction.id == uuid.UUID(wi["id"])).values(status=ActionStatus.APPROVED)
        )
        await s.commit()
        with pytest.raises(AuditedPermissionDenied):
            await harness.execution.execute(s, actor_from_user(user), uuid.UUID(wi["id"]))
    assert harness.ado.clients == [] or all(not c.items() for c in harness.ado.clients)


async def test_forged_graph_resume_cannot_execute_unapproved_action(harness: Harness) -> None:
    headers = await harness.login("member")
    detail = await sync_and_analyze(harness, headers, SPEC_SUBJECT)
    wi = find_action(detail, "create_azure_devops_work_item")
    # Simulates a resume payload claiming approval (e.g. produced by a compromised component or the LLM).
    async with harness.db.session() as s:
        owner = await s.get(User, uuid.UUID(wi["owner_user_id"]))
        assert owner is not None
    await harness.engine.resume(
        uuid.UUID(detail["run"]["id"]),
        owner.tenant_id,
        {
            "type": "execute",
            "action_id": wi["id"],
            "actor_user_id": wi["owner_user_id"],
            "version": wi["version"],
            "payload_hash": wi["payload_hash"],
        },
    )
    action = (await harness.get(f"/actions/{wi['id']}")).json()
    assert action["status"] == "pending_approval"
    assert action["executions"] == []
    async with harness.db.session() as s:
        denied = (
            (await s.execute(select(AuditEvent).where(AuditEvent.event_type == "permission_denied"))).scalars().all()
        )
        assert denied


async def test_tampered_payload_is_refused(harness: Harness) -> None:
    headers = await harness.login("member")
    detail = await sync_and_analyze(harness, headers, SPEC_SUBJECT)
    wi = find_action(detail, "create_azure_devops_work_item")
    body = {"version": wi["version"], "payload_hash": wi["payload_hash"]}
    assert (await harness.post(f"/actions/{wi['id']}/approve", headers, json=body)).status_code == 200
    async with harness.db.session() as s:
        action = await s.get(ProposedAction, uuid.UUID(wi["id"]))
        assert action is not None
        action.payload = {**action.payload, "title": "Tampered after approval"}
        await s.commit()
        user = await s.get(User, action.owner_user_id)
        assert user is not None
        with pytest.raises(Conflict):
            await harness.execution.execute(s, actor_from_user(user), action.id)


async def test_rejected_action_never_executes_via_service(harness: Harness) -> None:
    headers = await harness.login("member")
    detail = await sync_and_analyze(harness, headers, SPEC_SUBJECT)
    wi = find_action(detail, "create_azure_devops_work_item")
    await harness.post(f"/actions/{wi['id']}/reject", headers, json={"reason": "not needed"})
    async with harness.db.session() as s:
        user = await s.get(User, uuid.UUID(wi["owner_user_id"]))
        assert user is not None
        with pytest.raises(InvalidStateTransition):
            await harness.execution.execute(s, actor_from_user(user), uuid.UUID(wi["id"]))


async def test_settings_require_admin(harness: Harness) -> None:
    headers = await harness.login("member")
    current = (await harness.get("/settings")).json()
    resp = await harness.client.put(
        "/api/v1/settings",
        headers=headers,
        json={"expected_version": current["version"], "settings": current["settings"]},
    )
    assert resp.status_code == 403
    async with harness.new_client() as admin_client:
        await admin_client.post("/api/v1/auth/demo-login", json={"persona": "admin"})
        admin_headers = {"X-CSRF-Token": admin_client.cookies.get(harness.settings.csrf_cookie_name) or ""}
        new_settings = {**current["settings"], "auto_analyze_new_emails": True}
        resp = await admin_client.put(
            "/api/v1/settings",
            headers=admin_headers,
            json={"expected_version": current["version"], "settings": new_settings},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["version"] == current["version"] + 1
        stale = await admin_client.put(
            "/api/v1/settings",
            headers=admin_headers,
            json={"expected_version": current["version"], "settings": new_settings},
        )
        assert stale.status_code == 409


async def test_request_size_limit(harness: Harness) -> None:
    headers = await harness.login("member")
    big = "x" * (harness.settings.max_request_bytes + 10)
    resp = await harness.client.post(
        "/api/v1/emails/sync", content=big, headers={**headers, "content-type": "application/json"}
    )
    assert resp.status_code == 413
