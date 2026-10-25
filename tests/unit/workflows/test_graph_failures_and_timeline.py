"""Analysis-step failure recording (categories, sanitized re-raise) and timeline bounds (Prompt 5)."""

from __future__ import annotations

import traceback

import pytest
from sqlalchemy.exc import OperationalError

from app.agents.base import AgentError, AgentOutputError, AgentProviderError
from app.agents.tools import MAX_TOOL_CALLS, OVERFLOW_TOOL_NAME
from app.core.errors import AppError, NotFound
from app.domain.enums import ActionType, FailureCategory, WorkflowEventType, WorkflowStatus
from app.schemas.ai import PlannedToolCall
from app.workflows.graph import (
    MAX_TOOL_CALL_EVENTS,
    SanitizedStepError,
    _sanitized_error,
    _sanitized_tool_args,
    _step_failure,
)
from tests.unit.workflows.conftest import WF
from tests.unit.workflows.fakes import get_events, get_run

SECRET = "Authorization: Bearer abc.def.ghi and the email body text"


# ---------------------------------------------------------------------------------------------------
# Failure categories
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "category", "retryable"),
    [
        (AgentError("rejected", category=FailureCategory.AUTHORIZATION), FailureCategory.AUTHORIZATION, False),
        (AgentError("bad request", category=FailureCategory.CONFIGURATION), FailureCategory.CONFIGURATION, False),
        (AgentError("generic"), FailureCategory.LLM_PROVIDER, False),
        (AgentProviderError("down"), FailureCategory.LLM_PROVIDER, True),
        (AgentOutputError("schema"), FailureCategory.LLM_OUTPUT, False),
    ],
)
async def test_agent_error_category_becomes_the_run_error_category(
    wf: WF, error: AgentError, category: FailureCategory, retryable: bool
) -> None:
    run = await wf.new_run()
    wf.agent.fail("classify", error)
    with pytest.raises(type(error)):
        await wf.start(run)

    failed = await get_run(wf.db, run.id)
    assert (failed.status, failed.error_category, failed.error_message) == (
        WorkflowStatus.FAILED,
        category,
        error.message,
    )
    last = (await get_events(wf.db, run.id))[-1]
    assert last.details == {"failure_category": category.value, "retryable": retryable}


def test_step_failure_of_application_and_unexpected_errors() -> None:
    app_error = NotFound("Email not found.")
    failure = _step_failure(app_error, "prepare")
    assert (failure.category, failure.message, failure.retryable, failure.error) == (
        FailureCategory.INTERNAL,
        "Email not found.",
        False,
        app_error,
    )
    unexpected = _step_failure(KeyError(SECRET), "plan")
    assert (failure.error_type, unexpected.error_type) == ("NotFound", "KeyError")
    assert (unexpected.category, unexpected.message) == (FailureCategory.INTERNAL, "KeyError")
    assert type(unexpected.error) is KeyError
    assert SECRET not in repr(unexpected.error)


# ---------------------------------------------------------------------------------------------------
# Sanitized re-raise of unexpected exceptions
# ---------------------------------------------------------------------------------------------------


async def test_unexpected_exception_leaves_the_node_without_its_text_or_cause(wf: WF) -> None:
    run = await wf.new_run()
    try:
        raise ConnectionError("inner detail " + SECRET)
    except ConnectionError as inner:
        crash = RuntimeError(SECRET)
        crash.__cause__ = inner
    wf.agent.fail("extract", crash)

    with pytest.raises(RuntimeError) as raised:
        await wf.start(run)

    error = raised.value
    assert error is not crash
    assert str(error) == "Analysis step 'extract' failed (RuntimeError); the original error text is not recorded."
    assert (error.__cause__, error.__suppress_context__) == (None, True)
    rendered = "".join(traceback.format_exception(error))
    assert SECRET not in rendered
    assert "inner detail" not in rendered
    assert "fakes.py" in rendered  # the original frames (where it was raised) are kept
    failed = await get_run(wf.db, run.id)
    assert (failed.error_category, failed.error_message) == (FailureCategory.INTERNAL, "RuntimeError")


async def test_application_errors_leave_the_node_unchanged(wf: WF) -> None:
    run = await wf.new_run()
    error = AppError("Something the user may read.")
    wf.agent.fail("classify", error)
    with pytest.raises(AppError) as raised:
        await wf.start(run)
    assert raised.value is error


class _NeedsTwoArguments(Exception):
    def __init__(self, code: int, detail: str) -> None:
        super().__init__(code, detail)


