"""Analysis nodes (prepare → classify → extract → validate → plan) driven through WorkflowEngine."""

from __future__ import annotations

import pytest

from app.agents.base import AgentOutputError, AgentProviderError
from app.core.errors import AppError
from app.core.security import compute_payload_hash
from app.domain.enums import (
    ACTION_TARGETS,
    ActionStatus,
    ActionType,
    EmailAnalysisStatus,
    EmailCategory,
    FailureCategory,
    WorkflowEventType,
    WorkflowStatus,
)
from app.schemas.actions import REQUIRED_PERMISSIONS
from app.schemas.ai import AI_SCHEMA_VERSION
from app.services.action_policy import idempotency_key
from tests.unit.workflows.conftest import WF
from tests.unit.workflows.fakes import (
    INJECTION_BODY,
    get_actions,
    get_analyses,
    get_audits,
    get_email,
    get_events,
    get_run,
    set_run_fields,
)

E = WorkflowEventType


def _steps(events: list) -> list[tuple[str, str, str]]:  # type: ignore[type-arg]
    return [(e.step, e.event_type.value, e.status) for e in events]


async def test_happy_path_reaches_human_review(wf: WF) -> None:
    run = await wf.new_run()
    await wf.start(run)

    run_after = await get_run(wf.db, run.id)
    assert run_after.status is WorkflowStatus.PENDING_APPROVAL
    assert run_after.current_step == "review"
    assert run_after.analysis_attempt == 1
    assert run_after.started_at is not None
    assert run_after.awaiting_review_since is not None
    assert run_after.completed_at is None
    assert (run_after.model_provider, run_after.model_name, run_after.prompt_version) == (
        "fake-provider",
        "fake-model-1",
        "fake-prompt-v1",
    )
    assert run_after.schema_version == AI_SCHEMA_VERSION
    assert run_after.error_category is None
    # Metrics accumulate over the three LLM calls (tokens, cost, latency).
    assert run_after.metrics == {
        "llm_calls": 3,
        "input_tokens": 300,
        "output_tokens": 60,
        "total_tokens": 360,
        "estimated_cost_usd": 0.0045,
        "llm_latency_ms": 15,
    }
    assert wf.agent.calls == ["classify", "extract", "plan"]

    email = await get_email(wf.db, run.email_message_id)
    assert email.analysis_status is EmailAnalysisStatus.AWAITING_REVIEW
    assert email.category is EmailCategory.INCIDENT
    assert email.priority is not None
    assert email.content_changed_since_analysis is False

    (analysis,) = await get_analyses(wf.db, run.id)
    assert analysis.attempt == 1
    assert analysis.category is EmailCategory.INCIDENT
    assert analysis.model_provider == "fake-provider"
    assert analysis.schema_version == AI_SCHEMA_VERSION
    assert 0 <= analysis.confidence_score <= 1
    assert analysis.classification["category"] == "incident"
    assert analysis.injection_flags == []

    events = await get_events(wf.db, run.id)
    steps = _steps(events)
    assert steps[:4] == [
        ("prepare", "step_completed", "success"),
        ("classify", "llm_call", "success"),
        ("extract", "llm_call", "success"),
        ("validate", "step_completed", "success"),
    ]
    assert steps[4] == ("plan", "llm_call", "success")
    assert all(s == ("plan", "tool_call", "proposed") for s in steps[5:])
    prepare = events[0]
    assert prepare.details["attempt"] == 1
    assert prepare.details["truncated"] is False
    assert prepare.details["injection_rules"] == []
    assert prepare.details["body_length"] > 50
    classify = events[1]
    assert classify.latency_ms == 5
    assert classify.details["category"] == "incident"
    assert classify.details["prompt_version"] == "fake-prompt-v1"
    assert classify.details["usage"]["total_tokens"] == 120
    assert events[2].details["evidence_count"] >= 1
    assert "confidence_score" in events[3].details

    actions = await get_actions(wf.db, run.id)
    assert actions
    assert len(steps[5:]) == len(actions)  # one proposed tool call per created action
    for action in actions:
        assert action.status is ActionStatus.PENDING_APPROVAL
        assert action.version == 1
        assert action.analysis_attempt == 1
        assert action.owner_user_id == wf.owner.id
        assert action.target_integration is ACTION_TARGETS[action.action_type]
        assert action.idempotency_key == idempotency_key(wf.tenant.id, run.email_message_id, action.action_type, 0)
        assert action.payload_hash == compute_payload_hash(
            action.action_type.value, action.target_integration.value, action.payload
        )
        assert action.required_permissions == REQUIRED_PERMISSIONS[action.action_type]
        assert action.source == "agent"
    assert ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM in {a.action_type for a in actions}

    (proposed,) = await get_audits(wf.db, wf.tenant, "actions_proposed")
    assert proposed.details == {"action_ids": [str(a.id) for a in actions], "attempt": 1}
    assert proposed.actor_type.value == "agent"

    snap = await wf.snapshot(run)
    assert snap.next == ("wait_for_review",)
    (intr,) = snap.interrupts
    assert intr.value == {"run_id": str(run.id), "awaiting": "human_review"}
    assert snap.values["action_ids"] == [str(a.id) for a in actions]


