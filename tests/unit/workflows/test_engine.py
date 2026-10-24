"""WorkflowEngine: start/resume decision logic, the non-graph fallback and run-status resync."""

from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.base import AgentProviderError
from app.core.errors import StaleVersion
from app.db.models import ProposedAction, WorkflowRun
from app.domain.enums import ActionStatus, ActionType, AuditOutcome, WorkflowEventType, WorkflowStatus
from app.services.approval_service import ApprovalService
from app.services.authz import AuditedPermissionDenied
from app.services.identity import actor_from_user
from app.workflows.engine import WorkflowEngine
from tests.unit.workflows.conftest import WF
from tests.unit.workflows.fakes import (
    add_run,
    get_actions,
    get_audits,
    get_events,
    get_run,
    set_action_status,
    set_run_fields,
    tool_call,
)

E = WorkflowEventType
WI = ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM


def _fresh_engine(wf: WF) -> WorkflowEngine:
    """Same deps, empty checkpointer — as if the checkpoint store was lost."""
    return WorkflowEngine(wf.engine.deps, InMemorySaver())


def test_config_uses_run_id_as_thread() -> None:
    run_id = uuid.uuid4()
    assert WorkflowEngine._config(run_id) == {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 100}


async def test_start_fresh_runs_until_review(wf: WF) -> None:
    run = await wf.new_run()
    await wf.start(run)
    snap = await wf.snapshot(run)
    assert snap.values["run_id"] == str(run.id)
    assert snap.values["tenant_id"] == str(run.tenant_id)
    assert snap.interrupts


async def test_start_when_already_waiting_is_a_noop(wf: WF) -> None:
    run = await wf.new_run()
    await wf.start(run)
    events_before = len(await get_events(wf.db, run.id))
    await wf.start(run)  # job re-delivered
    assert wf.agent.calls == ["classify", "extract", "plan"]
    assert len(await get_events(wf.db, run.id)) == events_before


async def test_start_when_finished_is_a_noop(wf: WF) -> None:
    run = await wf.new_run()
    await set_run_fields(wf.db, run.id, status=WorkflowStatus.CANCELLED)
    await wf.start(run)  # halts and finalizes
    events_before = len(await get_events(wf.db, run.id))
    snap = await wf.snapshot(run)
    assert snap.values
    assert not snap.next
    await wf.start(run)
    assert len(await get_events(wf.db, run.id)) == events_before


async def test_start_with_pending_step_retries_it(wf: WF) -> None:
    run = await wf.new_run()
    wf.agent.fail("classify", AgentProviderError("down"))
    with pytest.raises(AgentProviderError):
        await wf.start(run)
    assert (await wf.snapshot(run)).next == ("classify",)
    await wf.start(run)
    assert wf.agent.calls == ["classify", "classify", "extract", "plan"]
    assert (await wf.snapshot(run)).interrupts


@pytest.mark.parametrize("kind", ["refresh", "reanalyze"])
async def test_resume_with_pending_step_reruns_it(wf: WF, kind: str) -> None:
    run = await wf.new_run()
    wf.agent.fail("plan", AgentProviderError("down"))
    with pytest.raises(AgentProviderError):
        await wf.start(run)
    await wf.resume(run, wf.decision(kind))
    assert wf.agent.calls == ["classify", "extract", "plan", "plan"]
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.PENDING_APPROVAL
    assert not [e for e in await get_events(wf.db, run.id) if e.event_type is E.RESUME]


async def test_resume_execute_while_analysis_failed_uses_fallback(wf: WF) -> None:
    run = await wf.new_run()
    wf.agent.fail("plan", AgentProviderError("down"))
    with pytest.raises(AgentProviderError):
        await wf.start(run)
    action_id = uuid.uuid4()
    await wf.resume(run, {"type": "execute", "action_id": str(action_id), "actor_user_id": str(wf.owner.id)})
    assert wf.execution.calls == [("execute", wf.owner.id, action_id, None, None)]
    assert wf.agent.calls == ["classify", "extract", "plan"]  # the failed step is not retried by an execute


async def test_resume_without_checkpoint_and_reanalyze_starts_the_graph(wf: WF) -> None:
    run = await wf.new_run()
    engine = _fresh_engine(wf)
    await engine.resume(run.id, run.tenant_id, {"type": "reanalyze"})
    snap = await engine.graph.aget_state(WorkflowEngine._config(run.id))
    assert snap.interrupts
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.PENDING_APPROVAL


async def test_resume_with_interrupt_passes_decision(wf: WF) -> None:
    run = await wf.new_run()
    await wf.start(run)
    await wf.resume(run, {"type": "refresh", "action_id": "a", "version": 2, "payload_hash": "h"})
    (event,) = [e for e in await get_events(wf.db, run.id) if e.event_type is E.RESUME]
    assert event.details == {"type": "refresh", "action_id": "a", "version": 2}


async def _reviewing(wf: WF) -> tuple[WorkflowRun, ProposedAction]:
    wf.agent.tool_calls = [tool_call(WI)]
    run = await wf.new_run()
    await wf.start(run)
    (action,) = await get_actions(wf.db, run.id)
    return run, action


