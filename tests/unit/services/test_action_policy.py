from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st
from pydantic import ValidationError

from app.db.models import EmailMessage
from app.domain.enums import ActionType, ConfidenceBucket, EmailCategory, Priority, Urgency
from app.schemas.actions import (
    AzureDevOpsWorkItemPayload,
    EmailReplyDraftPayload,
    FollowUpTaskPayload,
    NoActionPayload,
    SourceEmailReference,
    parse_payload,
)
from app.schemas.ai import ClassificationOutput, PlannedToolCall, ProposedField, ValidatedAnalysis, ValidatedExtraction
from app.schemas.settings import TenantSettings
from app.services.action_policy import (
    ActionPolicy,
    _clip,
    _first_error,
    _parse_iso_date,
    idempotency_key,
    payload_title,
    source_reference,
)
from tests.conftest import make_settings
from tests.unit.services.conftest import FOLLOW_UP, NO_ACTION, REPLY, WI

EMAIL_ID = uuid.UUID("00000000-0000-4000-8000-000000000042")
TENANT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
RECEIVED = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)

TOOL_NAMES = {
    WI: "propose_azure_devops_work_item",
    REPLY: "propose_email_reply",
    FOLLOW_UP: "propose_follow_up_task",
    NO_ACTION: "propose_no_action",
}


def _email(**kw: Any) -> EmailMessage:
    values: dict[str, Any] = {
        "id": EMAIL_ID,
        "tenant_id": TENANT_ID,
        "subject": "Production login failures",
        "sender_name": "Dana Ops",
        "sender_address": "dana@example.com",
        "internet_message_id": "<abc@example.com>",
        "received_at": RECEIVED,
        "body_text": "Production users are unable to log in.",
    }
    values.update(kw)
    return EmailMessage(**values)


def _analysis(
    *,
    category: EmailCategory = EmailCategory.INCIDENT,
    owner: str | None = None,
    fields: list[ProposedField] | None = None,
    deadline: datetime | None = None,
    summary: str = "Investigate production login failures",
) -> ValidatedAnalysis:
    return ValidatedAnalysis(
        classification=ClassificationOutput(
            category=category,
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
            source_email_id=EMAIL_ID,
            summary=summary,
            description="Users cannot log in.",
            suggested_owner=owner,
            deadline=deadline,
            proposed_fields=fields or [],
        ),
        confidence_score=0.9,
        confidence_bucket=ConfidenceBucket.HIGH,
    )


def _call(action_type: ActionType, **arguments: Any) -> PlannedToolCall:
    defaults: dict[ActionType, dict[str, Any]] = {
        WI: {
            "title": "Investigate production login failures",
            "description": "Users cannot log in to the portal.",
            "work_item_type": "Bug",
            "priority": "high",
            "tags": ["login", "production"],
            "rationale": "Production access is disrupted.",
            "expected_impact": "Restores login.",
        },
        REPLY: {
            "subject": "Re: Production login failures",
            "body": "We are investigating.",
            "rationale": "Acknowledge.",
            "expected_impact": "Requester informed.",
        },
        FOLLOW_UP: {
            "title": "Confirm the fix",
            "description": "Check with Dana.",
            "due_date": None,
            "rationale": "Close the loop.",
            "expected_impact": "Customer confirmed.",
        },
        NO_ACTION: {"reason": "FYI only", "rationale": "Nothing to do.", "expected_impact": "Email closed."},
    }
    return PlannedToolCall(
        tool_name=TOOL_NAMES[action_type],  # type: ignore[arg-type]
        action_type=action_type,
        arguments={**defaults[action_type], **arguments},
    )


def _policy(ado: dict[str, Any] | None = None, **ts: Any) -> ActionPolicy:
    tenant = TenantSettings.model_validate({"azure_devops": {"project": "NUVEXA-Demo", **(ado or {})}, **ts})
    return ActionPolicy(tenant, make_settings("sqlite+aiosqlite://"))


def _source() -> dict[str, Any]:
    return source_reference(_email()).model_dump(mode="json")


def _wi_payload(**overrides: Any) -> AzureDevOpsWorkItemPayload:
    data: dict[str, Any] = {
        "project": "NUVEXA-Demo",
        "work_item_type": "Bug",
        "title": "Investigate",
        "priority": 2,
        "source_email": _source(),
    }
    data.update(overrides)
    return AzureDevOpsWorkItemPayload.model_validate(data)


