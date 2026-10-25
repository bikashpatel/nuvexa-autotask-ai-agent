"""Planning-tool registry and schema-level validation of proposal tool calls."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agents.tools import (
    MAX_TOOL_CALLS,
    OVERFLOW_TOOL_NAME,
    collect_tool_calls,
    completed_action_types,
    offered_tools,
    safe_tool_name,
    validate_tool_call,
)
from app.domain.enums import ActionType
from app.schemas.ai import PLANNING_TOOLS, PlannedToolCall, PlanningContext, ProposeNoActionArgs

ALL = list(ActionType)
NO_ACTION = {"reason": "FYI", "rationale": "Informational.", "expected_impact": "Closed."}
REPLY = {"subject": "Re: hi", "body": "Thanks", "rationale": "Ack.", "expected_impact": "Informed."}
TASK = {"title": "Follow up", "description": "d", "due_date": None, "rationale": "r", "expected_impact": "e"}
WORK_ITEM = {
    "title": "Fix login",
    "description": "Users cannot log in.",
    "work_item_type": "Bug",
    "priority": "high",
    "tags": ["nuvexa"],
    "rationale": "Incident.",
    "expected_impact": "Tracked.",
}


def context(allowed: list[ActionType] | None = None, *, ado: bool = True) -> PlanningContext:
    return PlanningContext(
        allowed_action_types=ALL if allowed is None else allowed,
        allowed_work_item_types=["Bug", "Task"],
        default_work_item_type="Task",
        azure_devops_available=ado,
    )


@pytest.mark.parametrize(
    ("allowed", "ado", "expected"),
    [
        (ALL, True, list(PLANNING_TOOLS)),
        (ALL, False, ["propose_email_reply", "propose_follow_up_task", "propose_no_action"]),
        ([ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM], False, []),
        (
            [ActionType.MARK_NO_ACTION, ActionType.PROPOSE_EMAIL_REPLY],
            True,
            ["propose_email_reply", "propose_no_action"],
        ),
        ([], True, []),
    ],
)
def test_offered_tools(allowed: list[ActionType], ado: bool, expected: list[str]) -> None:
    offered = offered_tools(context(allowed, ado=ado))
    assert list(offered) == expected
    for name, (action_type, model) in offered.items():
        assert PLANNING_TOOLS[name] == (action_type, model)


def test_completed_action_types_accepts_action_values_and_tool_names() -> None:
    summaries = [
        "create_azure_devops_work_item: Bug: login",
        "PROPOSE_NO_ACTION : already closed",  # tool name, case/whitespace-insensitive head
        "propose_email_reply",  # no title
        "unknown_action: x",
        "",
        "Bug: create_follow_up_task",  # the action type must be the head, not part of the title
    ]
    assert completed_action_types(summaries) == {
        ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM,
        ActionType.MARK_NO_ACTION,
        ActionType.PROPOSE_EMAIL_REPLY,
    }
    assert completed_action_types([]) == set()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("propose_no_action", "propose_no_action"),
        ("rm -rf /; drop table", "rm-rfdroptable"),
        ("<script>alert(1)</script>", "scriptalert1script"),
        ("", "(empty)"),
        ("!!!", "(empty)"),
        (None, "None"),
        ("x" * 100, "x" * 64),
    ],
)
def test_safe_tool_name(raw: object, expected: str) -> None:
    assert safe_tool_name(raw) == expected


@pytest.mark.parametrize(
    "arguments",
    [
        json.dumps(NO_ACTION),
        json.dumps(NO_ACTION).encode(),
        NO_ACTION,
        ProposeNoActionArgs(**NO_ACTION),
    ],
    ids=["json-str", "json-bytes", "dict", "model"],
)
def test_validate_tool_call_accepts_every_argument_shape(arguments: object) -> None:
    result = validate_tool_call("propose_no_action", arguments, offered_tools(context()))
    assert isinstance(result, PlannedToolCall)
    assert result.action_type is ActionType.MARK_NO_ACTION
    assert result.arguments == NO_ACTION


class _ExplodingArgs(ProposeNoActionArgs):
    def model_dump(self, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        raise TypeError("not serializable")


@pytest.mark.parametrize(
    ("name", "arguments", "reason"),
    [
        ("propose_no_action", "{not json", "invalid arguments (arguments: json_invalid)"),
        ("propose_no_action", "[]", "invalid arguments (arguments: model_type)"),
        ("propose_no_action", None, "invalid arguments (arguments: model_type)"),
        ("propose_no_action", {**NO_ACTION, "extra": "x"}, "invalid arguments (extra: extra_forbidden)"),
        ("propose_no_action", {"reason": 1}, "invalid arguments (reason: string_type"),
        ("propose_no_action", _ExplodingArgs(**NO_ACTION), "invalid arguments (not a JSON object)"),
        ("delete_everything", {}, "unknown or unavailable tool"),
        ("PROPOSE_NO_ACTION", NO_ACTION, "unknown or unavailable tool"),  # names are exact
    ],
)
def test_validate_tool_call_rejections(name: str, arguments: object, reason: str) -> None:
    result = validate_tool_call(name, arguments, offered_tools(context()))
    assert isinstance(result, dict)
    assert result["reason"].startswith(reason)
    assert len(result["reason"]) <= 300


def test_rejection_reasons_never_echo_argument_values() -> None:
    secret = "SECRET-EMAIL-CONTENT-" * 20
    calls = [("propose_email_reply", {"subject": secret, "body": 7, "rationale": [secret], "x": secret})]
    _, rejected = collect_tool_calls(calls, offered_tools(context()))
    assert len(rejected) == 1
    assert "SECRET" not in rejected[0]["reason"]
    assert len(rejected[0]["reason"]) <= 300


def test_registered_tool_that_is_not_offered_is_rejected() -> None:
    offered = offered_tools(context([ActionType.MARK_NO_ACTION]))
    result = validate_tool_call("propose_email_reply", REPLY, offered)
    assert result == {"tool_name": "propose_email_reply", "reason": "unknown or unavailable tool"}


def test_tool_outside_the_known_literal_set_is_rejected_even_if_offered() -> None:
    offered = {"rogue_tool": (ActionType.MARK_NO_ACTION, ProposeNoActionArgs)}
    assert validate_tool_call("rogue_tool", NO_ACTION, offered) == {  # type: ignore[arg-type]
        "tool_name": "rogue_tool",
        "reason": "unknown or unavailable tool",
    }


def test_collect_caps_the_number_of_calls() -> None:
    # Regression (Prompt 5): calls beyond the cap collapse into one summary record (was one record per call).
    calls: list[tuple[str, object]] = [("propose_email_reply", REPLY)] + [("junk", {})] * (MAX_TOOL_CALLS + 2)
    accepted, rejected = collect_tool_calls(calls, offered_tools(context()))
    assert [c.tool_name for c in accepted] == ["propose_email_reply"]
    assert rejected[-1] == {"tool_name": OVERFLOW_TOOL_NAME, "reason": "too many tool calls (3 more ignored)"}
    assert len(rejected) == MAX_TOOL_CALLS  # 5 unknown tools within the cap + the summary


def test_collect_is_bounded_however_many_calls_the_model_makes() -> None:
    flood: list[tuple[str, object]] = [("propose_email_reply", REPLY)] + [(f"exfiltrate_{i}", {}) for i in range(5_000)]
    accepted, rejected = collect_tool_calls(flood, offered_tools(context()))
    assert len(accepted) + len(rejected) == MAX_TOOL_CALLS + 1
    assert rejected[-1]["reason"] == f"too many tool calls ({5_001 - MAX_TOOL_CALLS} more ignored)"


def test_exactly_the_cap_produces_no_overflow_record() -> None:
    calls: list[tuple[str, object]] = [("junk", {})] * MAX_TOOL_CALLS
    _, rejected = collect_tool_calls(calls, offered_tools(context()))
    assert [r["tool_name"] for r in rejected] == ["junk"] * MAX_TOOL_CALLS


def test_collect_dedupes_skips_completed_and_resolves_no_action_conflicts() -> None:
    calls: list[tuple[str, object]] = [
        ("propose_azure_devops_work_item", WORK_ITEM),
        ("propose_azure_devops_work_item", WORK_ITEM),
        ("propose_follow_up_task", TASK),
        ("propose_no_action", NO_ACTION),
        ("propose_email_reply", REPLY),
    ]
    accepted, rejected = collect_tool_calls(
        calls, offered_tools(context()), completed={ActionType.CREATE_FOLLOW_UP_TASK}
    )
    assert [c.tool_name for c in accepted] == ["propose_azure_devops_work_item", "propose_email_reply"]
    assert rejected == [
        {"tool_name": "propose_azure_devops_work_item", "reason": "duplicate proposal (one per action type)"},
        {"tool_name": "propose_follow_up_task", "reason": "action already completed for this email"},
        {"tool_name": "propose_no_action", "reason": "conflicts with other proposals"},
    ]


def test_no_action_alone_is_kept() -> None:
    accepted, rejected = collect_tool_calls([("propose_no_action", NO_ACTION)], offered_tools(context()))
    assert [c.action_type for c in accepted] == [ActionType.MARK_NO_ACTION]
    assert rejected == []
    assert collect_tool_calls([], offered_tools(context())) == ([], [])