@pytest.mark.parametrize("kind", ["execute", "reconcile"])
async def test_fallback_calls_services_directly_when_graph_is_not_waiting(wf: WF, kind: str) -> None:
    run, action = await _reviewing(wf)
    engine = _fresh_engine(wf)  # checkpoint unavailable
    decision = wf.decision(kind, action)
    await engine.resume(run.id, run.tenant_id, decision)
    if kind == "execute":
        assert wf.execution.calls == [("execute", wf.owner.id, action.id, 1, action.payload_hash)]
    else:
        assert wf.execution.calls == [("reconcile", wf.owner.id, action.id)]
    (note,) = [e for e in await get_events(wf.db, run.id) if e.message and "graph not waiting" in e.message]
    assert note.event_type is E.STATUS_CHANGED
    assert note.message == f"graph not waiting; handled '{kind}' directly"


async def test_fallback_permission_denied_is_audited(wf: WF) -> None:
    run, action = await _reviewing(wf)
    viewer = actor_from_user(wf.viewer)
    wf.execution.errors["execute"] = AuditedPermissionDenied(
        "denied", actor=viewer, resource_type="action", resource_id=str(action.id)
    )
    await _fresh_engine(wf).resume(run.id, run.tenant_id, wf.decision("execute", action, user=wf.viewer))
    (audit,) = await get_audits(wf.db, wf.tenant, "permission_denied")
    assert audit.outcome is AuditOutcome.DENIED
    assert audit.actor_user_id == wf.viewer.id


async def test_fallback_app_error_is_swallowed_and_status_resynced(wf: WF) -> None:
    run, action = await _reviewing(wf)
    wf.execution.errors["reconcile"] = StaleVersion()
    await _fresh_engine(wf).resume(run.id, run.tenant_id, wf.decision("reconcile", action))
    assert any("handled 'reconcile'" in (e.message or "") for e in await get_events(wf.db, run.id))


async def test_fallback_with_real_execution_after_run_finished(wf: WF) -> None:
    wf.use_real_execution()
    run, action = await _reviewing(wf)
    async with wf.db.session() as s:
        await ApprovalService(wf.settings).approve(
            s, wf.actor(), action.id, version=1, payload_hash=action.payload_hash, comment=None
        )
    engine = _fresh_engine(wf)
    await engine.resume(run.id, run.tenant_id, wf.decision("execute", action))
    (done,) = await get_actions(wf.db, run.id)
    assert done.status is ActionStatus.COMPLETED
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.COMPLETED


async def test_fallback_for_other_decisions_only_resyncs(wf: WF) -> None:
    run, action = await _reviewing(wf)
    await set_action_status(wf.db, action.id, status=ActionStatus.REJECTED)
    await _fresh_engine(wf).resume(run.id, run.tenant_id, {"type": "refresh"})
    assert wf.execution.calls == []
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.REJECTED


# --------------------------------------------------------------------------- sync_run_status


async def test_sync_run_status_unknown_run_is_ignored(wf: WF) -> None:
    await wf.engine.sync_run_status(uuid.uuid4(), note="x")


async def test_sync_run_status_skips_cancelled_runs(wf: WF) -> None:
    run, action = await _reviewing(wf)
    await set_run_fields(wf.db, run.id, status=WorkflowStatus.CANCELLED)
    await set_action_status(wf.db, action.id, status=ActionStatus.APPROVED)
    await wf.engine.sync_run_status(run.id, note="note-cancelled")
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.CANCELLED
    assert not [e for e in await get_events(wf.db, run.id) if e.message == "note-cancelled"]


async def test_sync_run_status_without_actions_does_nothing(wf: WF) -> None:
    run = await add_run(wf.db, await wf.email(), WorkflowStatus.VALIDATED)
    await wf.engine.sync_run_status(run.id, note="note-empty")
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.VALIDATED
    assert await get_events(wf.db, run.id) == []


async def test_sync_run_status_updates_status_and_records_event(wf: WF) -> None:
    run, action = await _reviewing(wf)
    await set_action_status(wf.db, action.id, status=ActionStatus.APPROVED)
    await wf.engine.sync_run_status(run.id, note="note-sync")
    synced = await get_run(wf.db, run.id)
    assert (synced.status, synced.current_step) == (WorkflowStatus.APPROVED, "review")
    (event,) = [e for e in await get_events(wf.db, run.id) if e.message == "note-sync"]
    assert (event.step, event.event_type) == ("review", E.STATUS_CHANGED)


async def test_sync_run_status_same_status_still_records_note(wf: WF) -> None:
    run, _ = await _reviewing(wf)
    await wf.engine.sync_run_status(run.id, note="note-same")
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.PENDING_APPROVAL
    assert [e.message for e in await get_events(wf.db, run.id) if e.message == "note-same"] == ["note-same"]


async def test_sync_run_status_ignores_invalid_transitions(wf: WF) -> None:
    run, _ = await _reviewing(wf)
    await set_run_fields(wf.db, run.id, status=WorkflowStatus.COMPLETED)
    await wf.engine.sync_run_status(run.id, note="note-invalid")
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.COMPLETED
    assert not [e for e in await get_events(wf.db, run.id) if e.message == "note-invalid"]
