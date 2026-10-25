"""RBAC matrix: persona x endpoint/operation, through the HTTP API with real sessions.

Personas: the four demo personas, a second member of the demo tenant and an *admin of another tenant*.
Rules under test (docs/api.md "Role permissions" + CLAUDE.md invariant 5):

* invisible resources (other owner without tenant-wide read, other tenant) → 404, never 403;
* visible but forbidden operations → 403 `permission_denied` and a `permission_denied` audit event;
* `ActionOut.permissions` flags are exactly what the API then allows.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from app.core.timeutil import utcnow
from app.db.models import ConnectedAccount
from app.domain.enums import AuditOutcome, ConnectionProvider, ConnectionStatus, UserRole
from app.schemas.api import ActionOut, AuditEventOut, Page
from app.services.email_sync_service import EmailSyncService
from tests.integration.conftest import (
    Harness,
    Persona,
    analyze,
    approve_and_execute,
    assert_error,
    find_action,
    review_body,
)

INCIDENT = "URGENT: Production login failures"
FOLLOW_UP = "Invoice sync between billing and ERP"
SEES_TENANT = {"admin", "approver"}
PERSONAS = ("admin", "approver", "member", "viewer", "member2", "outsider")


@dataclass
class World:
    h: Harness
    people: dict[str, Persona]
    run: dict[str, Any]  # member-owned incident workflow detail
    work_item: dict[str, Any]
    reply: dict[str, Any]
    email_id: str
    viewer_run: dict[str, Any] | None = None

    def __getitem__(self, name: str) -> Persona:
        return self.people[name]


async def build_world(h: Harness, *, viewer_data: bool = False) -> World:
    people = {name: await h.persona(name) for name in ("admin", "approver", "member", "viewer")}
    tenant_id = await h.demo_tenant_id()
    people["member2"] = await h.user_persona(await h.create_user(tenant_id, UserRole.MEMBER), "member2")
    other_tenant = await h.create_tenant("Rival Corp")
    people["outsider"] = await h.user_persona(await h.create_user(other_tenant, UserRole.ADMIN), "outsider")

    member = people["member"]
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await h.drain()
    emails = (await member.get("/emails", params={"limit": 100})).json()["items"]
    email = next(e for e in emails if INCIDENT.lower() in e["subject"].lower())
    run = await analyze(h, member, email["id"])
    world = World(
        h,
        people,
        run,
        find_action(run, "create_azure_devops_work_item"),
        find_action(run, "propose_email_reply"),
        email["id"],
    )
    if viewer_data:
        world.viewer_run = await _viewer_owned_run(h, people)
    return world


async def _viewer_owned_run(h: Harness, people: dict[str, Persona]) -> dict[str, Any]:
    """Viewers cannot sync; give the viewer a mailbox, ingest it worker-side, let an approver analyze."""
    viewer = people["viewer"]
    async with h.db.session() as s:
        account = ConnectedAccount(
            tenant_id=viewer.tenant_id,
            user_id=viewer.user_id,
            provider=ConnectionProvider.DEMO_MAILBOX,
            external_account_id="demo:viewer",
            email="vic.viewer@example.com",
            granted_scopes=["demo.mailbox.read"],
            status=ConnectionStatus.ACTIVE,
            connected_at=utcnow(),
        )
        s.add(account)
        await s.commit()
        await EmailSyncService(h.settings).sync_account(s, account.id)
    emails = (await viewer.get("/emails", params={"limit": 100})).json()["items"]
    email = next(e for e in emails if FOLLOW_UP.lower() in e["subject"].lower())
    return await analyze(h, people["approver"], email["id"])


def _expected_read(persona: str) -> int:
    return 200 if persona in SEES_TENANT | {"member"} else 404


async def _denials(h: Harness, user_id: uuid.UUID) -> list[Any]:
    return await h.audit_events(event_type="permission_denied", actor_user_id=user_id)


# ---------------------------------------------------------------------------------------------------
# Read visibility
# ---------------------------------------------------------------------------------------------------

READ_PATHS = (
    "/emails/{email}",
    "/workflows/{run}",
    "/workflows/{run}/analysis",
    "/workflows/{run}/actions",
    "/workflows/{run}/events",
    "/actions/{action}",
    "/actions/{action}/executions",
    "/actions/{action}/history",
)


@pytest.mark.parametrize("persona", PERSONAS)
async def test_read_visibility_of_a_members_resources(harness: Harness, persona: str) -> None:
    world = await build_world(harness)
    p = world[persona]
    ids = {"email": world.email_id, "run": world.run["run"]["id"], "action": world.work_item["id"]}
    expected = _expected_read(persona)
    for template in READ_PATHS:
        resp = await p.get(template.format(**ids))
        if expected == 404:
            assert_error(resp, 404, "not_found")
        else:
            assert resp.status_code == 200, (template, resp.text)


@pytest.mark.parametrize("persona", PERSONAS)
async def test_list_endpoints_are_scoped(harness: Harness, persona: str) -> None:
    world = await build_world(harness, viewer_data=True)
    p = world[persona]
    member_emails, viewer_emails = 10, 10  # first demo sync round each
    expected_emails = {
        "admin": member_emails + viewer_emails,
        "approver": member_emails + viewer_emails,
        "member": member_emails,
        "viewer": viewer_emails,
        "member2": 0,
        "outsider": 0,
    }[persona]
    expected_runs = {"admin": 2, "approver": 2, "member": 1, "viewer": 1, "member2": 0, "outsider": 0}[persona]
    assert (await p.get("/emails", params={"limit": 100})).json()["total"] == expected_emails
    assert (await p.get("/workflows")).json()["total"] == expected_runs
    actions = Page[ActionOut].model_validate((await p.get("/actions", params={"limit": 100})).json())
    owners = {a.owner_user_id for a in actions.items}
    if persona in SEES_TENANT:
        assert owners == {world["member"].user_id, world["viewer"].user_id}
    elif persona in ("member", "viewer"):
        assert owners == {p.user_id}
    else:
        assert actions.total == 0
    summary = (await p.get("/dashboard/summary")).json()
    assert summary["emails_total"] == expected_emails
    assert summary["actions_proposed"] == actions.total
    assert sum(summary["workflows_by_status"].values()) == expected_runs


@pytest.mark.parametrize("persona", PERSONAS)
async def test_audit_log_visibility(harness: Harness, persona: str) -> None:
    world = await build_world(harness)
    p = world[persona]
    page = Page[AuditEventOut].model_validate((await p.get("/audit-events", params={"limit": 100})).json())
    actors = {e.actor_user_id for e in page.items}
    if persona in SEES_TENANT:
        assert world["member"].user_id in actors  # sees other users' events
        assert any(e.actor_type.value == "agent" for e in page.items)  # and system/agent events
        member_events = (await p.get("/audit-events", params={"resource_id": world.email_id})).json()
        assert member_events["total"] >= 1
    else:
        assert actors <= {p.user_id}, "non-privileged users only see their own audit events"
        if persona in ("member", "viewer"):
            assert actors == {p.user_id}  # their own logins, syncs and analysis requests
    recent = (await p.get("/dashboard/summary")).json()["recent_activity"]
    assert {e["actor_user_id"] for e in recent} <= ({str(a) for a in actors} | {None})


# ---------------------------------------------------------------------------------------------------
# Operations on a member-owned action / workflow
# ---------------------------------------------------------------------------------------------------

Op = Callable[[World, Persona], Awaitable[httpx.Response]]


async def _edit(w: World, p: Persona) -> httpx.Response:
    payload = {**w.work_item["payload"], "title": "Edited by RBAC test"}
    return await p.patch(f"/actions/{w.work_item['id']}", json={"expected_version": 1, "payload": payload})


async def _approve(w: World, p: Persona) -> httpx.Response:
    return await p.post(f"/actions/{w.work_item['id']}/approve", json=review_body(w.work_item))


async def _reject(w: World, p: Persona) -> httpx.Response:
    return await p.post(f"/actions/{w.work_item['id']}/reject", json={"reason": "rbac"})


async def _cancel_action(w: World, p: Persona) -> httpx.Response:
    return await p.post(f"/actions/{w.work_item['id']}/cancel", json={})


async def _execute(w: World, p: Persona) -> httpx.Response:
    owner = w["member"]
    assert (await owner.post(f"/actions/{w.work_item['id']}/approve", json=review_body(w.work_item))).status_code == 200
    return await p.post(f"/actions/{w.work_item['id']}/execute", json=review_body(w.work_item))


async def _reconcile(w: World, p: Persona) -> httpx.Response:
    w.h.ado.fail_mode = "lookup_unavailable_after_timeout"
    done = await approve_and_execute(w.h, w["member"], w.work_item)
    assert done["status"] == "uncertain"
    w.h.ado.fail_mode = "none"
    return await p.post(f"/actions/{w.work_item['id']}/reconcile")


async def _analyze(w: World, p: Persona) -> httpx.Response:
    return await p.post(f"/emails/{w.email_id}/analyze")


async def _reanalyze(w: World, p: Persona) -> httpx.Response:
    return await p.post(f"/workflows/{w.run['run']['id']}/reanalyze")


async def _cancel_run(w: World, p: Persona) -> httpx.Response:
    return await p.post(f"/workflows/{w.run['run']['id']}/cancel")


OPS: dict[str, tuple[Op, int]] = {
    "edit": (_edit, 200),
    "approve": (_approve, 200),
    "reject": (_reject, 200),
    "cancel_action": (_cancel_action, 200),
    "execute": (_execute, 202),
    "reconcile": (_reconcile, 202),
    "analyze": (_analyze, 200),  # an active run exists → 200 created=false
    "reanalyze": (_reanalyze, 202),
    "cancel_workflow": (_cancel_run, 200),
}


@pytest.mark.parametrize("op", sorted(OPS))
@pytest.mark.parametrize("persona", PERSONAS)
async def test_operations_on_a_members_action(harness: Harness, persona: str, op: str) -> None:
    world = await build_world(harness)
    fn, success = OPS[op]
    resp = await fn(world, world[persona])
    if persona in SEES_TENANT | {"member"}:
        assert resp.status_code == success, resp.text
        if op == "execute":
            await harness.drain()
            assert (await world["member"].get(f"/actions/{world.work_item['id']}")).json()["status"] == "completed"
    else:
        assert_error(resp, 404, "not_found")
        assert await _denials(harness, world[persona].user_id) == []  # invisible ≠ denied: nothing leaks


async def test_approver_decisions_on_others_actions_are_attributed(harness: Harness) -> None:
    world = await build_world(harness)
    approver = world["approver"]
    resp = await approver.post(f"/actions/{world.work_item['id']}/approve", json=review_body(world.work_item))
    assert resp.status_code == 200
    approval = resp.json()["current_approval"]
    assert approval["decided_by_user_id"] == str(approver.user_id)
    assert approval["decided_by_name"] == "Riley Approver"
    [event] = await harness.audit_events(event_type="action_approved")
    assert event.actor_user_id == approver.user_id
    assert event.details["own_action"] is False
    resp = await approver.post(f"/actions/{world.work_item['id']}/execute", json=review_body(world.work_item))
    assert resp.status_code == 202
    await harness.drain()
    detail = (await world["member"].get(f"/actions/{world.work_item['id']}")).json()
    assert detail["status"] == "completed"
    assert detail["executions"][0]["requested_by_user_id"] == str(approver.user_id)


# ---------------------------------------------------------------------------------------------------
# Visible but forbidden: the viewer acting on items it owns
# ---------------------------------------------------------------------------------------------------


async def test_viewer_is_denied_every_write_on_its_own_items_and_each_denial_is_audited(harness: Harness) -> None:
    world = await build_world(harness, viewer_data=True)
    assert world.viewer_run is not None
    viewer = world["viewer"]
    task_action = find_action(world.viewer_run, "create_follow_up_task")
    run_id = world.viewer_run["run"]["id"]
    email_id = world.viewer_run["run"]["email_message_id"]

    # The approver executes the viewer's follow-up so the viewer owns a task, too.
    done = await approve_and_execute(harness, world["approver"], task_action)
    assert done["status"] == "completed"
    [task] = (await viewer.get("/tasks")).json()["items"]

    # The viewer can read everything it owns …
    action = (await viewer.get(f"/actions/{task_action['id']}")).json()
    assert all(v is False for v in action["permissions"].values())
    assert (await viewer.get(f"/workflows/{run_id}")).status_code == 200
    # … and every write is a 403 with an audit trail entry.
    attempts: list[tuple[str, Awaitable[httpx.Response]]] = [
        ("email:sync", viewer.post("/emails/sync", json={})),
        ("workflow:run", viewer.post(f"/emails/{email_id}/analyze")),
        ("workflow:run", viewer.post(f"/workflows/{run_id}/reanalyze")),
        ("action:edit", viewer.post(f"/workflows/{run_id}/cancel")),
        ("action:edit", viewer.patch(f"/actions/{task_action['id']}", json={"expected_version": 1, "payload": {}})),
        ("action:approve", viewer.post(f"/actions/{task_action['id']}/approve", json=review_body(task_action))),
        ("action:approve", viewer.post(f"/actions/{task_action['id']}/reject", json={"reason": "x"})),
        ("action:edit", viewer.post(f"/actions/{task_action['id']}/cancel", json={})),
        ("action:execute", viewer.post(f"/actions/{task_action['id']}/execute", json=review_body(task_action))),
        ("action:execute", viewer.post(f"/actions/{task_action['id']}/reconcile")),
        ("action:edit", viewer.patch(f"/tasks/{task['id']}", json={"status": "done"})),
        ("settings:write", viewer.put("/settings", json={"expected_version": 1, "settings": {}})),
    ]
    for permission, request in attempts:
        before = len(await _denials(harness, viewer.user_id))
        err = assert_error(await request, 403, "permission_denied")
        assert permission in err["message"] or "permission" in err["message"].lower()
        denials = await _denials(harness, viewer.user_id)
        assert len(denials) == before + 1, f"no audit event for denied {permission}"
        assert denials[-1].outcome is AuditOutcome.DENIED
        assert denials[-1].details["permission"] == permission
    assert (await viewer.get("/tasks")).json()["items"][0]["status"] == "open"


@pytest.mark.parametrize("persona", PERSONAS)
async def test_settings_write_requires_admin_of_the_same_tenant(harness: Harness, persona: str) -> None:
    world = await build_world(harness)
    p = world[persona]
    current = (await p.get("/settings")).json()
    body = {
        "expected_version": current["version"],
        "settings": {**current["settings"], "auto_analyze_new_emails": True},
    }
    resp = await p.put("/settings", json=body)
    if persona in ("admin", "outsider"):  # the outsider is an admin of its own tenant
        assert resp.status_code == 200, resp.text
    else:
        assert_error(resp, 403, "permission_denied")
        [denial] = await _denials(harness, p.user_id)
        assert (denial.resource_type, denial.details["permission"]) == ("settings", "settings:write")
    demo_settings = (await world["member"].get("/settings")).json()
    assert demo_settings["settings"]["auto_analyze_new_emails"] is (persona == "admin")


@pytest.mark.parametrize("persona", ["viewer", "member2", "outsider"])
async def test_sync_requires_permission_and_an_own_mailbox(harness: Harness, persona: str) -> None:
    world = await build_world(harness)
    p = world[persona]
    resp = await p.post("/emails/sync", json={})
    if persona == "viewer":
        assert_error(resp, 403, "permission_denied")
    else:
        assert resp.status_code == 202  # their own (empty) demo mailbox
    member_account = (await world["member"].get("/integrations")).json()["microsoft"]["connections"][0]["id"]
    resp = await p.post("/emails/sync", json={"connected_account_id": member_account})
    # Regression (Prompt 5): another user's connection is invisible (404, invariant 5), not a 409 conflict.
    assert_error(resp, *((403, "permission_denied") if persona == "viewer" else (404, "not_found")))


# ---------------------------------------------------------------------------------------------------
# Permission flags are truthful
# ---------------------------------------------------------------------------------------------------


async def _flags(p: Persona, action_id: str) -> dict[str, bool]:
    resp = await p.get(f"/actions/{action_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json()["permissions"])


def _ok(resp: httpx.Response) -> bool:
    if resp.status_code < 300:
        return True
    assert resp.status_code in (403, 409), resp.text  # forbidden or not allowed in this state
    return False


@pytest.mark.parametrize("persona", ["admin", "approver", "member", "viewer"])
async def test_permission_flags_match_what_the_api_allows(harness: Harness, persona: str) -> None:
    world = await build_world(harness, viewer_data=persona == "viewer")
    h, p = harness, world[persona]
    if persona == "viewer":
        assert world.viewer_run is not None
        actions = world.viewer_run["actions"]
        assert len(actions) == 1
        targets = [actions[0]] * 3
    else:
        targets = [world.work_item, world.reply, world.work_item]
    a, b, _ = targets

    flags = await _flags(p, a["id"])
    assert flags["can_execute"] is False  # nothing is approved yet
    assert flags["can_reconcile"] is False
    edited_payload = {**a["payload"]}
    edited_payload["title" if "title" in edited_payload else "reason"] = "Flag check edit"
    resp = await p.patch(f"/actions/{a['id']}", json={"expected_version": a["version"], "payload": edited_payload})
    assert _ok(resp) is flags["can_edit"]
    current = (await p.get(f"/actions/{a['id']}")).json()

    flags = await _flags(p, a["id"])
    assert _ok(await p.post(f"/actions/{a['id']}/approve", json=review_body(current))) is flags["can_approve"]
    flags = await _flags(p, a["id"])
    assert flags["can_execute"] is (persona != "viewer")
    assert _ok(await p.post(f"/actions/{a['id']}/execute", json=review_body(current))) is flags["can_execute"]
    await h.drain()
    after = await _flags(p, a["id"])
    if persona != "viewer":
        assert (await p.get(f"/actions/{a['id']}")).json()["status"] == "completed"
        assert not any(after.values()), "a completed action allows nothing"

    if persona != "viewer":
        flags = await _flags(p, b["id"])
        assert _ok(await p.post(f"/actions/{b['id']}/reject", json={"reason": "flag check"})) is flags["can_reject"]
        assert not any((await _flags(p, b["id"])).values())
        # Cancel + reconcile on a fresh proposal of another email.
        emails = (await world["member"].get("/emails", params={"limit": 100})).json()["items"]
        other = next(e for e in emails if e["subject"].startswith("Bug: CSV export"))
        run = await analyze(h, world["member"], other["id"])
        c = find_action(run, "propose_email_reply")
        flags = await _flags(p, c["id"])
        assert _ok(await p.post(f"/actions/{c['id']}/cancel", json={})) is flags["can_cancel"]
        d = find_action(run, "create_azure_devops_work_item")
        h.ado.fail_mode = "lookup_unavailable_after_timeout"
        assert (await approve_and_execute(h, world["member"], d))["status"] == "uncertain"
        h.ado.fail_mode = "none"
        flags = await _flags(p, d["id"])
        assert flags == {
            "can_edit": False,
            "can_approve": False,
            "can_reject": False,
            "can_cancel": False,
            "can_execute": False,
            "can_reconcile": True,
        }
        assert _ok(await p.post(f"/actions/{d['id']}/reconcile")) is True
    else:
        for path, body in (
            ("reject", {"reason": "x"}),
            ("cancel", {}),
            ("reconcile", None),
        ):
            flag = {"reject": "can_reject", "cancel": "can_cancel", "reconcile": "can_reconcile"}[path]
            flags = await _flags(p, a["id"])
            resp = (
                await p.post(f"/actions/{a['id']}/{path}", json=body)
                if body is not None
                else await p.post(f"/actions/{a['id']}/{path}")
            )
            assert _ok(resp) is flags[flag]