# ---------------------------------------------------------------------------------------------
# Idempotency keys
# ---------------------------------------------------------------------------------------------

_KEY_INPUTS = st.tuples(st.uuids(), st.uuids(), st.sampled_from(list(ActionType)), st.integers(0, 50))


@hsettings(max_examples=200, derandomize=True, database=None)
@given(_KEY_INPUTS)
def test_idempotency_key_is_stable_and_hex(inputs: tuple[uuid.UUID, uuid.UUID, ActionType, int]) -> None:
    key = idempotency_key(*inputs)
    assert key == idempotency_key(*inputs)
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


@hsettings(max_examples=200, derandomize=True, database=None)
@given(_KEY_INPUTS, _KEY_INPUTS)
def test_idempotency_key_differs_for_different_inputs(
    a: tuple[uuid.UUID, uuid.UUID, ActionType, int], b: tuple[uuid.UUID, uuid.UUID, ActionType, int]
) -> None:
    assert (idempotency_key(*a) == idempotency_key(*b)) is (a == b)


def test_idempotency_key_default_slot_is_zero() -> None:
    assert idempotency_key(TENANT_ID, EMAIL_ID, WI) == idempotency_key(TENANT_ID, EMAIL_ID, WI, 0)
    assert idempotency_key(TENANT_ID, EMAIL_ID, WI) != idempotency_key(TENANT_ID, EMAIL_ID, WI, 1)


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def test_source_reference_maps_provenance() -> None:
    ref = source_reference(_email())
    assert ref == SourceEmailReference(
        email_id=EMAIL_ID,
        internet_message_id="<abc@example.com>",
        subject="Production login failures",
        sender="dana@example.com",
        received_at=RECEIVED,
    )


def test_source_reference_normalizes_missing_and_long_values() -> None:
    ref = source_reference(_email(internet_message_id="", subject="x" * 1500, sender_address=None))
    assert ref.internet_message_id is None
    assert ref.subject == "x" * 998
    assert ref.sender is None
    assert source_reference(_email(subject=None)).subject == ""


def test_payload_title_for_every_payload_type() -> None:
    source = _source()
    assert payload_title(_wi_payload(title="Fix login", work_item_type="Task")) == "Task: Fix login"
    reply = EmailReplyDraftPayload(to=["dana@example.com"], subject="Re: hi", body="b", source_email=source)  # type: ignore[arg-type,list-item]
    assert payload_title(reply) == "Reply draft: Re: hi"
    task = FollowUpTaskPayload(title="Ping Dana", source_email=source)  # type: ignore[arg-type]
    assert payload_title(task) == "Follow-up: Ping Dana"
    assert payload_title(NoActionPayload(reason="FYI", source_email=source)) == "Mark as no action required"  # type: ignore[arg-type]


def test_payload_title_is_capped_at_255_characters() -> None:
    title = payload_title(_wi_payload(title="t" * 255, work_item_type="User Story"))
    assert len(title) == 255
    assert title.startswith("User Story: ttt")


@pytest.mark.parametrize(
    ("value", "limit", "expected"),
    [
        ("  short  ", 10, "short"),
        ("exactly10!", 10, "exactly10!"),
        ("this is far too long", 10, "this is f…"),
        ("trailing   spaces here", 12, "trailing…"),
        ("abcdefghijk", 5, "abcd…"),
    ],
)
def test_clip(value: str, limit: int, expected: str) -> None:
    assert _clip(value, limit) == expected
    assert len(_clip(value, limit)) <= limit


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("next tuesday", None),
        ("2026-11-01", datetime(2026, 11, 1, tzinfo=UTC)),
        ("2026-11-01T17:00:00+02:00", datetime(2026, 11, 1, 17, tzinfo=timezone(timedelta(hours=2)))),
    ],
)
def test_parse_iso_date(value: str | None, expected: datetime | None) -> None:
    parsed = _parse_iso_date(value)
    assert parsed == expected
    if parsed is not None:
        assert parsed.tzinfo is not None


