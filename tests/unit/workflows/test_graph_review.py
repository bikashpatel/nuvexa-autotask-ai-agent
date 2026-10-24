"""Planning details, the review loop, execution/reconcile nodes, re-analysis and finalize."""

from __future__ import annotations

import uuid

import pytest

from app.core.errors import InvalidStateTransition, StaleVersion
from app.db.models import ProposedAction, WorkflowRun
from app.domain.enums import (
    ActionStatus,
    ActionType,
    AuditOutcome,
    EmailAnalysisStatus,
    WorkflowEventType,
    WorkflowStatus,
)
from app.services.approval_service import ApprovalService
from app.services.authz import AuditedPermissionDenied
from app.services.identity import actor_from_user
from app.workflows.engine import WorkflowEngine
from tests.unit.workflows.conftest import WF
from tests.unit.workflows.fakes import (
    get_actions,
    get_analyses,
    get_audits,
    get_email,
    get_events,
    get_run,
    set_action_status,
    set_run_fields,
    tool_call,
)

E = WorkflowEventType
WI = ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM


async def _reviewing(wf: WF, *types: ActionType, **email_kwargs: object) -> tuple[WorkflowRun, list[ProposedAction]]:
    """Start a run whose planner proposes exactly the given action types; returns the run and its actions."""
    wf.agent.tool_calls = [tool_call(t) for t in (types or (WI,))]
    run = await wf.new_run(**email_kwargs)
    await wf.start(run)
    actions = await get_actions(wf.db, run.id)
    assert [a.action_type for a in actions] == list(types or (WI,))
    return run, actions


async def _approve(wf: WF, action: ProposedAction) -> None:
    async with wf.db.session() as s:
        await ApprovalService(wf.settings).approve(
            s, wf.actor(), action.id, version=action.version, payload_hash=action.payload_hash, comment=None
        )


async def _reject(wf: WF, action: ProposedAction) -> None:
    async with wf.db.session() as s:
        await ApprovalService(wf.settings).reject(s, wf.actor(), action.id, reason="not needed")


async def _cancel_action(wf: WF, action: ProposedAction) -> None:
    async with wf.db.session() as s:
        await ApprovalService(wf.settings).cancel(s, wf.actor(), action.id, reason=None)


# --------------------------------------------------------------------------- plan


async def test_tool_call_arguments_are_sanitized_in_the_timeline(wf: WF) -> None:
    long_title = "T" * 200
    wf.agent.tool_calls = [tool_call(WI, title=long_title, description="SECRET free text from the email body")]
    run = await wf.new_run()
    await wf.start(run)
    (event,) = [e for e in await get_events(wf.db, run.id) if e.event_type is E.TOOL_CALL]
    assert event.status == "proposed"
    assert event.details["tool"] == "propose_azure_devops_work_item"
    args = event.details["arguments"]
    assert args["title"] == "T" * 120
    assert args["work_item_type"] == "Bug"
    assert args["priority"] == "high"
    assert args["tags"] == ["login", "production"]
    assert args["argument_keys"] == sorted(
        ["title", "description", "work_item_type", "priority", "tags", "rationale", "expected_impact"]
    )
    assert "description" not in args
    assert "rationale" not in args
    assert "SECRET" not in str(event.details)
    assert "nothing executed" in (event.message or "")


async def test_rejected_tool_calls_are_recorded(wf: WF) -> None:
    wf.agent.tool_calls = [tool_call(ActionType.MARK_NO_ACTION)]
    wf.agent.rejected_calls = [{"tool": "delete_mailbox", "reason": "unknown tool"}]
    run = await wf.new_run()
    await wf.start(run)
    rejected = [e for e in await get_events(wf.db, run.id) if e.status == "rejected"]
    assert len(rejected) == 1
    assert rejected[0].event_type is E.TOOL_CALL
    assert rejected[0].details == {"tool": "delete_mailbox", "reason": "unknown tool"}
    plan_call = next(e for e in await get_events(wf.db, run.id) if e.step == "plan" and e.event_type is E.LLM_CALL)
    assert plan_call.message == "Planner proposed 1 action(s)."


