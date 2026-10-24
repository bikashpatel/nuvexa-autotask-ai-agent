from __future__ import annotations

import uuid

from app.core.timeutil import utcnow
from app.db.models import EmailMessage
from app.domain.enums import ActionType, ConfidenceBucket, EmailCategory, Priority, Urgency
from app.schemas.ai import (
    ClassificationOutput,
    PlannedToolCall,
    ProposedField,
    ValidatedAnalysis,
    ValidatedExtraction,
)
from app.schemas.settings import TenantSettings
from app.services.action_policy import ActionPolicy, idempotency_key
from tests.conftest import make_settings


def _email() -> EmailMessage:
    return EmailMessage(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        subject="Production login failures",
        sender_name="Dana Ops",
        sender_address="dana@example.com",
        internet_message_id="<abc@example.com>",
        received_at=utcnow(),
        body_text="Production users are unable to log in.",
    )


def _analysis(
    email: EmailMessage, *, owner: str | None = None, fields: list[ProposedField] | None = None
) -> ValidatedAnalysis:
    return ValidatedAnalysis(
        classification=ClassificationOutput(
            category=EmailCategory.INCIDENT,
            rationale="Production access is disrupted.",
            urgency=Urgency.IMMEDIATE,
            priority=Priority.HIGH,
            action_recommended=True,
            needs_more_info=False,
            missing_information=[],
            extraction_warnings=[],
            confidence=0.9,
        ),
        extraction=ValidatedExtraction(
            source_email_id=email.id,
            summary="Investigate production login failures",
            description="Users cannot log in.",
            suggested_owner=owner,
            proposed_fields=fields or [],
        ),
        confidence_score=0.9,
        confidence_bucket=ConfidenceBucket.HIGH,
    )


def _wi_call(**overrides: object) -> PlannedToolCall:
    args = {
        "title": "Investigate production login failures after deployment",
        "description": "Users cannot log in to the customer portal following the latest deployment.",
        "work_item_type": "Bug",
        "priority": "high",
        "tags": ["login", "production"],
        "rationale": "Production access is disrupted.",
        "expected_impact": "Restores login.",
    }
    args.update(overrides)
    return PlannedToolCall(
        tool_name="propose_azure_devops_work_item", action_type=ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM, arguments=args
    )


def _policy(**ado: object) -> ActionPolicy:
    ts = TenantSettings.model_validate({"azure_devops": {"project": "NUVEXA-Demo", **ado}})
    return ActionPolicy(ts, make_settings("sqlite+aiosqlite://"))


def test_project_and_assignee_come_from_configuration_not_the_model() -> None:
    email = _email()
    [spec] = _policy().build_proposals(email, _analysis(email, owner="the CEO"), [_wi_call()])
    assert spec.payload["project"] == "NUVEXA-Demo"
    assert spec.payload["assigned_to"] is None
    assert spec.payload["priority"] == 1  # high → ADO priority 1 by default mapping
    assert any("owner" in w for w in spec.warnings)
    assert not spec.errors


def test_disallowed_work_item_type_falls_back_with_warning() -> None:
    email = _email()
    [spec] = _policy().build_proposals(email, _analysis(email), [_wi_call(work_item_type="Delete Everything")])
    assert spec.payload["work_item_type"] == "Bug"
    assert any("not allowed" in w for w in spec.warnings)


def test_unlisted_fields_are_dropped() -> None:
    email = _email()
    fields = [
        ProposedField(name="System.AssignedTo", value="ceo@example.com"),
        ProposedField(name="Microsoft.VSTS.Common.Severity", value="1 - Critical"),
    ]
    [spec] = _policy().build_proposals(email, _analysis(email, fields=fields), [_wi_call()])
    assert spec.payload["additional_fields"] == {"Microsoft.VSTS.Common.Severity": "1 - Critical"}


def test_missing_project_is_a_blocking_validation_error() -> None:
    email = _email()
    ts = TenantSettings()
    policy = ActionPolicy(ts, make_settings("sqlite+aiosqlite://", azure_devops_default_project=None))
    [spec] = policy.build_proposals(email, _analysis(email), [_wi_call()])
    assert spec.errors


def test_reply_is_forced_to_original_sender() -> None:
    email = _email()
    call = PlannedToolCall(
        tool_name="propose_email_reply",
        action_type=ActionType.PROPOSE_EMAIL_REPLY,
        arguments={
            "subject": "Re: Production login failures",
            "body": "We are investigating.",
            "rationale": "Acknowledge.",
            "expected_impact": "Requester informed.",
        },
    )
    [spec] = _policy().build_proposals(email, _analysis(email), [call])
    assert spec.payload["to"] == ["dana@example.com"]


def test_no_action_is_exclusive_and_invalid_calls_fall_back() -> None:
    email = _email()
    no_action = PlannedToolCall(
        tool_name="propose_no_action",
        action_type=ActionType.MARK_NO_ACTION,
        arguments={"reason": "FYI", "rationale": "Nothing to do.", "expected_impact": "None."},
    )
    specs = _policy().build_proposals(email, _analysis(email), [_wi_call(), no_action])
    assert [s.action_type for s in specs] == [ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM]

    bad = PlannedToolCall(
        tool_name="propose_azure_devops_work_item",
        action_type=ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM,
        arguments={"title": 123},
    )
    [fallback] = _policy().build_proposals(email, _analysis(email), [bad])
    assert fallback.action_type is ActionType.MARK_NO_ACTION
    assert any("rejected" in w for w in fallback.warnings)


def test_idempotency_key_is_stable_per_email_and_type() -> None:
    t, e = uuid.uuid4(), uuid.uuid4()
    assert idempotency_key(t, e, ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM) == idempotency_key(
        t, e, ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM
    )
    assert idempotency_key(t, e, ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM) != idempotency_key(
        t, e, ActionType.PROPOSE_EMAIL_REPLY
    )