def test_first_error_with_and_without_location() -> None:
    with pytest.raises(ValidationError) as located:
        _wi_payload(priority=9)
    assert _first_error(located.value).startswith("priority: ")
    with pytest.raises(ValidationError) as unlocated:
        parse_payload({})  # discriminator missing → error without a location
    assert _first_error(unlocated.value) == "Unable to extract tag using discriminator 'kind'"


# ---------------------------------------------------------------------------------------------
# build_proposals: selection, ordering, fallbacks
# ---------------------------------------------------------------------------------------------


def test_proposals_are_ordered_and_no_action_is_exclusive() -> None:
    calls = [_call(NO_ACTION), _call(REPLY), _call(FOLLOW_UP), _call(WI)]
    specs = _policy().build_proposals(_email(), _analysis(), calls)
    assert [s.action_type for s in specs] == [WI, FOLLOW_UP, REPLY]
    assert all(not s.errors for s in specs)
    assert [s.slot for s in specs] == [0, 0, 0]


def test_duplicate_tool_calls_keep_the_first_and_warn() -> None:
    specs = _policy().build_proposals(
        _email(), _analysis(), [_call(WI, title="First"), _call(REPLY), _call(WI, title="Second")]
    )
    assert [s.action_type for s in specs] == [WI, REPLY]
    assert specs[0].payload["title"] == "First"
    assert "Duplicate 'propose_azure_devops_work_item' proposal ignored (one per action type)." in specs[0].warnings
    assert not any("Duplicate" in w for w in specs[1].warnings)


def test_invalid_arguments_are_dropped_with_reason_on_the_first_proposal() -> None:
    bad = PlannedToolCall(tool_name="propose_follow_up_task", action_type=FOLLOW_UP, arguments={"title": 1})
    specs = _policy().build_proposals(_email(), _analysis(), [bad, _call(REPLY)])
    assert [s.action_type for s in specs] == [REPLY]
    assert any(w.startswith("Proposal 'propose_follow_up_task' rejected: ") for w in specs[0].warnings)


def test_no_valid_proposal_falls_back_to_no_action_with_warnings() -> None:
    bad = PlannedToolCall(tool_name="propose_azure_devops_work_item", action_type=WI, arguments={"title": 123})
    [spec] = _policy().build_proposals(_email(), _analysis(), [bad])
    assert spec.action_type is NO_ACTION
    assert spec.payload["reason"].startswith("The agent did not propose a valid action.")
    assert spec.rationale == "Fallback proposal created by policy."
    assert spec.expected_impact == "Email is closed."
    assert spec.title == "Mark as no action required"
    assert not spec.errors
    assert len(spec.warnings) == 1
    assert "rejected" in spec.warnings[0]


def test_empty_plan_falls_back_to_no_action() -> None:
    [spec] = _policy().build_proposals(_email(), _analysis(), [])
    assert spec.action_type is NO_ACTION
    assert spec.warnings == []


def test_reply_without_sender_is_skipped() -> None:
    email = _email(sender_address=None)
    [spec] = _policy().build_proposals(email, _analysis(), [_call(REPLY)])
    assert spec.action_type is NO_ACTION  # nothing to reply to → fallback
    specs = _policy().build_proposals(email, _analysis(), [_call(REPLY), _call(FOLLOW_UP)])
    assert [s.action_type for s in specs] == [FOLLOW_UP]


def test_single_no_action_proposal_is_kept() -> None:
    [spec] = _policy().build_proposals(_email(), _analysis(), [_call(NO_ACTION, reason="Newsletter")])
    assert spec.payload["kind"] == "mark_no_action"
    assert spec.payload["reason"] == "Newsletter"
    assert spec.rationale == "Nothing to do."
    assert spec.payload["source_email"]["email_id"] == str(EMAIL_ID)


def test_empty_no_action_reason_gets_a_default() -> None:
    [spec] = _policy().build_proposals(_email(), _analysis(), [_call(NO_ACTION, reason="")])
    assert spec.payload["reason"] == "No action required."
    assert not spec.errors


# ---------------------------------------------------------------------------------------------
# Work items
# ---------------------------------------------------------------------------------------------