@pytest.mark.parametrize(
    ("step", "error", "category", "retryable"),
    [
        ("classify", AgentProviderError("provider down"), FailureCategory.LLM_PROVIDER, True),
        ("extract", AgentProviderError("rate limited"), FailureCategory.LLM_PROVIDER, True),
        ("plan", AgentProviderError("timeout"), FailureCategory.LLM_PROVIDER, True),
        ("classify", AgentOutputError("refused"), FailureCategory.LLM_OUTPUT, False),
        ("extract", AgentOutputError("schema invalid"), FailureCategory.LLM_OUTPUT, False),
        ("plan", AgentOutputError("bad tool call"), FailureCategory.LLM_OUTPUT, False),
    ],
)
async def test_analysis_step_failure_marks_run_failed_and_propagates(
    wf: WF, step: str, error: Exception, category: FailureCategory, retryable: bool
) -> None:
    run = await wf.new_run()
    wf.agent.fail(step, error)
    with pytest.raises(type(error)):
        await wf.start(run)

    failed = await get_run(wf.db, run.id)
    assert failed.status is WorkflowStatus.FAILED
    assert failed.current_step == step
    assert failed.error_category is category
    assert failed.error_message == error.message  # type: ignore[attr-defined]
    email = await get_email(wf.db, run.email_message_id)
    assert email.analysis_status is EmailAnalysisStatus.FAILED
    last = (await get_events(wf.db, run.id))[-1]
    assert (last.step, last.event_type, last.status) == (step, E.STEP_FAILED, "failure")
    assert last.details == {"failure_category": category.value, "retryable": retryable}
    assert last.message == error.message  # type: ignore[attr-defined]
    assert await get_actions(wf.db, run.id) == []
    snap = await wf.snapshot(run)
    assert snap.next == (step,)
    assert not snap.interrupts


async def test_unexpected_exception_is_internal_and_message_is_type_name(wf: WF) -> None:
    run = await wf.new_run()
    wf.agent.fail("extract", RuntimeError("secret detail from provider"))
    with pytest.raises(RuntimeError):
        await wf.start(run)
    failed = await get_run(wf.db, run.id)
    assert failed.error_category is FailureCategory.INTERNAL
    assert failed.error_message == "RuntimeError"  # never the raw exception text


async def test_retry_resumes_from_failed_node_and_clears_stale_error(wf: WF) -> None:
    run = await wf.new_run()
    wf.agent.fail("extract", AgentProviderError("provider down"))
    with pytest.raises(AgentProviderError):
        await wf.start(run)
    assert wf.agent.calls == ["classify", "extract"]

    await wf.start(run)  # job retried: the engine re-runs only the failed step and what follows

    assert wf.agent.calls == ["classify", "extract", "extract", "plan"]
    recovered = await get_run(wf.db, run.id)
    assert recovered.status is WorkflowStatus.PENDING_APPROVAL
    assert recovered.analysis_attempt == 1  # prepare did not run again
    # Regression: the "Run failed" banner fields must not survive a successful retry.
    assert recovered.error_category is None
    assert recovered.error_message is None
    assert recovered.metrics["llm_calls"] == 3
    email = await get_email(wf.db, run.email_message_id)
    assert email.analysis_status is EmailAnalysisStatus.AWAITING_REVIEW
    events = await get_events(wf.db, run.id)
    assert [e.step for e in events if e.event_type is E.STEP_FAILED] == ["extract"]
    assert sum(1 for e in events if e.step == "prepare") == 1


async def test_retry_after_failure_in_plan(wf: WF) -> None:
    run = await wf.new_run()
    wf.agent.fail("plan", AgentOutputError("bad"))
    with pytest.raises(AgentOutputError):
        await wf.start(run)
    await wf.start(run)
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.PENDING_APPROVAL
    assert len(await get_analyses(wf.db, run.id)) == 1


async def test_purged_body_refuses_analysis(wf: WF) -> None:
    run = await wf.new_run(purged=True)
    with pytest.raises(AppError, match="purged by the retention policy"):
        await wf.start(run)
    failed = await get_run(wf.db, run.id)
    assert failed.status is WorkflowStatus.FAILED
    assert failed.current_step == "prepare"
    assert failed.error_category is FailureCategory.INTERNAL
    assert failed.error_message is not None
    assert "purged" in failed.error_message
    assert wf.agent.calls == []
    (event,) = await get_events(wf.db, run.id)
    assert (event.step, event.event_type) == ("prepare", E.STEP_FAILED)
    assert event.details["retryable"] is False


