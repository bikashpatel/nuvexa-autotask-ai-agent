"""Planning-tool helpers shared by all agents: which tools are offered and how tool calls are validated.

Tool calls are proposals only. Validation here is schema-level; business policy (allowed projects, fields,
recipients) is enforced later by `app.services.action_policy`.
"""

from __future__ import annotations

import re
from typing import Any, cast, get_args

from pydantic import BaseModel, ValidationError

from app.domain.enums import ActionType
from app.schemas.ai import PLANNING_TOOLS, PlannedToolCall, PlanningContext

ToolRegistry = dict[str, tuple[ActionType, type[BaseModel]]]

MAX_TOOL_CALLS = 6
OVERFLOW_TOOL_NAME = "(overflow)"  # rejection record summarizing the calls beyond MAX_TOOL_CALLS
_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]")
_KNOWN_TOOL_NAMES = frozenset(get_args(PlannedToolCall.model_fields["tool_name"].annotation))


def offered_tools(context: PlanningContext) -> ToolRegistry:
    """Tools whose action type is allowed by the tenant (Azure DevOps only when available)."""
    allowed = set(context.allowed_action_types)
    offered: ToolRegistry = {}
    for name, (action_type, model) in PLANNING_TOOLS.items():
        if action_type not in allowed:
            continue
        if action_type is ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM and not context.azure_devops_available:
            continue
        offered[name] = (action_type, model)
    return offered


def completed_action_types(summaries: list[str]) -> set[ActionType]:
    """Parse `already_completed_actions` entries of the form "<action_type>: <title>"."""
    found: set[ActionType] = set()
    for summary in summaries:
        head = summary.split(":", 1)[0].strip().lower()
        for action_type in ActionType:
            if head == action_type.value:
                found.add(action_type)
        for name, (action_type, _) in PLANNING_TOOLS.items():
            if head == name:
                found.add(action_type)
    return found


def safe_tool_name(name: object) -> str:
    return _NAME_SAFE_RE.sub("", str(name))[:64] or "(empty)"


def _error_summary(exc: ValidationError) -> str:
    # Field locations and error types only: input values may contain email content.
    parts = []
    for error in exc.errors()[:5]:
        loc = ".".join(str(p) for p in error.get("loc", ())) or "arguments"
        parts.append(f"{loc}: {error.get('type', 'invalid')}")
    return "; ".join(parts)


def validate_tool_call(name: str, arguments: object, offered: ToolRegistry) -> PlannedToolCall | dict[str, str]:
    """Validate one tool call. Returns a `PlannedToolCall` or a sanitized rejection record."""
    tool_name = safe_tool_name(name)
    if name not in offered or name not in _KNOWN_TOOL_NAMES:
        return {"tool_name": tool_name, "reason": "unknown or unavailable tool"}
    action_type, model = offered[name]
    try:
        if isinstance(arguments, model):
            parsed = model.model_validate(arguments.model_dump())
        elif isinstance(arguments, str | bytes):
            parsed = model.model_validate_json(arguments)
        else:
            parsed = model.model_validate(arguments)
    except ValidationError as exc:
        return {"tool_name": tool_name, "reason": f"invalid arguments ({_error_summary(exc)})"[:300]}
    except ValueError, TypeError:
        return {"tool_name": tool_name, "reason": "invalid arguments (not a JSON object)"}
    tool_literal = cast(Any, name)
    return PlannedToolCall(tool_name=tool_literal, action_type=action_type, arguments=parsed.model_dump(mode="json"))


def collect_tool_calls(
    calls: list[tuple[str, object]], offered: ToolRegistry, *, completed: set[ActionType] | None = None
) -> tuple[list[PlannedToolCall], list[dict[str, str]]]:
    """Validate a batch of calls: one per tool, no proposals for completed actions, no-action only alone.

    Only the first MAX_TOOL_CALLS calls are considered; any beyond are summarized in one rejection record, so the
    result (and the workflow timeline built from it) stays bounded however many calls the model makes.
    """
    accepted: list[PlannedToolCall] = []
    rejected: list[dict[str, str]] = []
    completed = completed or set()
    for name, arguments in calls[:MAX_TOOL_CALLS]:
        result = validate_tool_call(name, arguments, offered)
        if isinstance(result, dict):
            rejected.append(result)
        elif any(c.tool_name == result.tool_name for c in accepted):
            rejected.append({"tool_name": result.tool_name, "reason": "duplicate proposal (one per action type)"})
        elif result.action_type in completed:
            rejected.append({"tool_name": result.tool_name, "reason": "action already completed for this email"})
        else:
            accepted.append(result)
    if len(calls) > MAX_TOOL_CALLS:
        ignored = len(calls) - MAX_TOOL_CALLS
        rejected.append({"tool_name": OVERFLOW_TOOL_NAME, "reason": f"too many tool calls ({ignored} more ignored)"})
    if len(accepted) > 1 and any(c.action_type is ActionType.MARK_NO_ACTION for c in accepted):
        accepted = [c for c in accepted if c.action_type is not ActionType.MARK_NO_ACTION]
        rejected.append({"tool_name": "propose_no_action", "reason": "conflicts with other proposals"})
    return accepted, rejected