def test_work_item_payload_is_built_from_configuration() -> None:
    policy = _policy({"area_path": "Portal\\Auth", "iteration_path": "Portal\\Sprint 1"})
    [spec] = policy.build_proposals(_email(), _analysis(owner="the CEO"), [_call(WI)])
    payload = spec.payload
    assert payload["kind"] == "create_azure_devops_work_item"
    assert payload["project"] == "NUVEXA-Demo"
    assert payload["area_path"] == "Portal\\Auth"
    assert payload["iteration_path"] == "Portal\\Sprint 1"
    assert payload["assigned_to"] is None
    assert payload["priority"] == 1
    assert payload["tags"] == ["login", "production", "category-incident"]
    assert payload["source_email"] == _source()
    assert spec.title == "Bug: Investigate production login failures"
    assert spec.rationale == "Production access is disrupted."
    assert spec.expected_impact == "Restores login."
    assert any("'the CEO' as owner" in w for w in spec.warnings)
    assert not spec.errors


def test_disallowed_type_uses_category_mapping_then_default() -> None:
    [incident] = _policy().build_proposals(_email(), _analysis(), [_call(WI, work_item_type="Delete Everything")])
    assert incident.payload["work_item_type"] == "Bug"
    assert "Work item type 'Delete Everything' is not allowed; using 'Bug'." in incident.warnings

    [other] = _policy({"work_item_type": "Issue"}).build_proposals(
        _email(), _analysis(category=EmailCategory.OTHER), [_call(WI, work_item_type="  Spike  ")]
    )
    assert other.payload["work_item_type"] == "Issue"  # OTHER has no category mapping → tenant default
    assert "Work item type 'Spike' is not allowed; using 'Issue'." in other.warnings


def test_allowed_type_is_kept_after_trimming() -> None:
    [spec] = _policy().build_proposals(_email(), _analysis(), [_call(WI, work_item_type=" Task ")])
    assert spec.payload["work_item_type"] == "Task"
    assert not any("not allowed" in w for w in spec.warnings)


def test_tags_are_limited_filtered_and_tagged_with_category() -> None:
    noisy = ["ok", "", "   ", "a;b", "x" * 65, "never-seen"]
    [spec] = _policy().build_proposals(
        _email(), _analysis(category=EmailCategory.SERVICE_REQUEST), [_call(WI, tags=noisy)]
    )
    assert spec.payload["tags"] == ["ok", "category-service-request"]

    [six] = _policy().build_proposals(_email(), _analysis(), [_call(WI, tags=["a", "b", "c", "d", "e", "f"])])
    assert six.payload["tags"] == ["a", "b", "c", "d", "e", "category-incident"]


def test_priority_uses_tenant_mapping() -> None:
    mapping = {"critical": 1, "high": 2, "medium": 3, "low": 4}
    policy = _policy(priority={"azure_devops_priority": mapping})
    for level, expected in mapping.items():
        [spec] = policy.build_proposals(_email(), _analysis(), [_call(WI, priority=level)])
        assert spec.payload["priority"] == expected


def test_title_falls_back_to_summary_and_is_clipped() -> None:
    [fallback] = _policy().build_proposals(_email(), _analysis(summary="Summary title"), [_call(WI, title="")])
    assert fallback.payload["title"] == "Summary title"
    [long] = _policy().build_proposals(_email(), _analysis(), [_call(WI, title="w" * 400)])
    assert len(long.payload["title"]) == 255
    assert long.payload["title"].endswith("…")


def test_additional_fields_respect_allow_list() -> None:
    fields = [
        ProposedField(name="System.AssignedTo", value="ceo@example.com"),
        ProposedField(name="Microsoft.VSTS.Common.Severity", value="1 - Critical"),
        ProposedField(name="Microsoft.VSTS.TCM.ReproSteps", value="s" * 5000),
    ]
    [spec] = _policy().build_proposals(_email(), _analysis(fields=fields), [_call(WI)])
    extra = spec.payload["additional_fields"]
    assert set(extra) == {"Microsoft.VSTS.Common.Severity", "Microsoft.VSTS.TCM.ReproSteps"}
    assert len(extra["Microsoft.VSTS.TCM.ReproSteps"]) == 4000
    assert "Field 'System.AssignedTo' is not in the allowed field list and was dropped." in spec.warnings
    assert not spec.errors


def test_project_falls_back_to_deployment_default() -> None:
    policy = ActionPolicy(TenantSettings(), make_settings("sqlite+aiosqlite://", azure_devops_default_project="Ops"))
    [spec] = policy.build_proposals(_email(), _analysis(), [_call(WI)])
    assert spec.payload["project"] == "Ops"
    assert not spec.errors