class _RewritesItsMessage(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(f"wrapped: {message}")


@pytest.mark.parametrize(
    ("error", "expected_type"),
    [
        (ValueError(SECRET), ValueError),
        (KeyError(SECRET), KeyError),
        (OSError(2, SECRET, "/secret/path"), FileNotFoundError),  # OSError() picks the errno subclass
    ],
)
def test_sanitized_error_keeps_the_type_when_it_can(error: Exception, expected_type: type[Exception]) -> None:
    try:
        raise error
    except Exception as exc:
        clean = _sanitized_error(exc, "classify")
    assert type(clean) is expected_type
    assert clean.args == (
        "Analysis step 'classify' failed (" + type(error).__name__ + "); the original error text is not recorded.",
    )
    assert SECRET not in repr(clean)
    assert "/secret/path" not in str(clean)
    assert clean.__traceback__ is not None


@pytest.mark.parametrize(
    "error",
    [
        _NeedsTwoArguments(1, SECRET),  # cannot be built from one message
        _RewritesItsMessage(SECRET),  # would not carry the fixed message as given
        OperationalError("SELECT secret FROM t WHERE x = ?", ("email body",), Exception(SECRET)),
    ],
)
def test_types_that_cannot_carry_a_fixed_message_get_a_stand_in_with_the_same_name(error: Exception) -> None:
    clean = _sanitized_error(error, "plan")
    assert isinstance(clean, SanitizedStepError)
    assert type(clean).__name__ == type(error).__name__
    assert type(clean) is type(_sanitized_error(error, "plan"))  # one stand-in class per original type
    rendered = "".join(traceback.format_exception(clean))
    assert SECRET not in rendered
    assert "email body" not in rendered
    assert f"app.workflows.graph.{type(error).__qualname__}: Analysis step 'plan' failed" in rendered


async def test_a_database_error_keeps_its_type_name_for_the_worker(wf: WF) -> None:
    run = await wf.new_run()
    wf.agent.fail("classify", OperationalError("SELECT 1", {"p": SECRET}, Exception(SECRET)))
    with pytest.raises(SanitizedStepError) as raised:
        await wf.start(run)
    assert type(raised.value).__name__ == "OperationalError"
    assert SECRET not in repr(raised.value)
    assert (await get_run(wf.db, run.id)).error_message == "OperationalError"


# ---------------------------------------------------------------------------------------------------
# Timeline bounds
# ---------------------------------------------------------------------------------------------------


def test_sanitized_tool_args_bounds_lists_elements_and_nested_values() -> None:
    args = {
        "title": "t" * 500,
        "tags": [f"tag-{i}-" + "x" * 500 for i in range(1_000)] + [{"nested": "x" * 10_000}],
        "priority": "high",
        "work_item_type": {"evil": "x" * 10_000},
        "subject": None,
        "due_date": 3,
        **{f"extra_{i}": "v" for i in range(100)},
    }
    out = _sanitized_tool_args(args)
    assert len(out["tags"]) == 10
    assert all(len(t) <= 64 for t in out["tags"])
    assert out["tags_omitted"] == 991
    assert out["title"] == "t" * 120
    assert out["work_item_type"] == "(dict)"
    assert (out["subject"], out["due_date"], out["priority"]) == (None, 3, "high")
    assert len(out["argument_keys"]) == 20
    assert len(str(out)) < 3_000


def test_short_tag_lists_are_copied_as_they_are() -> None:
    out = _sanitized_tool_args({"tags": ["a", "b"], "title": "t"})
    assert out["tags"] == ["a", "b"]
    assert "tags_omitted" not in out


def _call(tool: str, action_type: ActionType) -> PlannedToolCall:
    args = {"reason": "r", "rationale": "r", "expected_impact": "i"}
    return PlannedToolCall(tool_name=tool, action_type=action_type, arguments=args)  # type: ignore[arg-type]


async def _tool_events(wf: WF, run_id: object) -> list[object]:
    return [e for e in await get_events(wf.db, run_id) if e.event_type is WorkflowEventType.TOOL_CALL]  # type: ignore[arg-type]


async def test_any_number_of_rejected_calls_from_an_agent_is_bounded_in_the_timeline(wf: WF) -> None:
    wf.agent.rejected_calls = [{"tool_name": f"junk_{i}", "reason": "unknown or unavailable tool"} for i in range(50)]
    run = await wf.new_run()
    await wf.start(run)

    events = await _tool_events(wf, run.id)
    assert len(events) == MAX_TOOL_CALL_EVENTS == MAX_TOOL_CALLS + 1
    summary = events[-1]
    assert summary.status == "rejected"  # type: ignore[attr-defined]
    proposed = sum(1 for e in events if e.status == "proposed")  # type: ignore[attr-defined]
    assert summary.details == {  # type: ignore[attr-defined]
        "tool_name": OVERFLOW_TOOL_NAME,
        "reason": "too many tool calls",
        "omitted": proposed + 50 - (MAX_TOOL_CALL_EVENTS - 1),
    }


async def test_rejected_call_records_are_clipped_in_the_timeline(wf: WF) -> None:
    wf.agent.tool_calls = [_call("propose_no_action", ActionType.MARK_NO_ACTION)]
    wf.agent.rejected_calls = [{"tool_name": "x" * 500, "reason": "r" * 5_000, **{f"k{i}": "v" for i in range(10)}}]
    run = await wf.new_run()
    await wf.start(run)

    [proposed, rejected] = await _tool_events(wf, run.id)
    assert proposed.status == "proposed"  # type: ignore[attr-defined]
    assert rejected.details == {"tool_name": "x" * 300, "reason": "r" * 300, "k0": "v", "k1": "v"}  # type: ignore[attr-defined]


async def test_exactly_the_event_budget_has_no_summary(wf: WF) -> None:
    wf.agent.tool_calls = [_call("propose_no_action", ActionType.MARK_NO_ACTION)]
    count = MAX_TOOL_CALL_EVENTS - 1
    wf.agent.rejected_calls = [
        {"tool_name": f"junk_{i}", "reason": "unknown or unavailable tool"} for i in range(count)
    ]
    run = await wf.new_run()
    await wf.start(run)

    events = await _tool_events(wf, run.id)
    assert len(events) == MAX_TOOL_CALL_EVENTS
    assert all(e.details.get("tool_name") != OVERFLOW_TOOL_NAME for e in events)  # type: ignore[attr-defined]