async def test_plan_rerun_after_crash_reuses_its_actions(wf: WF) -> None:
    run, actions = await _reviewing(wf, WI, ActionType.PROPOSE_EMAIL_REPLY)
    config = WorkflowEngine._config(run.id)
    # Simulate a crash after plan committed but before its checkpoint was written: the run is still
    # action_proposed in the database and the graph's next node is plan.
    await set_run_fields(wf.db, run.id, status=WorkflowStatus.ACTION_PROPOSED, awaiting_review_since=None)
    await wf.engine.graph.aupdate_state(config, None, as_node="validate")
    assert (await wf.snapshot(run)).next == ("plan",)

    await wf.start(run)

    again = await get_actions(wf.db, run.id)
    assert [a.id for a in again] == [a.id for a in actions]
    assert all(a.status is ActionStatus.PENDING_APPROVAL for a in again)
    assert not [e for e in await get_events(wf.db, run.id) if e.status == "skipped"]
    audits = await get_audits(wf.db, wf.tenant, "actions_proposed")
    assert [a.details["action_ids"] for a in audits] == [[str(a.id) for a in actions]] * 2
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.PENDING_APPROVAL
    assert wf.agent.calls.count("plan") == 2


async def test_duplicate_is_prevented_when_another_run_holds_the_key(wf: WF) -> None:
    run1, (wi1,) = await _reviewing(wf, WI)
    await set_action_status(wf.db, wi1.id, status=ActionStatus.COMPLETED, external_id="1001")
    await set_run_fields(wf.db, run1.id, status=WorkflowStatus.COMPLETED)

    from tests.unit.workflows.fakes import add_run

    run2 = await add_run(wf.db, await get_email(wf.db, run1.email_message_id))
    wf.agent.tool_calls = [tool_call(WI), tool_call(ActionType.CREATE_FOLLOW_UP_TASK)]
    await wf.start(run2)

    context = wf.agent.contexts[-1]
    assert context.already_completed_actions == [f"create_azure_devops_work_item: {wi1.title}"]
    actions2 = await get_actions(wf.db, run2.id)
    assert [a.action_type for a in actions2] == [ActionType.CREATE_FOLLOW_UP_TASK]
    (skipped,) = [e for e in await get_events(wf.db, run2.id) if e.status == "skipped"]
    assert skipped.step == "plan"
    assert skipped.event_type is E.STEP_COMPLETED
    assert skipped.details == {
        "existing_action_id": str(wi1.id),
        "action_type": "create_azure_devops_work_item",
        "external_id": "1001",
    }
    assert "(completed)" in (skipped.message or "")
    (audit,) = [a for a in await get_audits(wf.db, wf.tenant, "actions_proposed") if a.workflow_run_id == run2.id]
    assert audit.details["action_ids"] == [str(actions2[0].id)]


async def test_all_proposals_duplicate_completes_the_run(wf: WF) -> None:
    run1, (wi1,) = await _reviewing(wf, WI)
    await set_action_status(wf.db, wi1.id, status=ActionStatus.COMPLETED)
    await set_run_fields(wf.db, run1.id, status=WorkflowStatus.COMPLETED)
    from tests.unit.workflows.fakes import add_run

    run2 = await add_run(wf.db, await get_email(wf.db, run1.email_message_id))
    await wf.start(run2)
    done = await get_run(wf.db, run2.id)
    assert done.status is WorkflowStatus.COMPLETED
    assert done.completed_at is not None
    assert await get_actions(wf.db, run2.id) == []
    assert (await get_email(wf.db, run2.email_message_id)).analysis_status is EmailAnalysisStatus.DONE


