"""Full workflow lifecycles through HTTP + worker for every demo email, plus every review path.

sync → analyze → proposals → (edit) → approve → execute → completed, and reject / cancel / re-analysis,
with the timeline, tasks, reply drafts, dashboard counters and the audit trail checked at each step.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Any

import pytest
from sqlalchemy import func, select

from app.core.security import compute_payload_hash
from app.db.models import EmailMessage, ProposedAction, WorkflowRun
from app.domain.enums import ActionStatus, EmailAnalysisStatus
from app.integrations.azure_devops.mapping import FIELD_TITLE
from app.integrations.microsoft_graph.demo_mailbox import load_demo_fixture
from app.schemas.actions import parse_payload
from app.schemas.api import ActionDetail, AnalysisOut, DashboardSummaryOut, WorkflowDetail
from tests.integration.conftest import (
    Harness,
    Persona,
    analyze,
    approve_and_execute,
    assert_error,
    email_id_by_provider_id,
    find_action,
    review_body,
    sync_all,
)

FIXTURE = load_demo_fixture()["messages"]
MESSAGE_IDS = [m["id"] for m in FIXTURE]
INCIDENT_ID = FIXTURE[0]["id"]  # URGENT: Production login failures (work item + reply)
FEATURE_ID = next(m["id"] for m in FIXTURE if m["subject"].startswith("Feature request"))  # work item only
FOLLOW_UP_ID = next(m["id"] for m in FIXTURE if "Invoice sync" in m["subject"])  # follow-up task
NEWSLETTER_ID = next(m["id"] for m in FIXTURE if "Newsletter" in m["subject"])  # no action
ANALYSIS_STEPS = ["prepare", "classify", "extract", "validate", "plan"]


async def _start(h: Harness, provider_id: str) -> tuple[Persona, uuid.UUID, dict[str, Any]]:
    member = await h.persona("member")
    await sync_all(h, member)
    email_id = await email_id_by_provider_id(h, member.user_id, provider_id)
    return member, email_id, await analyze(h, member, email_id)


def _check_proposal(action: dict[str, Any], email: dict[str, Any]) -> None:
    payload = parse_payload(action["payload"])
    assert payload.kind == action["action_type"]
    assert action["payload_hash"] == compute_payload_hash(
        action["action_type"], action["target_integration"], action["payload"]
    )
    assert len(action["idempotency_key"]) == 64
    assert (action["status"], action["version"], action["source"]) == ("pending_approval", 1, "agent")
    assert action["validation_errors"] == []
    assert action["title"]
    assert action["rationale"]
    assert action["required_permissions"]
    assert action["payload"]["source_email"]["email_id"] == email["id"]
    assert action["permissions"] == {
        "can_edit": True,
        "can_approve": True,
        "can_reject": True,
        "can_cancel": True,
        "can_execute": False,
        "can_reconcile": False,
    }
    match action["action_type"]:
        case "create_azure_devops_work_item":
            assert action["target_integration"] == "azure_devops"
            assert action["payload"]["project"] == "NUVEXA-Demo"
            assert action["payload"]["assigned_to"] is None  # never chosen by the model
            assert action["payload"]["work_item_type"] in ("Bug", "Task", "User Story")
        case "propose_email_reply":
            assert action["target_integration"] == "reply_drafts"
            assert action["payload"]["to"] == [email["sender_address"]]  # only ever the original sender
            assert "never sent" in " ".join(action["validation_warnings"])
        case "create_follow_up_task":
            assert action["target_integration"] == "internal_tasks"
        case "mark_no_action":
            assert action["target_integration"] == "none"
            assert action["payload"]["reason"]


async def _assert_dashboard_matches_db(h: Harness, p: Persona) -> None:
    summary = DashboardSummaryOut.model_validate((await p.get("/dashboard/summary")).json())
    async with h.db.session() as s:
        own_email = (EmailMessage.owner_user_id == p.user_id, EmailMessage.deleted_at.is_(None))
        total = (await s.execute(select(func.count()).select_from(EmailMessage).where(*own_email))).scalar_one()
        analyzed = (
            await s.execute(
                select(func.count())
                .select_from(EmailMessage)
                .where(
                    *own_email,
                    EmailMessage.analysis_status.in_([EmailAnalysisStatus.AWAITING_REVIEW, EmailAnalysisStatus.DONE]),
                )
            )
        ).scalar_one()
        statuses = Counter(
            (await s.execute(select(ProposedAction.status).where(ProposedAction.owner_user_id == p.user_id)))
            .scalars()
            .all()
        )
        runs = Counter(
            (await s.execute(select(WorkflowRun.status).where(WorkflowRun.owner_user_id == p.user_id))).scalars().all()
        )
        categories = Counter(
            (await s.execute(select(EmailMessage.category).where(*own_email, EmailMessage.category.is_not(None))))
            .scalars()
            .all()
        )
    assert summary.emails_total == total
    assert summary.emails_analyzed == analyzed
    assert summary.actions_proposed == sum(v for k, v in statuses.items() if k is not ActionStatus.SUPERSEDED)
    assert summary.pending_approvals == statuses[ActionStatus.PENDING_APPROVAL]
    assert summary.completed_actions == statuses[ActionStatus.COMPLETED]
    assert summary.failed_actions == statuses[ActionStatus.FAILED]
    assert summary.uncertain_actions == statuses[ActionStatus.UNCERTAIN]
    assert summary.rejected_actions == statuses[ActionStatus.REJECTED]
    assert summary.workflows_by_status == {k.value: v for k, v in runs.items()}
    assert summary.categories == {k.value: v for k, v in categories.items()}


# ---------------------------------------------------------------------------------------------------
# Every demo email: analysis → proposals → approve + execute every action → completed
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("provider_id", MESSAGE_IDS)
async def test_every_demo_email_completes_its_lifecycle(harness: Harness, provider_id: str) -> None:
    member, email_id, detail = await _start(harness, provider_id)
    WorkflowDetail.model_validate(detail)
    email = (await member.get(f"/emails/{email_id}")).json()
    run = detail["run"]
    assert run["status"] == "pending_approval", run
    assert (run["trigger"], run["analysis_attempt"], run["error_category"]) == ("manual", 1, None)
    analysis = AnalysisOut.model_validate(detail["analysis"])
    assert email["category"] == analysis.category.value
    assert email["priority"] == analysis.priority.value
    assert email["analysis_status"] == "awaiting_review"
    assert 0 <= analysis.confidence_score <= 1
    assert detail["actions"], "every email gets at least one proposal"
    for action in detail["actions"]:
        _check_proposal(action, email)
    types = [a["action_type"] for a in detail["actions"]]
    assert len(types) == len(set(types)), "one proposal per action type"
    if "mark_no_action" in types:
        assert types == ["mark_no_action"], "no-action is exclusive"

    # Timeline: every analysis step, three LLM calls, the planner's tool calls; never the email body.
    events = detail["events"]
    completed_steps = [e["step"] for e in events if e["event_type"] in ("step_completed", "llm_call")]
    assert completed_steps[:5] == ANALYSIS_STEPS
    assert sum(e["event_type"] == "llm_call" for e in events) == 3
    assert any(e["event_type"] == "tool_call" and e["status"] == "proposed" for e in events)
    body = (email["body_text"] or "").strip()
    if len(body) > 40:
        assert body[:40] not in json.dumps(events), "email content leaked into the timeline"

    # Approve and execute every proposal.
    for action in detail["actions"]:
        done = await approve_and_execute(harness, member, action)
        assert done["status"] == "completed", done
        assert [e["status"] for e in done["executions"]] == ["succeeded"]
        assert done["executed_at"] is not None
        assert done["current_approval"]["action_version"] == action["version"]
        if action["action_type"] == "create_azure_devops_work_item":
            assert done["external_url"].startswith("mock://azure-devops/NUVEXA-Demo/workitems/")
            [item] = harness.ado.clients[-1].items()
            assert item.fields[FIELD_TITLE] == action["payload"]["title"]
        if action["action_type"] == "propose_email_reply":
            [draft] = (await member.get("/reply-drafts")).json()["items"]
            assert (draft["action_id"], draft["subject"]) == (action["id"], action["payload"]["subject"])
        if action["action_type"] == "create_follow_up_task":
            [task] = (await member.get("/tasks")).json()["items"]
            assert (task["action_id"], task["title"], task["status"]) == (
                action["id"],
                action["payload"]["title"],
                "open",
            )

    final = (await member.get(f"/workflows/{run['id']}")).json()
    assert final["run"]["status"] == "completed"
    assert final["run"]["completed_at"] is not None
    assert final["run"]["metrics"]["approval_wait_seconds"] >= 0
    assert final["events"][-1]["step"] == "finalize"
    assert (await member.get(f"/emails/{email_id}")).json()["analysis_status"] == "done"
    await _assert_dashboard_matches_db(harness, member)

    # Audit trail: every state change of the run and of each action.
    run_events = [e.event_type for e in await harness.audit_events(workflow_run_id=uuid.UUID(run["id"]))]
    assert run_events[0] == "analysis_requested"
    assert run_events[1] == "actions_proposed"
    assert run_events[-1] == "workflow_finished"
    for action in detail["actions"]:
        kinds = [e.event_type for e in await harness.audit_events(action_id=uuid.UUID(action["id"]))]
        assert kinds == ["action_approved", "execute_requested", "execution_started", "external_write"], kinds


# ---------------------------------------------------------------------------------------------------
# Reject / mixed decisions / cancel / re-analysis
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("provider_id", [INCIDENT_ID, FEATURE_ID, FOLLOW_UP_ID, NEWSLETTER_ID])
async def test_rejecting_every_proposal_rejects_the_workflow(harness: Harness, provider_id: str) -> None:
    member, email_id, detail = await _start(harness, provider_id)
    for action in detail["actions"]:
        resp = await member.post(f"/actions/{action['id']}/reject", json={"reason": "Not ours"})
        assert resp.status_code == 200
        assert ActionDetail.model_validate(resp.json()).status is ActionStatus.REJECTED
        assert_error(await member.post(f"/actions/{action['id']}/execute", json=review_body(action)), 409)
    await harness.drain()
    final = (await member.get(f"/workflows/{detail['run']['id']}")).json()
    assert final["run"]["status"] == "rejected"
    assert (await member.get(f"/emails/{email_id}")).json()["analysis_status"] == "done"
    assert harness.ado.clients == []  # nothing was ever sent
    for action in detail["actions"]:
        [event] = await harness.audit_events(action_id=uuid.UUID(action["id"]), event_type="action_rejected")
        assert event.details["reason"] == "Not ours"
    await _assert_dashboard_matches_db(harness, member)


async def test_execute_one_reject_the_other_completes_the_workflow(harness: Harness) -> None:
    member, _, detail = await _start(harness, INCIDENT_ID)
    work_item = find_action(detail, "create_azure_devops_work_item")
    reply = find_action(detail, "propose_email_reply")
    assert (await member.post(f"/actions/{reply['id']}/reject", json={"reason": "no reply"})).status_code == 200
    await harness.drain()
    assert (await member.get(f"/workflows/{detail['run']['id']}")).json()["run"]["status"] == "pending_approval"
    assert (await approve_and_execute(harness, member, work_item))["status"] == "completed"
    assert (await member.get(f"/workflows/{detail['run']['id']}")).json()["run"]["status"] == "completed"
    assert (await member.get("/reply-drafts")).json()["total"] == 0


async def test_approved_but_not_executed_keeps_the_workflow_open(harness: Harness) -> None:
    member, _, detail = await _start(harness, FEATURE_ID)
    [action] = detail["actions"]
    assert (await member.post(f"/actions/{action['id']}/approve", json=review_body(action))).status_code == 200
    await harness.drain()
    run = (await member.get(f"/workflows/{detail['run']['id']}")).json()["run"]
    assert run["status"] == "approved"
    assert run["metrics"]["approval_wait_seconds"] >= 0


async def test_cancel_workflow_cancels_open_proposals_and_invalidates_approvals(harness: Harness) -> None:
    member, email_id, detail = await _start(harness, INCIDENT_ID)
    run_id = detail["run"]["id"]
    approved = find_action(detail, "create_azure_devops_work_item")
    assert (await member.post(f"/actions/{approved['id']}/approve", json=review_body(approved))).status_code == 200
    resp = await member.post(f"/workflows/{run_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    await harness.drain()  # the queued 'cancel' resume finalizes the graph
    final = (await member.get(f"/workflows/{run_id}")).json()
    assert final["run"]["status"] == "cancelled"
    assert {a["status"] for a in final["actions"]} == {"cancelled"}
    assert final["events"][-1]["step"] == "finalize"
    cancelled = (await member.get(f"/actions/{approved['id']}")).json()
    assert cancelled["current_approval"] is None
    assert cancelled["approvals"][0]["invalidated_reason"] == "workflow cancelled"
    assert_error(await member.post(f"/actions/{approved['id']}/execute", json=review_body(approved)), 409)
    [audit] = await harness.audit_events(event_type="workflow_cancelled")
    assert sorted(audit.details["cancelled_actions"]) == sorted(a["id"] for a in detail["actions"])
    # The email can be analyzed again; a fresh run is created.
    again = await member.post(f"/emails/{email_id}/analyze")
    assert again.status_code == 202
    assert again.json()["workflow_run"]["id"] != run_id
    await harness.drain()
    fresh = (await member.get(f"/workflows/{again.json()['workflow_run']['id']}")).json()
    assert fresh["run"]["status"] == "pending_approval"
    assert all(a["status"] == "pending_approval" for a in fresh["actions"])


async def test_cancelled_run_while_analysis_is_queued_halts_without_proposals(harness: Harness) -> None:
    member = await harness.persona("member")
    await sync_all(harness, member)
    email_id = await email_id_by_provider_id(harness, member.user_id, INCIDENT_ID)
    run_id = (await member.post(f"/emails/{email_id}/analyze")).json()["workflow_run"]["id"]
    assert (await member.post(f"/workflows/{run_id}/cancel")).status_code == 200
    await harness.drain()
    final = (await member.get(f"/workflows/{run_id}")).json()
    assert final["run"]["status"] == "cancelled"
    assert final["actions"] == []
    assert harness.engine is not None


async def test_reanalysis_supersedes_open_proposals(harness: Harness) -> None:
    member, _, detail = await _start(harness, INCIDENT_ID)
    run_id = detail["run"]["id"]
    approved = find_action(detail, "create_azure_devops_work_item")
    assert (await member.post(f"/actions/{approved['id']}/approve", json=review_body(approved))).status_code == 200
    assert (await member.post(f"/workflows/{run_id}/reanalyze")).status_code == 202
    await harness.drain()

    after = (await member.get(f"/workflows/{run_id}")).json()
    assert after["run"]["status"] == "pending_approval"
    assert after["run"]["analysis_attempt"] == 2
    assert after["analysis"]["attempt"] == 2
    old_ids = {a["id"] for a in detail["actions"]}
    old = [a for a in after["actions"] if a["id"] in old_ids]
    new = [a for a in after["actions"] if a["id"] not in old_ids]
    assert {a["status"] for a in old} == {"superseded"}
    assert {a["action_type"] for a in new} == {a["action_type"] for a in detail["actions"]}
    assert all(a["status"] == "pending_approval" for a in new)
    # Same email + type → same idempotency key, released by the superseded proposal and reused.
    assert {a["idempotency_key"] for a in new} == {a["idempotency_key"] for a in detail["actions"]}
    superseded = (await member.get(f"/actions/{approved['id']}")).json()
    assert superseded["current_approval"] is None
    assert superseded["approvals"][0]["invalidated_reason"] == "re-analysis requested"
    assert_error(await member.post(f"/actions/{approved['id']}/execute", json=review_body(approved)), 409)
    steps = [e["step"] for e in after["events"]]
    assert steps.count("prepare") == 2
    assert "reanalyze" in steps
    superseded_audits = await harness.audit_events(event_type="action_superseded", workflow_run_id=uuid.UUID(run_id))
    assert {str(e.action_id) for e in superseded_audits} == old_ids
    assert len(await harness.audit_events(event_type="reanalysis_requested")) == 1
    await _assert_dashboard_matches_db(harness, member)


async def test_reanalysis_is_refused_while_an_action_is_uncertain(harness: Harness) -> None:
    member, _, detail = await _start(harness, INCIDENT_ID)
    work_item = find_action(detail, "create_azure_devops_work_item")
    harness.ado.fail_mode = "lookup_unavailable_after_timeout"
    assert (await approve_and_execute(harness, member, work_item))["status"] == "uncertain"
    run = (await member.get(f"/workflows/{detail['run']['id']}")).json()["run"]
    assert run["status"] == "failed"
    err = assert_error(await member.post(f"/workflows/{run['id']}/reanalyze"), 409, "conflict")
    assert "uncertain" in err["message"]


async def test_reanalysis_after_completion_never_duplicates_the_work_item(harness: Harness) -> None:
    member, _, detail = await _start(harness, INCIDENT_ID)
    work_item = find_action(detail, "create_azure_devops_work_item")
    assert (await approve_and_execute(harness, member, work_item))["status"] == "completed"
    run_id = detail["run"]["id"]
    assert (await member.post(f"/workflows/{run_id}/reanalyze")).status_code == 202  # reply still pending
    await harness.drain()
    after = (await member.get(f"/workflows/{run_id}")).json()
    live_work_items = [
        a
        for a in after["actions"]
        if a["action_type"] == "create_azure_devops_work_item" and a["status"] != "superseded"
    ]
    assert [a["id"] for a in live_work_items] == [work_item["id"]]
    skipped = [e for e in after["events"] if e["status"] == "skipped" and e["step"] == "plan"]
    assert skipped == [] or skipped[0]["details"]["existing_action_id"] == work_item["id"]


# ---------------------------------------------------------------------------------------------------
# Edit → invalidation → re-approve → execute, for every action type
# ---------------------------------------------------------------------------------------------------

EDITS: dict[str, tuple[str, dict[str, Any]]] = {
    "create_azure_devops_work_item": (INCIDENT_ID, {"title": "Edited: login failures after 2.4", "priority": 1}),
    "propose_email_reply": (INCIDENT_ID, {"subject": "Re: we are on it", "body": "Edited reply body."}),
    "create_follow_up_task": (FOLLOW_UP_ID, {"title": "Edited follow-up", "description": "Chase the ERP team"}),
    "mark_no_action": (NEWSLETTER_ID, {"reason": "Edited: newsletter, nothing to do"}),
}


@pytest.mark.parametrize("action_type", sorted(EDITS))
async def test_edit_invalidates_approval_then_reapproval_executes_the_edited_payload(
    harness: Harness, action_type: str
) -> None:
    provider_id, changes = EDITS[action_type]
    member, email_id, detail = await _start(harness, provider_id)
    action = find_action(detail, action_type)
    assert (await member.post(f"/actions/{action['id']}/approve", json=review_body(action))).status_code == 200

    resp = await member.patch(
        f"/actions/{action['id']}",
        json={"expected_version": 1, "payload": {**action["payload"], **changes}},
    )
    assert resp.status_code == 200, resp.text
    edited = ActionDetail.model_validate(resp.json())
    assert (edited.version, edited.status, edited.source) == (2, ActionStatus.PENDING_APPROVAL, "user_edited")
    assert edited.current_approval is None
    assert edited.approvals[0].invalidated_at is not None
    for key, value in changes.items():
        assert edited.payload[key] == value
    assert edited.payload_hash == compute_payload_hash(action_type, action["target_integration"], edited.payload)
    # The approval of version 1 no longer authorizes anything.
    assert_error(await member.post(f"/actions/{action['id']}/execute", json=review_body(action)), 409, "stale_version")
    assert_error(await member.post(f"/actions/{action['id']}/approve", json=review_body(action)), 409, "stale_version")
    kinds = [e.event_type for e in await harness.audit_events(action_id=uuid.UUID(action["id"]))]
    assert kinds == ["action_approved", "action_edited", "approval_invalidated"]

    done = await approve_and_execute(harness, member, edited.model_dump(mode="json"))
    assert done["status"] == "completed"
    assert done["executions"][0]["action_version"] == 2
    match action_type:
        case "create_azure_devops_work_item":
            [item] = harness.ado.clients[-1].items()
            assert item.fields[FIELD_TITLE] == changes["title"]
            assert item.fields["Microsoft.VSTS.Common.Priority"] == 1
        case "propose_email_reply":
            [draft] = (await member.get("/reply-drafts")).json()["items"]
            assert (draft["subject"], draft["body"]) == (changes["subject"], changes["body"])
        case "create_follow_up_task":
            [task] = (await member.get("/tasks")).json()["items"]
            assert (task["title"], task["description"]) == (changes["title"], changes["description"])
        case "mark_no_action":
            assert (await member.get(f"/emails/{email_id}")).json()["analysis_status"] == "done"


async def test_edit_cannot_change_system_controlled_fields(harness: Harness) -> None:
    member, _, detail = await _start(harness, INCIDENT_ID)
    reply = find_action(detail, "propose_email_reply")
    forged = {
        **reply["payload"],
        "to": ["attacker@evil.example"],
        "source_email": {**reply["payload"]["source_email"], "email_id": str(uuid.uuid4())},
        "body": "Edited",
    }
    resp = await member.patch(f"/actions/{reply['id']}", json={"expected_version": 1, "payload": forged})
    assert resp.status_code == 200, resp.text
    edited = resp.json()
    assert edited["payload"]["to"] == reply["payload"]["to"]
    assert edited["payload"]["source_email"] == reply["payload"]["source_email"]
    assert edited["payload"]["body"] == "Edited"
    work_item = find_action(detail, "create_azure_devops_work_item")
    bad = {**work_item["payload"], "work_item_type": "Spike"}
    err = assert_error(
        await member.patch(f"/actions/{work_item['id']}", json={"expected_version": 1, "payload": bad}), 422
    )
    assert any("Spike" in d["msg"] for d in err["details"])


async def test_failed_execution_can_be_edited_reapproved_and_completed(harness: Harness) -> None:
    member, _, detail = await _start(harness, INCIDENT_ID)
    work_item = find_action(detail, "create_azure_devops_work_item")
    harness.ado.fail_mode = "permanent_validation"
    failed = await approve_and_execute(harness, member, work_item)
    assert failed["status"] == "failed"
    assert failed["last_error"]
    assert failed["permissions"]["can_edit"] is True
    assert failed["permissions"]["can_execute"] is True  # the approval is still valid for this version
    run = (await member.get(f"/workflows/{detail['run']['id']}")).json()["run"]
    assert run["status"] == "failed"
    harness.ado.fail_mode = "none"
    payload = {**work_item["payload"], "title": "Shorter title"}
    edited = (
        await member.patch(f"/actions/{work_item['id']}", json={"expected_version": 1, "payload": payload})
    ).json()
    assert (edited["status"], edited["version"], edited["last_error"]) == ("pending_approval", 2, None)
    done = await approve_and_execute(harness, member, edited)
    assert done["status"] == "completed"
    assert [(e["attempt_number"], e["status"], e["action_version"]) for e in done["executions"]] == [
        (1, "failed", 1),
        (2, "succeeded", 2),
    ]
    assert len(harness.ado.clients[-1].items()) == 1


async def test_reserved_additional_fields_are_refused_even_when_allow_listed(harness: Harness) -> None:
    """Regression (fixed in ActionPolicy.policy_issues): an allow-listed reserved field (System.AssignedTo) passed
    review, then made build_create_request() raise during execution, which was recorded as UNCERTAIN."""
    admin = await harness.persona("admin")
    current = (await admin.get("/settings")).json()
    ado = {**current["settings"]["azure_devops"], "allowed_additional_fields": ["System.AssignedTo"]}
    settings_body = {**current["settings"], "azure_devops": ado}
    assert (
        await admin.put("/settings", json={"expected_version": current["version"], "settings": settings_body})
    ).status_code == 200
    member, _, detail = await _start(harness, INCIDENT_ID)
    work_item = find_action(detail, "create_azure_devops_work_item")
    payload = {**work_item["payload"], "additional_fields": {"System.AssignedTo": "boss@example.com"}}
    resp = await member.patch(f"/actions/{work_item['id']}", json={"expected_version": 1, "payload": payload})
    err = assert_error(resp, 422, "validation_failed")
    assert any("System.AssignedTo" in d["msg"] for d in err["details"])
    assert (await member.get(f"/actions/{work_item['id']}")).json()["version"] == 1
    assert harness.ado.clients == []


# ---------------------------------------------------------------------------------------------------
# Tasks, auto-analysis, audit completeness
# ---------------------------------------------------------------------------------------------------


async def test_task_status_updates_are_audited(harness: Harness) -> None:
    member, _, detail = await _start(harness, FOLLOW_UP_ID)
    await approve_and_execute(harness, member, find_action(detail, "create_follow_up_task"))
    [task] = (await member.get("/tasks")).json()["items"]
    for status in ("done", "dismissed", "open", "done"):
        resp = await member.patch(f"/tasks/{task['id']}", json={"status": status})
        assert resp.status_code == 200
        assert resp.json()["status"] == status
        assert (resp.json()["completed_at"] is None) is (status == "open")
        listed = (await member.get("/tasks", params={"status": status})).json()["items"]
        assert [t["id"] for t in listed] == [task["id"]]
    events = await harness.audit_events(event_type="task_updated", resource_id=task["id"])
    assert [e.details["status"] for e in events] == ["done", "dismissed", "open", "done"]
    assert {e.actor_user_id for e in events} == {member.user_id}
    approver = await harness.persona("approver")  # approve_any may update others' tasks
    assert (await approver.patch(f"/tasks/{task['id']}", json={"status": "open"})).status_code == 200


async def test_auto_analysis_of_new_emails(harness: Harness) -> None:
    admin = await harness.persona("admin")
    current = (await admin.get("/settings")).json()
    body = {
        "expected_version": current["version"],
        "settings": {**current["settings"], "auto_analyze_new_emails": True},
    }
    assert (await admin.put("/settings", json=body)).status_code == 200
    member = await harness.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await harness.drain()
    runs = (await member.get("/workflows", params={"limit": 100})).json()
    assert runs["total"] == 10  # first demo round
    assert {r["trigger"] for r in runs["items"]} == {"auto"}
    assert {r["status"] for r in runs["items"]} == {"pending_approval"}
    proposed = await harness.audit_events(event_type="actions_proposed")
    assert len(proposed) == 10
    assert {e.actor_type.value for e in proposed} == {"agent"}


async def test_every_state_change_has_an_audit_event(harness: Harness) -> None:
    member, email_id, detail = await _start(harness, INCIDENT_ID)
    run_id = detail["run"]["id"]
    work_item = find_action(detail, "create_azure_devops_work_item")
    reply = find_action(detail, "propose_email_reply")
    payload = {**work_item["payload"], "title": "Audited edit"}
    edited = (
        await member.patch(f"/actions/{work_item['id']}", json={"expected_version": 1, "payload": payload})
    ).json()
    await approve_and_execute(harness, member, edited)
    await member.post(f"/actions/{reply['id']}/cancel", json={"reason": "not needed"})
    await harness.drain()
    viewer = await harness.persona("viewer")
    await viewer.post("/emails/sync", json={})
    admin = await harness.persona("admin")
    settings = (await admin.get("/settings")).json()
    await admin.put("/settings", json={"expected_version": settings["version"], "settings": settings["settings"]})

    expected = {
        ("login", "user", str(member.user_id)),
        ("mailbox_sync_requested", "mailbox", None),
        ("mailbox_synced", "connected_account", None),
        ("analysis_requested", "email", str(email_id)),
        ("actions_proposed", "workflow_run", run_id),
        ("action_edited", "action", work_item["id"]),
        ("action_approved", "action", work_item["id"]),
        ("execute_requested", "action", work_item["id"]),
        ("execution_started", "action", work_item["id"]),
        ("external_write", "action", work_item["id"]),
        ("action_cancelled", "action", reply["id"]),
        ("workflow_finished", "workflow_run", run_id),
        ("permission_denied", "mailbox", None),
        ("settings_updated", "settings", None),
    }
    events = await harness.audit_events()
    present = {(e.event_type, e.resource_type, e.resource_id) for e in events}
    for event_type, resource_type, resource_id in expected:
        assert any(
            (t, r) == (event_type, resource_type) and (resource_id is None or i == resource_id) for t, r, i in present
        ), f"missing audit event {event_type} for {resource_type}:{resource_id}"
    assert all(e.correlation_id for e in events if e.actor_type.value == "user")
    edit_event = next(e for e in events if e.event_type == "action_edited")
    assert {"from_version": 1, "to_version": 2, "changed_fields": ["title"]}.items() <= edit_event.details.items()
    write = next(e for e in events if e.event_type == "external_write")
    assert (write.outcome.value, write.details["result"], write.details["deduplicated"]) == (
        "success",
        "created",
        False,
    )
    final = (await member.get(f"/workflows/{run_id}")).json()["run"]
    assert final["status"] == "completed"  # completed + cancelled → completed