def test_missing_project_blocks_the_proposal() -> None:
    policy = ActionPolicy(TenantSettings(), make_settings("sqlite+aiosqlite://", azure_devops_default_project=None))
    [spec] = policy.build_proposals(_email(), _analysis(), [_call(WI)])
    assert spec.errors
    assert spec.errors[0].startswith("Payload is invalid: create_azure_devops_work_item.project: ")
    assert spec.payload["project"] == ""  # raw payload kept for the reviewer to fix
    assert spec.title == "Investigate production login failures"


def test_unparseable_payload_is_flagged_not_dropped() -> None:
    [spec] = _policy().build_proposals(_email(), _analysis(), [_call(WI, tags=["bad<tag>"])])
    assert spec.action_type is WI
    [error] = spec.errors
    assert error == "Payload is invalid: create_azure_devops_work_item.tags: Value error, Invalid tag: 'bad<tag>'"
    assert spec.title == "Investigate production login failures"  # raw title, not a parsed payload title


def test_disallowed_project_is_a_policy_error() -> None:
    [spec] = _policy({"allowed_projects": ["Customer-Portal"]}).build_proposals(_email(), _analysis(), [_call(WI)])
    assert spec.errors == ["Project 'NUVEXA-Demo' is not in the allowed project list."]


# ---------------------------------------------------------------------------------------------
# Replies, follow-ups
# ---------------------------------------------------------------------------------------------


def test_reply_goes_only_to_the_original_sender() -> None:
    [spec] = _policy().build_proposals(_email(), _analysis(), [_call(REPLY, subject="  ")])
    assert spec.payload["to"] == ["dana@example.com"]
    assert spec.payload["subject"] == "Re: Production login failures"
    assert spec.payload["in_reply_to_internet_message_id"] == "<abc@example.com>"
    assert spec.warnings == ["Reply drafts are stored in NUVEXA and are never sent automatically."]
    assert spec.title == "Reply draft: Re: Production login failures"
    assert not spec.errors


def test_reply_body_is_clipped() -> None:
    [spec] = _policy().build_proposals(_email(), _analysis(), [_call(REPLY, body="b" * 25_000)])
    assert len(spec.payload["body"]) == 20_000


def test_reply_to_unparseable_sender_is_blocked() -> None:
    [spec] = _policy().build_proposals(_email(sender_address="not-an-address"), _analysis(), [_call(REPLY)])
    assert spec.errors
    assert spec.errors[0].startswith("Payload is invalid: propose_email_reply.to.0: ")


def test_follow_up_due_date_requires_a_stated_deadline() -> None:
    [dropped] = _policy().build_proposals(_email(), _analysis(), [_call(FOLLOW_UP, due_date="2026-11-01")])
    assert dropped.payload["due_at"] is None
    assert dropped.warnings == ["Due date was dropped because the email does not state a deadline."]

    deadline = datetime(2026, 11, 1, tzinfo=UTC)
    [kept] = _policy().build_proposals(
        _email(), _analysis(deadline=deadline), [_call(FOLLOW_UP, due_date="2026-11-01")]
    )
    assert datetime.fromisoformat(kept.payload["due_at"]) == deadline
    assert kept.warnings == []
    assert kept.title == "Follow-up: Confirm the fix"


def test_follow_up_with_invalid_or_missing_date_has_no_due_date() -> None:
    for due in ("soon", None):
        [spec] = _policy().build_proposals(_email(), _analysis(), [_call(FOLLOW_UP, due_date=due)])
        assert spec.payload["due_at"] is None
        assert spec.warnings == []


# ---------------------------------------------------------------------------------------------
# validate_payload / policy_issues (edit, approve and execute paths)
# ---------------------------------------------------------------------------------------------


def test_validate_payload_accepts_matching_kind() -> None:
    data = _wi_payload().model_dump(mode="json")
    parsed = _policy().validate_payload(WI, data)
    assert isinstance(parsed, AzureDevOpsWorkItemPayload)