async def test_thread_duplicate_warning_for_work_items_only(wf: WF) -> None:
    first, (wi1,) = await _reviewing(wf, WI, conversation_id="conv-1", subject="Login outage")
    await set_action_status(wf.db, wi1.id, external_id="1001")
    _, actions = await _reviewing(wf, WI, ActionType.CREATE_FOLLOW_UP_TASK, conversation_id="conv-1")
    wi2, follow_up = actions
    (warning,) = [w for w in wi2.validation_warnings if w.startswith("Possible duplicate")]
    assert f"work item '{wi1.title}' (#1001) (pending_approval)" in warning
    assert "from email 'Login outage'" in warning
    assert not [w for w in follow_up.validation_warnings if w.startswith("Possible duplicate")]
    assert first.id != wi2.workflow_run_id


async def test_thread_duplicate_warning_ignores_released_and_other_threads(wf: WF) -> None:
    _, (wi1,) = await _reviewing(wf, WI, conversation_id="conv-2")
    await set_action_status(wf.db, wi1.id, status=ActionStatus.REJECTED)
    _, (wi_same,) = await _reviewing(wf, WI, conversation_id="conv-2")
    assert not [w for w in wi_same.validation_warnings if w.startswith("Possible duplicate")]
    _, (wi_none,) = await _reviewing(wf, WI, conversation_id=None)
    assert not [w for w in wi_none.validation_warnings if w.startswith("Possible duplicate")]


async def test_policy_errors_are_stored_on_the_proposal(wf: WF) -> None:
    wf.agent.tool_calls = [tool_call(WI, work_item_type="Spaceship")]
    run = await wf.new_run()
    await wf.start(run)
    (action,) = await get_actions(wf.db, run.id)
    assert action.payload["work_item_type"] == "Bug"  # fallback from category mapping
    assert any("not allowed" in w for w in action.validation_warnings)


# --------------------------------------------------------------------------- review gate / wait


async def test_resume_event_records_only_safe_decision_fields(wf: WF) -> None:
    run, (wi,) = await _reviewing(wf, WI)
    await wf.resume(run, wf.decision("refresh", wi))
    (resume,) = [e for e in await get_events(wf.db, run.id) if e.event_type is E.RESUME]
    assert resume.step == "review"
    assert resume.message == "Resumed: refresh"
    assert resume.details == {"type": "refresh", "action_id": str(wi.id), "version": 1}
    assert (await wf.snapshot(run)).interrupts  # waiting again
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.PENDING_APPROVAL


@pytest.mark.parametrize("decision", [{"actor_user_id": "x"}, {"type": "bogus"}])
async def test_unknown_decisions_route_back_to_review(wf: WF, decision: dict[str, str]) -> None:
    run, _ = await _reviewing(wf, WI)
    await wf.resume(run, decision)
    (resume,) = [e for e in await get_events(wf.db, run.id) if e.event_type is E.RESUME]
    assert resume.message == f"Resumed: {decision.get('type', 'unknown')}"
    assert (await wf.snapshot(run)).interrupts
    assert wf.execution.calls == []


async def test_approval_moves_run_to_approved_and_records_wait_metric(wf: WF) -> None:
    run, (wi,) = await _reviewing(wf, WI)
    pending = await get_run(wf.db, run.id)
    assert pending.awaiting_review_since is not None
    assert "approval_wait_seconds" not in pending.metrics

    await _approve(wf, wi)
    await wf.resume(run, wf.decision("refresh"))

    approved = await get_run(wf.db, run.id)
    assert approved.status is WorkflowStatus.APPROVED
    assert approved.current_step == "review"
    wait = approved.metrics["approval_wait_seconds"]
    assert isinstance(wait, float)
    assert wait >= 0
    assert approved.metrics["llm_calls"] == 3  # other metrics preserved
    assert approved.awaiting_review_since == pending.awaiting_review_since

    await wf.resume(run, wf.decision("refresh"))  # the metric is recorded once
    assert (await get_run(wf.db, run.id)).metrics["approval_wait_seconds"] == wait


