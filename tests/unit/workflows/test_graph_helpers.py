"""Graph helpers and crash/re-run edge cases of individual nodes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.agents.base import LLMUsage
from app.core.errors import InvalidStateTransition, NotFound
from app.core.timeutil import utcnow
from app.db.models import EmailAnalysis, EmailMessage, WorkflowRun
from app.domain.enums import FailureCategory, WorkflowEventType, WorkflowStatus
from app.workflows.engine import WorkflowEngine
from app.workflows.graph import _accumulate, _sanitized_tool_args, email_context, set_run_status
from tests.conftest import make_settings
from tests.unit.workflows.conftest import WF
from tests.unit.workflows.fakes import get_actions, get_analyses, get_events, get_run, set_run_fields

W = WorkflowStatus


def _run(status: WorkflowStatus, **fields: object) -> WorkflowRun:
    return WorkflowRun(status=status, metrics={}, **fields)


def test_set_run_status_sets_step_and_validates() -> None:
    run = _run(W.RECEIVED)
    set_run_status(run, W.NORMALIZED)
    assert (run.status, run.current_step) == (W.NORMALIZED, "normalized")
    set_run_status(run, W.CLASSIFIED, "classify")
    assert run.current_step == "classify"
    with pytest.raises(InvalidStateTransition):
        set_run_status(run, W.COMPLETED)
    assert run.status is W.CLASSIFIED


def test_set_run_status_clears_errors_only_when_leaving_failed() -> None:
    failed = _run(W.FAILED, error_category=FailureCategory.LLM_PROVIDER, error_message="down")
    set_run_status(failed, W.FAILED, "classify")  # re-entering failed keeps the error
    assert failed.error_category is FailureCategory.LLM_PROVIDER
    set_run_status(failed, W.CLASSIFIED)
    assert (failed.error_category, failed.error_message) == (None, None)

    other = _run(W.PENDING_APPROVAL, error_category=FailureCategory.INTERNAL, error_message="kept")
    set_run_status(other, W.APPROVED)
    assert other.error_message == "kept"


def test_accumulate_sums_and_handles_missing_values() -> None:
    run = _run(W.CLASSIFIED)
    _accumulate(run, LLMUsage(provider="p", model="m", input_tokens=10, output_tokens=2, total_tokens=12, latency_ms=3))
    assert run.metrics == {
        "llm_calls": 1,
        "input_tokens": 10,
        "output_tokens": 2,
        "total_tokens": 12,
        "llm_latency_ms": 3,
    }
    _accumulate(run, LLMUsage(provider="p", model="m", latency_ms=4, estimated_cost_usd=0.0000014))
    _accumulate(run, LLMUsage(provider="p", model="m", input_tokens=1, estimated_cost_usd=0.1234567))
    assert run.metrics["llm_calls"] == 3
    assert run.metrics["input_tokens"] == 11
    assert run.metrics["total_tokens"] == 12
    assert run.metrics["llm_latency_ms"] == 7
    assert run.metrics["estimated_cost_usd"] == pytest.approx(0.123458, abs=1e-9)  # rounded to 6 places


def test_accumulate_tolerates_empty_metrics() -> None:
    run = _run(W.CLASSIFIED)
    run.metrics = None  # type: ignore[assignment]
    _accumulate(run, LLMUsage(provider="p", model="m"))
    assert run.metrics["llm_calls"] == 1


def test_sanitized_tool_args_keeps_structural_fields_only() -> None:
    args = {
        "title": "x" * 300,
        "work_item_type": "Bug",
        "priority": "high",
        "tags": ["a", "b"],
        "subject": "Re: hello",
        "due_date": None,
        "description": "free text",
        "body": "free text",
        "reason": "free text",
    }
    out = _sanitized_tool_args(args)
    assert out["title"] == "x" * 120
    assert out["tags"] == ["a", "b"]
    assert out["due_date"] is None
    assert out["argument_keys"] == sorted(args)
    assert set(out) == {"title", "work_item_type", "priority", "tags", "subject", "due_date", "argument_keys"}
    assert _sanitized_tool_args({}) == {"argument_keys": []}


def test_email_context_maps_and_bounds_fields() -> None:
    email = EmailMessage(
        id=uuid.uuid4(),
        subject=None,
        sender_name="Jo",
        sender_address="jo@example.com",
        to_recipients=[{"address": "a@example.com"}, {"name": "no address"}, {"address": ""}],
        cc_recipients=[{"address": "c@example.com"}],
        received_at=utcnow(),
        body_text="Hello " * 200,
        has_attachments=True,
        attachments=[{"name": "a.txt"}, {}],
    )
    ctx = email_context(email, make_settings("sqlite+aiosqlite:///x.db", llm_max_email_chars=500))
    assert ctx.subject == ""
    assert ctx.to == ["a@example.com"]
    assert ctx.cc == ["c@example.com"]
    assert ctx.attachment_names == ["a.txt"]
    assert ctx.truncated is True
    assert len(ctx.body_text) <= 500
    assert ctx.email_id == email.id


async def test_validate_rerun_reuses_its_analysis_row(wf: WF) -> None:
    run = await wf.new_run()
    await wf.start(run)
    (first,) = await get_analyses(wf.db, run.id)
    actions = await get_actions(wf.db, run.id)
    # Crash after validate committed: run is `validated`, the graph's next node is validate again.
    await set_run_fields(wf.db, run.id, status=W.VALIDATED)
    await wf.engine.graph.aupdate_state(WorkflowEngine._config(run.id), None, as_node="extract")
    await wf.start(run)
    (again,) = await get_analyses(wf.db, run.id)
    assert again.id == first.id
    assert [a.id for a in await get_actions(wf.db, run.id)] == [a.id for a in actions]
    assert (await get_run(wf.db, run.id)).status is W.PENDING_APPROVAL


async def test_plan_without_its_analysis_fails_the_run(wf: WF) -> None:
    run = await wf.new_run()
    await wf.start(run)
    async with wf.db.session() as s:
        await s.execute(delete(EmailAnalysis).where(EmailAnalysis.workflow_run_id == run.id))
        await s.commit()
    await set_run_fields(wf.db, run.id, status=W.VALIDATED)
    await wf.engine.graph.aupdate_state(WorkflowEngine._config(run.id), None, as_node="validate")
    with pytest.raises(NotFound, match="Analysis not found"):
        await wf.start(run)
    failed = await get_run(wf.db, run.id)
    assert failed.status is W.FAILED
    assert (failed.error_category, failed.error_message) == (FailureCategory.INTERNAL, "Analysis not found.")


async def test_failure_on_a_finished_run_keeps_its_status(wf: WF) -> None:
    run = await wf.new_run()

    async def finish() -> None:
        await set_run_fields(wf.db, run.id, status=W.COMPLETED)

    wf.agent.hooks["classify"] = finish  # run finished elsewhere while the model call was in flight
    with pytest.raises(InvalidStateTransition):
        await wf.start(run)
    after = await get_run(wf.db, run.id)
    assert after.status is W.COMPLETED  # a terminal status is never overwritten with failed
    assert after.error_category is FailureCategory.INTERNAL
    last = (await get_events(wf.db, run.id))[-1]
    assert (last.step, last.event_type) == ("classify", WorkflowEventType.STEP_FAILED)


async def test_unknown_run_is_not_found(wf: WF) -> None:
    run = await wf.new_run()
    await set_run_fields(wf.db, run.id, status=W.RECEIVED)
    ghost = WorkflowRun(id=uuid.uuid4(), tenant_id=run.tenant_id)
    with pytest.raises(NotFound, match="Workflow run not found"):
        await wf.start(ghost)
