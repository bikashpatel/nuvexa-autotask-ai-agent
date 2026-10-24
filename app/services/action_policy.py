"""Deterministic policy: turns LLM proposal-tool calls into validated, executable payloads.

The model never chooses the Azure DevOps organization, project, area/iteration path, assignee or
reply recipients — those come from configuration and the source email. Everything else it suggests
is validated against the payload schemas and tenant allow-lists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import sha256_hex
from app.db.models import EmailMessage
from app.domain.enums import ActionType, Priority
from app.schemas.actions import (
    AzureDevOpsWorkItemPayload,
    EmailReplyDraftPayload,
    FollowUpTaskPayload,
    NoActionPayload,
    SourceEmailReference,
    parse_payload,
)
from app.schemas.ai import (
    PlannedToolCall,
    ProposeAzureDevOpsWorkItemArgs,
    ProposeEmailReplyArgs,
    ProposeFollowUpTaskArgs,
    ProposeNoActionArgs,
    ValidatedAnalysis,
)
from app.schemas.settings import TenantSettings

AnyPayload = AzureDevOpsWorkItemPayload | EmailReplyDraftPayload | FollowUpTaskPayload | NoActionPayload

_ACTION_ORDER = [
    ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM,
    ActionType.CREATE_FOLLOW_UP_TASK,
    ActionType.PROPOSE_EMAIL_REPLY,
    ActionType.MARK_NO_ACTION,
]


@dataclass
class ProposalSpec:
    action_type: ActionType
    payload: dict[str, Any]
    title: str
    rationale: str
    expected_impact: str
    slot: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def idempotency_key(tenant_id: uuid.UUID, email_id: uuid.UUID, action_type: ActionType, slot: int = 0) -> str:
    """Stable per (email, action type, slot): re-analysis of the same email maps to the same key."""
    return sha256_hex(f"{tenant_id}:{email_id}:{action_type.value}:{slot}")


def source_reference(email: EmailMessage) -> SourceEmailReference:
    return SourceEmailReference(
        email_id=email.id,
        internet_message_id=(email.internet_message_id or None),
        subject=(email.subject or "")[:998],
        sender=email.sender_address,
        received_at=email.received_at,
    )


def payload_title(payload: AnyPayload) -> str:
    match payload:
        case AzureDevOpsWorkItemPayload():
            return f"{payload.work_item_type}: {payload.title}"[:255]
        case EmailReplyDraftPayload():
            return f"Reply draft: {payload.subject}"[:255]
        case FollowUpTaskPayload():
            return f"Follow-up: {payload.title}"[:255]
        case NoActionPayload():
            return "Mark as no action required"


def _clip(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _parse_iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class ActionPolicy:
    def __init__(self, tenant_settings: TenantSettings, settings: Settings) -> None:
        self.ts = tenant_settings
        self.settings = settings

    # ------------------------------------------------------------------ building proposals

    def build_proposals(
        self, email: EmailMessage, analysis: ValidatedAnalysis, tool_calls: list[PlannedToolCall]
    ) -> list[ProposalSpec]:
        specs: dict[ActionType, ProposalSpec] = {}
        dropped: list[str] = []
        for call in tool_calls:
            if call.action_type in specs:
                dropped.append(f"Duplicate '{call.tool_name}' proposal ignored (one per action type).")
                continue
            try:
                spec = self._build_one(email, analysis, call)
            except ValidationError as exc:
                dropped.append(f"Proposal '{call.tool_name}' rejected: {_first_error(exc)}")
                continue
            if spec is not None:
                specs[call.action_type] = spec
        if len(specs) > 1:
            specs.pop(ActionType.MARK_NO_ACTION, None)  # "no action" is exclusive
        if not specs:
            reason = "The agent did not propose a valid action. Review the email and confirm no action is needed."
            spec = self._no_action(email, reason, "Fallback proposal created by policy.", "Email is closed.")
            spec.warnings.extend(dropped)
            return [spec]
        ordered = [specs[t] for t in _ACTION_ORDER if t in specs]
        ordered[0].warnings.extend(dropped)
        return ordered

    def _build_one(
        self, email: EmailMessage, analysis: ValidatedAnalysis, call: PlannedToolCall
    ) -> ProposalSpec | None:
        match call.action_type:
            case ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM:
                return self._work_item(email, analysis, ProposeAzureDevOpsWorkItemArgs.model_validate(call.arguments))
            case ActionType.PROPOSE_EMAIL_REPLY:
                return self._reply(email, ProposeEmailReplyArgs.model_validate(call.arguments))
            case ActionType.CREATE_FOLLOW_UP_TASK:
                return self._follow_up(email, analysis, ProposeFollowUpTaskArgs.model_validate(call.arguments))
            case ActionType.MARK_NO_ACTION:
                args = ProposeNoActionArgs.model_validate(call.arguments)
                return self._no_action(email, args.reason, args.rationale, args.expected_impact)

    def _work_item(
        self, email: EmailMessage, analysis: ValidatedAnalysis, args: ProposeAzureDevOpsWorkItemArgs
    ) -> ProposalSpec:
        ado = self.ts.azure_devops
        warnings: list[str] = []
        category = analysis.classification.category
        wit = args.work_item_type.strip()
        if wit not in ado.allowed_work_item_types:
            fallback = ado.category_work_item_types.get(category) or ado.work_item_type
            warnings.append(f"Work item type '{_clip(wit, 40)}' is not allowed; using '{fallback}'.")
            wit = fallback
        priority = self.ts.priority.azure_devops_priority.get(args.priority, 3)
        tags = [t for t in (tag.strip() for tag in args.tags[:5]) if t and ";" not in t and len(t) <= 64]
        tags.append(f"category-{category.value.replace('_', '-')}")
        extra: dict[str, str | int | float] = {}
        for pf in analysis.extraction.proposed_fields:
            if pf.name in ado.allowed_additional_fields:
                extra[pf.name] = _clip(pf.value, 4000)
            else:
                warnings.append(f"Field '{_clip(pf.name, 80)}' is not in the allowed field list and was dropped.")
        if analysis.extraction.suggested_owner:
            warnings.append(
                f"Email mentions '{_clip(analysis.extraction.suggested_owner, 80)}' as owner; assignment is left "
                "empty because ownership cannot be verified automatically."
            )
        payload = {
            "kind": "create_azure_devops_work_item",
            "project": ado.project or self.settings.azure_devops_default_project or "",
            "work_item_type": wit,
            "title": _clip(args.title or analysis.extraction.summary, 255),
            "description": _clip(args.description, 32_000),
            "priority": priority,
            "area_path": ado.area_path,
            "iteration_path": ado.iteration_path,
            "tags": tags,
            "assigned_to": None,
            "additional_fields": extra,
            "source_email": source_reference(email).model_dump(mode="json"),
        }
        spec = ProposalSpec(
            ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM,
            payload,
            "",
            _clip(args.rationale, 2000),
            _clip(args.expected_impact, 2000),
            warnings=warnings,
        )
        return self._finalize(spec)

    def _reply(self, email: EmailMessage, args: ProposeEmailReplyArgs) -> ProposalSpec | None:
        if not email.sender_address:
            return None
        subject = args.subject.strip() or f"Re: {email.subject}"
        payload = {
            "kind": "propose_email_reply",
            "to": [email.sender_address],  # forced: replies only ever go back to the original sender
            "subject": _clip(subject, 255),
            "body": _clip(args.body, 20_000),
            "in_reply_to_internet_message_id": email.internet_message_id,
            "source_email": source_reference(email).model_dump(mode="json"),
        }
        spec = ProposalSpec(
            ActionType.PROPOSE_EMAIL_REPLY,
            payload,
            "",
            _clip(args.rationale, 2000),
            _clip(args.expected_impact, 2000),
            warnings=["Reply drafts are stored in NUVEXA and are never sent automatically."],
        )
        return self._finalize(spec)

    def _follow_up(
        self, email: EmailMessage, analysis: ValidatedAnalysis, args: ProposeFollowUpTaskArgs
    ) -> ProposalSpec:
        warnings: list[str] = []
        due = _parse_iso_date(args.due_date)
        if due is not None and analysis.extraction.deadline is None:
            warnings.append("Due date was dropped because the email does not state a deadline.")
            due = None
        payload = {
            "kind": "create_follow_up_task",
            "title": _clip(args.title, 255),
            "description": _clip(args.description, 10_000),
            "due_at": due.isoformat() if due else None,
            "source_email": source_reference(email).model_dump(mode="json"),
        }
        spec = ProposalSpec(
            ActionType.CREATE_FOLLOW_UP_TASK,
            payload,
            "",
            _clip(args.rationale, 2000),
            _clip(args.expected_impact, 2000),
            warnings=warnings,
        )
        return self._finalize(spec)

    def _no_action(self, email: EmailMessage, reason: str, rationale: str, impact: str) -> ProposalSpec:
        payload = {
            "kind": "mark_no_action",
            "reason": _clip(reason or "No action required.", 1000),
            "source_email": source_reference(email).model_dump(mode="json"),
        }
        spec = ProposalSpec(ActionType.MARK_NO_ACTION, payload, "", _clip(rationale, 2000), _clip(impact, 2000))
        return self._finalize(spec)

    def _finalize(self, spec: ProposalSpec) -> ProposalSpec:
        try:
            parsed = parse_payload(spec.payload)
        except ValidationError as exc:
            spec.errors.append(f"Payload is invalid: {_first_error(exc)}")
            spec.title = _clip(str(spec.payload.get("title") or spec.action_type.value), 255)
            return spec
        spec.payload = parsed.model_dump(mode="json")
        spec.title = payload_title(parsed)
        spec.errors.extend(self.policy_issues(spec.action_type, parsed))
        return spec

    # ------------------------------------------------------------------ validation (edit/approve/execute)

    def validate_payload(self, action_type: ActionType, data: dict[str, Any]) -> AnyPayload:
        """Schema validation; raises pydantic.ValidationError. Kind must match the action type."""
        parsed = parse_payload(data)
        if parsed.kind != action_type.value:
            raise ValueError("Payload kind does not match the action type.")
        return parsed

    def policy_issues(
        self, action_type: ActionType, payload: AnyPayload, email: EmailMessage | None = None
    ) -> list[str]:
        issues: list[str] = []
        ado = self.ts.azure_devops
        match payload:
            case AzureDevOpsWorkItemPayload():
                if not payload.project:
                    issues.append("No Azure DevOps project is configured. Set a default project in Settings.")
                elif ado.allowed_projects and payload.project not in ado.allowed_projects:
                    issues.append(f"Project '{payload.project}' is not in the allowed project list.")
                if payload.work_item_type not in ado.allowed_work_item_types:
                    issues.append(f"Work item type '{payload.work_item_type}' is not allowed.")
                for name in payload.additional_fields:
                    if name not in ado.allowed_additional_fields:
                        issues.append(f"Field '{name}' is not in the allowed field list.")
            case EmailReplyDraftPayload():
                if email is not None and [str(a).lower() for a in payload.to] != [(email.sender_address or "").lower()]:
                    issues.append("Reply recipients must be exactly the original sender.")
            case FollowUpTaskPayload() | NoActionPayload():
                pass
        if action_type.value != payload.kind:
            issues.append("Payload kind does not match the action type.")
        return issues


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    return f"{loc}: {err.get('msg', 'invalid')}" if loc else str(err.get("msg", "invalid"))


__all__ = ["ActionPolicy", "AnyPayload", "Priority", "ProposalSpec", "idempotency_key", "payload_title"]