async def test_partial_approval_stays_pending(wf: WF) -> None:
    run, (wi, _reply) = await _reviewing(wf, WI, ActionType.PROPOSE_EMAIL_REPLY)
    await _approve(wf, wi)
    await wf.resume(run, wf.decision("refresh"))
    still = await get_run(wf.db, run.id)
    assert still.status is WorkflowStatus.PENDING_APPROVAL
    assert "approval_wait_seconds" not in still.metrics
    assert (await get_email(wf.db, run.email_message_id)).analysis_status is EmailAnalysisStatus.AWAITING_REVIEW


async def test_rejecting_everything_finishes_rejected(wf: WF) -> None:
    run, actions = await _reviewing(wf, WI, ActionType.PROPOSE_EMAIL_REPLY)
    for action in actions:
        await _reject(wf, action)
    await wf.resume(run, wf.decision("refresh"))
    done = await get_run(wf.db, run.id)
    assert done.status is WorkflowStatus.REJECTED
    assert done.current_step == "finalize"
    assert done.completed_at is not None
    assert (await get_email(wf.db, run.email_message_id)).analysis_status is EmailAnalysisStatus.DONE
    (finished,) = await get_audits(wf.db, wf.tenant, "workflow_finished")
    assert finished.details == {"status": "rejected"}
    assert finished.outcome is AuditOutcome.SUCCESS
    snap = await wf.snapshot(run)
    assert snap.next == ()


async def test_cancelled_actions_finish_cancelled(wf: WF) -> None:
    run, (wi,) = await _reviewing(wf, WI)
    await _cancel_action(wf, wi)
    await wf.resume(run, wf.decision("refresh"))
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.CANCELLED


async def test_cancel_decision_finalizes_a_cancelled_run(wf: WF) -> None:
    run, _ = await _reviewing(wf, WI)
    await set_run_fields(wf.db, run.id, status=WorkflowStatus.CANCELLED)
    await wf.resume(run, {"type": "cancel"})
    done = await get_run(wf.db, run.id)
    assert done.status is WorkflowStatus.CANCELLED
    assert done.current_step == "finalize"
    assert done.completed_at is not None
    assert (await wf.snapshot(run)).next == ()


async def test_cancel_decision_on_open_run_cancels_it(wf: WF) -> None:
    run, _ = await _reviewing(wf, WI)
    await wf.resume(run, {"type": "cancel"})
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.CANCELLED


async def test_refresh_after_cancellation_finalizes(wf: WF) -> None:
    run, _ = await _reviewing(wf, WI)
    await set_run_fields(wf.db, run.id, status=WorkflowStatus.CANCELLED)
    await wf.resume(run, {"type": "refresh"})
    done = await get_run(wf.db, run.id)
    assert (done.status, done.current_step) == (WorkflowStatus.CANCELLED, "finalize")


# --------------------------------------------------------------------------- execute / reconcile


async def test_execute_node_with_fake_service(wf: WF) -> None:
    run, (wi,) = await _reviewing(wf, WI)
    await wf.resume(run, wf.decision("execute", wi))
    assert wf.execution.calls == [("execute", wf.owner.id, wi.id, 1, wi.payload_hash)]
    (event,) = [e for e in await get_events(wf.db, run.id) if e.event_type is E.INTEGRATION_CALL]
    assert (event.step, event.status) == ("execute", "succeeded")
    assert event.message == "Execute finished: succeeded."
    assert event.details["action_id"] == str(wi.id)
    assert event.details["operation"] == "execute"
    assert event.details["external_id"] == "4242"
    assert event.details["retry_count"] == 1
    assert event.details["deduplicated"] is False
    assert event.details["error_category"] is None
    assert uuid.UUID(event.details["execution_id"])
    assert event.latency_ms is not None
    # The fake changed nothing, so the gate derives pending_approval again from the action.
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.PENDING_APPROVAL


