"""Executable action payload schemas (what an approval is bound to)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.schemas.actions import (
    AzureDevOpsWorkItemPayload,
    EmailReplyDraftPayload,
    FollowUpTaskPayload,
    NoActionPayload,
    SourceEmailReference,
    parse_payload,
)

SOURCE = {"email_id": str(uuid.uuid4()), "subject": "Subject", "sender": "a@example.com"}


def work_item(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "create_azure_devops_work_item",
        "project": "NUVEXA-Demo",
        "work_item_type": "Bug",
        "title": "Fix login",
        "priority": 2,
        "source_email": SOURCE,
    }
    data.update(overrides)
    return data


def test_minimal_work_item_defaults() -> None:
    p = AzureDevOpsWorkItemPayload.model_validate(work_item())
    assert p.tags == []
    assert p.additional_fields == {}
    assert p.description == ""
    assert p.assigned_to is None
    assert p.area_path is None
    assert isinstance(p.source_email, SourceEmailReference)


def test_strings_are_stripped_and_extra_fields_forbidden() -> None:
    p = AzureDevOpsWorkItemPayload.model_validate(work_item(title="  padded  "))
    assert p.title == "padded"
    with pytest.raises(ValidationError, match="extra"):
        AzureDevOpsWorkItemPayload.model_validate(work_item(url="https://evil.example"))
    with pytest.raises(ValidationError):
        AzureDevOpsWorkItemPayload.model_validate(work_item(source_email={**SOURCE, "headers": {}}))


@pytest.mark.parametrize("priority", [0, 5, -1])
def test_priority_range(priority: int) -> None:
    with pytest.raises(ValidationError):
        AzureDevOpsWorkItemPayload.model_validate(work_item(priority=priority))


@pytest.mark.parametrize("field", ["project", "work_item_type", "title"])
def test_required_strings_cannot_be_blank(field: str) -> None:
    with pytest.raises(ValidationError):
        AzureDevOpsWorkItemPayload.model_validate(work_item(**{field: "   "}))


def test_tags_are_cleaned_and_deduplicated() -> None:
    p = AzureDevOpsWorkItemPayload.model_validate(
        work_item(tags=[" alpha ", "", "   ", "beta", "alpha", "with space", "a.b-c:d/e_f"])
    )
    assert p.tags == ["alpha", "beta", "with space", "a.b-c:d/e_f"]


@pytest.mark.parametrize("tag", ["semi;colon", "x" * 65, "<script>", 'quote"', "new\nline", "comma,tag"])
def test_invalid_tags_rejected(tag: str) -> None:
    with pytest.raises(ValidationError, match="Invalid tag"):
        AzureDevOpsWorkItemPayload.model_validate(work_item(tags=[tag]))


def test_tag_count_limit() -> None:
    with pytest.raises(ValidationError):
        AzureDevOpsWorkItemPayload.model_validate(work_item(tags=[f"t{i}" for i in range(11)]))


@given(st.from_regex(r"[A-Za-z0-9_]{1,20}( [A-Za-z0-9_]{1,10})?", fullmatch=True))
def test_safe_tags_roundtrip(tag: str) -> None:
    assert AzureDevOpsWorkItemPayload.model_validate(work_item(tags=[tag])).tags == [tag]


@pytest.mark.parametrize("name", ["Microsoft.VSTS.Common.Severity", "Custom.Field_1", "a.b"])
def test_valid_field_refs(name: str) -> None:
    p = AzureDevOpsWorkItemPayload.model_validate(work_item(additional_fields={name: "1 - Critical"}))
    assert p.additional_fields == {name: "1 - Critical"}


@pytest.mark.parametrize("name", ["NoDot", "1Starts.With.Digit", "Has Space.x", "System.Title;drop", ".leading", "a."])
def test_invalid_field_refs(name: str) -> None:
    with pytest.raises(ValidationError, match="Invalid field reference name"):
        AzureDevOpsWorkItemPayload.model_validate(work_item(additional_fields={name: "v"}))


def test_field_value_length_and_types() -> None:
    with pytest.raises(ValidationError, match="too long"):
        AzureDevOpsWorkItemPayload.model_validate(work_item(additional_fields={"A.b": "x" * 4001}))
    ok = AzureDevOpsWorkItemPayload.model_validate(
        work_item(additional_fields={"A.b": 3, "A.c": 1.5, "A.d": "x" * 4000})
    )
    assert ok.additional_fields["A.b"] == 3
    with pytest.raises(ValidationError):
        AzureDevOpsWorkItemPayload.model_validate(work_item(additional_fields={"A.b": {"nested": 1}}))
    with pytest.raises(ValidationError):
        AzureDevOpsWorkItemPayload.model_validate(work_item(additional_fields={f"A.f{i}": 1 for i in range(11)}))


@pytest.mark.parametrize("field", ["area_path", "iteration_path"])
def test_classification_paths(field: str) -> None:
    ok = AzureDevOpsWorkItemPayload.model_validate(work_item(**{field: "Project\\Team A\\Sprint 1"}))
    assert getattr(ok, field) == "Project\\Team A\\Sprint 1"
    assert getattr(AzureDevOpsWorkItemPayload.model_validate(work_item(**{field: None})), field) is None
    for bad in ["a<b", "a|b", 'a"b', "a*b", "a?b", "a:b", "a\x01b"]:
        with pytest.raises(ValidationError, match="Invalid classification path"):
            AzureDevOpsWorkItemPayload.model_validate(work_item(**{field: bad}))


def test_assigned_to_must_be_an_email() -> None:
    with pytest.raises(ValidationError):
        AzureDevOpsWorkItemPayload.model_validate(work_item(assigned_to="not-an-email"))
    p = AzureDevOpsWorkItemPayload.model_validate(work_item(assigned_to="dev@example.com"))
    assert p.assigned_to == "dev@example.com"


def reply(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "propose_email_reply",
        "to": ["sender@example.com"],
        "subject": "Re: hi",
        "body": "Thanks",
        "source_email": SOURCE,
    }
    data.update(overrides)
    return data


def test_reply_recipients_exactly_one() -> None:
    assert EmailReplyDraftPayload.model_validate(reply()).to == ["sender@example.com"]
    with pytest.raises(ValidationError):
        EmailReplyDraftPayload.model_validate(reply(to=[]))
    with pytest.raises(ValidationError):
        EmailReplyDraftPayload.model_validate(reply(to=["a@example.com", "b@example.com"]))
    with pytest.raises(ValidationError):
        EmailReplyDraftPayload.model_validate(reply(to=["not-an-address"]))


def test_reply_body_and_subject_required() -> None:
    with pytest.raises(ValidationError):
        EmailReplyDraftPayload.model_validate(reply(body=""))
    with pytest.raises(ValidationError):
        EmailReplyDraftPayload.model_validate(reply(subject="x" * 256))


def test_follow_up_and_no_action() -> None:
    fu = FollowUpTaskPayload.model_validate(
        {
            "kind": "create_follow_up_task",
            "title": "Call back",
            "due_at": "2026-11-01T10:00:00Z",
            "source_email": SOURCE,
        }
    )
    assert fu.due_at is not None
    assert fu.due_at.year == 2026
    na = NoActionPayload.model_validate({"kind": "mark_no_action", "reason": "FYI", "source_email": SOURCE})
    assert na.reason == "FYI"
    with pytest.raises(ValidationError):
        NoActionPayload.model_validate({"kind": "mark_no_action", "reason": "", "source_email": SOURCE})


@pytest.mark.parametrize(
    ("data", "cls"),
    [
        (work_item(), AzureDevOpsWorkItemPayload),
        (reply(), EmailReplyDraftPayload),
        ({"kind": "create_follow_up_task", "title": "t", "source_email": SOURCE}, FollowUpTaskPayload),
        ({"kind": "mark_no_action", "reason": "r", "source_email": SOURCE}, NoActionPayload),
    ],
)
def test_parse_payload_discriminates_by_kind(data: dict[str, Any], cls: type) -> None:
    parsed = parse_payload(data)
    assert type(parsed) is cls
    assert parsed.kind == data["kind"]
    assert parse_payload(parsed.model_dump(mode="json")) == parsed


@pytest.mark.parametrize("kind", ["delete_everything", None, ""])
def test_parse_payload_rejects_unknown_kind(kind: str | None) -> None:
    data = work_item()
    if kind is None:
        data.pop("kind")
    else:
        data["kind"] = kind
    with pytest.raises(ValidationError):
        parse_payload(data)


def test_parse_payload_kind_controls_schema() -> None:
    # A work-item body under the reply discriminator is validated as a reply (and fails).
    with pytest.raises(ValidationError):
        parse_payload({**work_item(), "kind": "propose_email_reply"})


def test_source_email_reference_limits() -> None:
    with pytest.raises(ValidationError):
        SourceEmailReference.model_validate({"email_id": "not-a-uuid"})
    with pytest.raises(ValidationError):
        SourceEmailReference.model_validate({"email_id": str(uuid.uuid4()), "sender": "x" * 321})
    ref = SourceEmailReference.model_validate({"email_id": str(uuid.uuid4())})
    assert ref.subject == ""