def test_validate_payload_rejects_kind_mismatch_and_invalid_data() -> None:
    data = _wi_payload().model_dump(mode="json")
    with pytest.raises(ValueError, match="Payload kind does not match the action type"):
        _policy().validate_payload(REPLY, data)
    with pytest.raises(ValidationError):
        _policy().validate_payload(WI, {**data, "priority": 0})
    with pytest.raises(ValidationError):
        _policy().validate_payload(WI, {**data, "organization": "evil-org"})  # extra keys are forbidden


def test_policy_issues_for_work_items() -> None:
    policy = _policy({"allowed_projects": ["NUVEXA-Demo"], "allowed_work_item_types": ["Bug"]})
    assert policy.policy_issues(WI, _wi_payload()) == []
    assert policy.policy_issues(WI, _wi_payload(project="Elsewhere")) == [
        "Project 'Elsewhere' is not in the allowed project list."
    ]
    assert policy.policy_issues(WI, _wi_payload(work_item_type="Epic")) == ["Work item type 'Epic' is not allowed."]
    assert policy.policy_issues(WI, _wi_payload(additional_fields={"Custom.Secret": "x"})) == [
        "Field 'Custom.Secret' is not in the allowed field list."
    ]


def test_policy_issues_any_project_allowed_when_list_empty() -> None:
    assert _policy().policy_issues(WI, _wi_payload(project="Any-Project")) == []


def test_policy_issues_missing_project() -> None:
    payload = AzureDevOpsWorkItemPayload.model_construct(
        project="", work_item_type="Bug", title="t", priority=2, additional_fields={}
    )
    issues = _policy().policy_issues(WI, payload)
    assert issues == ["No Azure DevOps project is configured. Set a default project in Settings."]


def test_policy_issues_reply_recipient_must_be_the_sender() -> None:
    email = _email(sender_address="Dana@Example.com")
    ok = EmailReplyDraftPayload(to=["dana@example.com"], subject="Re", body="b", source_email=_source())  # type: ignore[arg-type,list-item]
    redirected = EmailReplyDraftPayload(to=["attacker@example.net"], subject="Re", body="b", source_email=_source())  # type: ignore[arg-type,list-item]
    policy = _policy()
    assert policy.policy_issues(REPLY, ok, email) == []  # case-insensitive match
    assert policy.policy_issues(REPLY, redirected, email) == ["Reply recipients must be exactly the original sender."]
    assert policy.policy_issues(REPLY, redirected) == []  # no email → recipient check not possible here
    assert policy.policy_issues(REPLY, ok, _email(sender_address=None)) == [
        "Reply recipients must be exactly the original sender."
    ]


def test_policy_issues_none_for_tasks_and_no_action() -> None:
    task = FollowUpTaskPayload(title="t", source_email=_source())  # type: ignore[arg-type]
    none = NoActionPayload(reason="r", source_email=_source())  # type: ignore[arg-type]
    assert _policy().policy_issues(FOLLOW_UP, task, _email()) == []
    assert _policy().policy_issues(NO_ACTION, none, _email()) == []


def test_policy_issues_flags_kind_mismatch() -> None:
    task = FollowUpTaskPayload(title="t", source_email=_source())  # type: ignore[arg-type]
    assert _policy().policy_issues(WI, task) == ["Payload kind does not match the action type."]
    assert _policy().policy_issues(NO_ACTION, _wi_payload()) == ["Payload kind does not match the action type."]


@pytest.mark.parametrize("field", ["System.AssignedTo", "System.Title", "System.Tags", "System.AreaPath"])
def test_reserved_fields_are_policy_issues_even_when_allow_listed(field: str) -> None:
    # Regression: allow-listing a reserved field let it past policy; execution then failed inside the write path.
    policy = _policy({"allowed_additional_fields": [field]})
    assert policy.policy_issues(WI, _wi_payload(additional_fields={field: "x"})) == [
        f"Field '{field}' can only be set through its dedicated attribute."
    ]


def test_reserved_field_proposed_by_the_model_blocks_the_proposal() -> None:
    policy = _policy({"allowed_additional_fields": ["System.AssignedTo"]})
    analysis = _analysis(fields=[ProposedField(name="System.AssignedTo", value="ceo@example.com")])
    [spec] = policy.build_proposals(_email(), analysis, [_call(WI)])
    assert spec.errors == ["Field 'System.AssignedTo' can only be set through its dedicated attribute."]
    assert spec.payload["assigned_to"] is None