async def test_reconcile_node_with_fake_service(wf: WF) -> None:
    from app.domain.enums import ExecutionStatus

    run, (wi,) = await _reviewing(wf, WI)
    wf.execution.status = ExecutionStatus.FAILED
    await wf.resume(run, wf.decision("reconcile", wi))
    assert wf.execution.calls == [("reconcile", wf.owner.id, wi.id)]
    (event,) = [e for e in await get_events(wf.db, run.id) if e.event_type is E.INTEGRATION_CALL]
    assert (event.step, event.status, event.details["error_category"]) == ("reconcile", "failed", "transient")
    assert event.message == "Reconcile finished: failed."


async def test_execute_permission_denied_is_audited(wf: WF) -> None:
    run, (wi,) = await _reviewing(wf, WI)
    viewer = actor_from_user(wf.viewer)
    wf.execution.errors["execute"] = AuditedPermissionDenied(
        "Missing permission 'action:execute'.", actor=viewer, resource_type="action", resource_id=str(wi.id)
    )
    await wf.resume(run, wf.decision("execute", wi, user=wf.viewer))
    (event,) = [e for e in await get_events(wf.db, run.id) if e.event_type is E.INTEGRATION_CALL]
    assert event.status == "failure"
    assert event.message == "Execute denied: Missing permission 'action:execute'."
    (denied,) = await get_audits(wf.db, wf.tenant, "permission_denied")
    assert denied.actor_user_id == wf.viewer.id
    assert denied.outcome is AuditOutcome.DENIED
    assert denied.action_id == wi.id
    run_after = await get_run(wf.db, run.id)
    assert run_after.status is WorkflowStatus.PENDING_APPROVAL


async def test_execute_app_error_is_recorded_not_raised(wf: WF) -> None:
    run, (wi,) = await _reviewing(wf, WI)
    wf.execution.errors["execute"] = StaleVersion()
    await wf.resume(run, wf.decision("execute", wi))
    (event,) = [e for e in await get_events(wf.db, run.id) if e.event_type is E.INTEGRATION_CALL]
    assert event.status == "failure"
    assert event.message is not None
    assert event.message.startswith("Execute not performed: The resource was modified.")
    assert event.details["error_code"] == "stale_version"
    assert (await wf.snapshot(run)).interrupts


async def test_reconcile_with_unknown_actor_is_not_performed(wf: WF) -> None:
    run, (wi,) = await _reviewing(wf, WI)
    decision = wf.decision("reconcile", wi)
    decision["actor_user_id"] = str(uuid.uuid4())
    await wf.resume(run, decision)
    (event,) = [e for e in await get_events(wf.db, run.id) if e.event_type is E.INTEGRATION_CALL]
    assert event.details["error_code"] == "authentication_required"
    assert event.message is not None
    assert event.message.startswith("Reconcile not performed:")
    assert wf.execution.calls == []


async def test_real_execution_completes_and_finishes_the_run(wf: WF) -> None:
    wf.use_real_execution()
    run, (wi, _reply) = await _reviewing(wf, WI, ActionType.PROPOSE_EMAIL_REPLY)
    await _approve(wf, wi)
    await wf.resume(run, wf.decision("refresh"))
    approved = (await get_actions(wf.db, run.id))[0]
    await wf.resume(run, wf.decision("execute", approved))

    wi_after, reply_after = await get_actions(wf.db, run.id)
    assert wi_after.status is ActionStatus.COMPLETED
    assert wi_after.external_id is not None
    assert len(wf.ado_clients) == 1
    assert len(wf.ado_clients[0].items()) == 1
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.PENDING_APPROVAL  # reply still open
    (event,) = [e for e in await get_events(wf.db, run.id) if e.event_type is E.INTEGRATION_CALL]
    assert event.status == "succeeded"
    assert event.details["external_id"] == wi_after.external_id

    await _reject(wf, reply_after)
    await wf.resume(run, wf.decision("refresh"))
    done = await get_run(wf.db, run.id)
    assert done.status is WorkflowStatus.COMPLETED
    assert (await get_audits(wf.db, wf.tenant, "workflow_finished"))[0].details == {"status": "completed"}