async def test_missing_body_without_purge_is_analyzed_as_empty(wf: WF) -> None:
    run = await wf.new_run(body=None)
    await wf.start(run)
    assert (await get_events(wf.db, run.id))[0].details["body_length"] == 0
    assert wf.agent.calls == ["classify", "extract", "plan"]


async def _assert_cancelled_and_finalized(wf: WF, run_id) -> None:  # type: ignore[no-untyped-def]
    done = await get_run(wf.db, run_id)
    assert done.status is WorkflowStatus.CANCELLED
    assert done.current_step == "finalize"
    assert done.completed_at is not None
    email = await get_email(wf.db, done.email_message_id)
    assert email.analysis_status is EmailAnalysisStatus.DONE
    events = await get_events(wf.db, run_id)
    assert events[-1].step == "finalize"
    assert events[-1].event_type is E.STATUS_CHANGED
    assert events[-1].message == "Workflow cancelled."
    assert await get_actions(wf.db, run_id) == []
    (finished,) = await get_audits(wf.db, wf.tenant, "workflow_finished")
    assert finished.details == {"status": "cancelled"}
    snap = await wf.engine.graph.aget_state({"configurable": {"thread_id": str(run_id)}})
    assert snap.next == ()
    assert snap.values["review_done"] is True


@pytest.mark.parametrize("step", ["classify", "extract", "plan"])
async def test_cancel_while_model_call_in_flight_halts_and_finalizes(wf: WF, step: str) -> None:
    # Regression: classify/extract did not re-check for cancellation after the model call and failed with
    # InvalidStateTransition (error recorded on a cancelled run, email marked failed, run never finalized).
    run = await wf.new_run()

    async def cancel() -> None:
        await set_run_fields(wf.db, run.id, status=WorkflowStatus.CANCELLED)

    wf.agent.hooks[step] = cancel
    await wf.start(run)

    order = ["classify", "extract", "plan"]
    assert wf.agent.calls == order[: order.index(step) + 1]
    await _assert_cancelled_and_finalized(wf, run.id)
    done = await get_run(wf.db, run.id)
    assert done.error_category is None
    assert done.error_message is None
    events = await get_events(wf.db, run.id)
    assert not [e for e in events if e.event_type is E.STEP_FAILED]
    assert not [e for e in events if e.step == step]  # the halted step recorded nothing


async def test_cancelled_before_start_halts_in_prepare(wf: WF) -> None:
    run = await wf.new_run()
    await set_run_fields(wf.db, run.id, status=WorkflowStatus.CANCELLED)
    await wf.start(run)
    assert wf.agent.calls == []
    await _assert_cancelled_and_finalized(wf, run.id)
    assert [e.step for e in await get_events(wf.db, run.id)] == ["finalize"]


@pytest.mark.parametrize("step", ["classify", "extract", "validate", "plan"])
async def test_retry_of_a_cancelled_failed_run_halts_at_step_start(
    wf: WF, step: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await wf.new_run()
    if step == "validate":
        import app.workflows.graph as graph_module

        real = graph_module.validate_analysis
        calls = {"n": 0}

        def flaky(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("validator crashed")
            return real(*args, **kwargs)

        monkeypatch.setattr(graph_module, "validate_analysis", flaky)
        expected_error: type[Exception] = RuntimeError
    else:
        wf.agent.fail(step, AgentProviderError("down"))
        expected_error = AgentProviderError
    with pytest.raises(expected_error):
        await wf.start(run)
    assert (await get_run(wf.db, run.id)).status is WorkflowStatus.FAILED
    calls_before = list(wf.agent.calls)

    await set_run_fields(wf.db, run.id, status=WorkflowStatus.CANCELLED)  # the user cancels the failed run
    await wf.start(run)  # the retry job runs anyway

    assert wf.agent.calls == calls_before  # the failed step was not attempted again
    await _assert_cancelled_and_finalized(wf, run.id)


async def test_injection_flags_propagate_to_prepare_analysis_and_actions(wf: WF) -> None:
    run = await wf.new_run(body=INJECTION_BODY)
    await wf.start(run)
    prepare = (await get_events(wf.db, run.id))[0]
    assert prepare.details["injection_rules"]
    (analysis,) = await get_analyses(wf.db, run.id)
    assert analysis.injection_flags
    actions = await get_actions(wf.db, run.id)
    assert actions
    for action in actions:
        assert action.validation_warnings[0] == (
            "Prompt-injection indicators were detected in the source email. Review carefully."
        )


async def test_large_bodies_are_truncated_for_the_model(wf: WF) -> None:
    run = await wf.new_run(body="Production users are unable to log in. " * 1000)
    await wf.start(run)
    prepare = (await get_events(wf.db, run.id))[0]
    assert prepare.details["truncated"] is True
    assert prepare.details["body_length"] <= wf.settings.llm_max_email_chars