async def test_real_execution_of_unapproved_action_is_denied_and_audited(wf: WF) -> None:
    wf.use_real_execution()
    run, (wi,) = await _reviewing(wf, WI)
    await wf.resume(run, wf.decision("execute", wi))  # forged resume: no approval exists
    (event,) = [e for e in await get_events(wf.db, run.id) if e.event_type is E.INTEGRATION_CALL]
    assert event.message == "Execute denied: The action has not been approved."
    assert await get_audits(wf.db, wf.tenant, "permission_denied")
    assert not wf.ado_clients
    assert (await get_actions(wf.db, run.id))[0].status is ActionStatus.PENDING_APPROVAL
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.PENDING_APPROVAL


async def test_stale_execute_for_cancelled_action_does_not_wedge_the_run(wf: WF) -> None:
    # Regression: the run was moved to `executing` before the call; when nothing was executed and every
    # action had been cancelled meanwhile, finalize tried executing → cancelled (not allowed) and the run
    # stayed `executing` forever (every later job re-ran finalize and failed).
    wf.use_real_execution()
    run, (wi,) = await _reviewing(wf, WI)
    await _approve(wf, wi)
    await wf.resume(run, wf.decision("refresh"))
    approved = (await get_actions(wf.db, run.id))[0]
    execute = wf.decision("execute", approved)  # queued by the user …
    await _cancel_action(wf, approved)  # … then the action was cancelled before the job ran

    await wf.resume(run, execute)

    done = await get_run(wf.db, run.id)
    assert done.status is WorkflowStatus.CANCELLED
    assert done.current_step == "finalize"
    assert (await wf.snapshot(run)).next == ()
    (event,) = [e for e in await get_events(wf.db, run.id) if e.event_type is E.INTEGRATION_CALL]
    assert event.details["error_code"] == "invalid_state_transition"
    assert (await get_audits(wf.db, wf.tenant, "execution_blocked"))[0].action_id == approved.id


async def test_failed_execution_returns_run_to_review_as_failed(wf: WF) -> None:
    wf.use_real_execution(fail_mode="permanent_validation")
    run, (wi,) = await _reviewing(wf, WI)
    await _approve(wf, wi)
    await wf.resume(run, wf.decision("refresh"))
    approved = (await get_actions(wf.db, run.id))[0]
    await wf.resume(run, wf.decision("execute", approved))
    assert (await get_actions(wf.db, run.id))[0].status is ActionStatus.FAILED
    failed = await get_run(wf.db, run.id)
    assert failed.status is WorkflowStatus.FAILED
    assert failed.error_category is None  # action-level failure; the run itself did not error
    assert (await wf.snapshot(run)).interrupts  # still waiting: a retry is possible


# --------------------------------------------------------------------------- re-analysis


async def test_reanalyze_supersedes_open_actions_and_runs_a_new_attempt(wf: WF) -> None:
    run, (wi, reply) = await _reviewing(wf, WI, ActionType.PROPOSE_EMAIL_REPLY)
    await _approve(wf, wi)
    await wf.resume(run, wf.decision("refresh"))

    wf.agent.tool_calls = [tool_call(WI)]
    await wf.resume(run, wf.decision("reanalyze"))

    actions = await get_actions(wf.db, run.id)
    old = [a for a in actions if a.analysis_attempt == 1]
    new = [a for a in actions if a.analysis_attempt == 2]
    assert {a.status for a in old} == {ActionStatus.SUPERSEDED}
    assert len(new) == 1
    assert new[0].status is ActionStatus.PENDING_APPROVAL
    assert new[0].idempotency_key == wi.idempotency_key  # the released key is reused
    started = next(e for e in await get_events(wf.db, run.id) if e.step == "reanalyze")
    assert started.event_type is E.STEP_STARTED
    assert sorted(started.details["superseded_action_ids"]) == sorted([str(wi.id), str(reply.id)])
    assert len(await get_audits(wf.db, wf.tenant, "action_superseded")) == 2
    assert [a.attempt for a in await get_analyses(wf.db, run.id)] == [1, 2]
    again = await get_run(wf.db, run.id)
    assert again.status is WorkflowStatus.PENDING_APPROVAL
    assert again.analysis_attempt == 2
    assert again.metrics["llm_calls"] == 6
    assert wf.agent.calls == ["classify", "extract", "plan"] * 2


async def test_reanalyze_never_duplicates_completed_work(wf: WF) -> None:
    run, (wi,) = await _reviewing(wf, WI)
    await set_action_status(wf.db, wi.id, status=ActionStatus.COMPLETED, external_id="77")
    await wf.resume(run, wf.decision("reanalyze"))
    assert [a.analysis_attempt for a in await get_actions(wf.db, run.id)] == [1]
    (skipped,) = [e for e in await get_events(wf.db, run.id) if e.status == "skipped"]
    assert skipped.details["external_id"] == "77"
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.COMPLETED


@pytest.mark.parametrize("busy", [ActionStatus.EXECUTING, ActionStatus.UNCERTAIN])
async def test_reanalyze_skipped_while_an_action_is_busy(wf: WF, busy: ActionStatus) -> None:
    run, (wi,) = await _reviewing(wf, WI)
    await set_action_status(wf.db, wi.id, status=busy)
    await wf.resume(run, wf.decision("reanalyze"))
    (skipped,) = [e for e in await get_events(wf.db, run.id) if e.step == "reanalyze"]
    assert (skipped.event_type, skipped.status) == (E.STEP_FAILED, "skipped")
    assert (await get_actions(wf.db, run.id))[0].status is busy
    assert wf.agent.calls == ["classify", "extract", "plan"]
    expected = WorkflowStatus.EXECUTING if busy is ActionStatus.EXECUTING else WorkflowStatus.FAILED
    assert (await get_run(wf.db, run.id)).status is expected
    snap = await wf.snapshot(run)
    assert snap.interrupts
    assert snap.values["halt"] is True


async def test_reanalyze_of_cancelled_run_is_skipped_and_finalized(wf: WF) -> None:
    run, _ = await _reviewing(wf, WI)
    await set_run_fields(wf.db, run.id, status=WorkflowStatus.CANCELLED)
    await wf.resume(run, wf.decision("reanalyze"))
    assert [e.status for e in await get_events(wf.db, run.id) if e.step == "reanalyze"] == ["skipped"]
    done = await get_run(wf.db, run.id)
    assert (done.status, done.current_step) == (WorkflowStatus.CANCELLED, "finalize")


async def test_finalize_raises_for_unknown_run(wf: WF) -> None:
    from app.core.errors import NotFound

    run, _ = await _reviewing(wf, WI)
    async with wf.db.session() as s:
        row = await s.get(WorkflowRun, run.id)
        await s.delete(row)
        await s.commit()
    with pytest.raises(NotFound, match="Workflow run not found"):
        await wf.resume(run, {"type": "cancel"})


async def test_invalid_transition_in_gate_propagates(wf: WF) -> None:
    run, (wi,) = await _reviewing(wf, WI)
    # A completed run cannot be moved back by a stray refresh.
    await set_action_status(wf.db, wi.id, status=ActionStatus.APPROVED)
    await set_run_fields(wf.db, run.id, status=WorkflowStatus.COMPLETED)
    with pytest.raises(InvalidStateTransition):
        await wf.resume(run, {"type": "refresh"})
