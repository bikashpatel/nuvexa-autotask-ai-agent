"""Email, AI and API schema contracts."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.enums import ActionType, ConfidenceBucket, EmailCategory, Priority, Urgency
from app.schemas.ai import (
    PLANNING_TOOLS,
    ClassificationOutput,
    ExtractionOutput,
    PlannedToolCall,
    PlanningContext,
    ValidatedAnalysis,
    ValidatedExtraction,
)
from app.schemas.api import ActionUpdateRequest, ApproveRequest, DemoLoginRequest, ErrorResponse, Page, RejectRequest
from app.schemas.email import AttachmentMeta, EmailAddress, InjectionFlag, NormalizedEmail

RECEIVED = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def email(**overrides: Any) -> NormalizedEmail:
    data: dict[str, Any] = {
        "provider_message_id": "m-1",
        "subject": "Login broken",
        "sender": EmailAddress(name="Jo", address="jo@example.com"),
        "received_at": RECEIVED,
        "body_text": "Users cannot log in.",
        "attachments": [AttachmentMeta(name="log.txt")],
    }
    data.update(overrides)
    return NormalizedEmail(**data)


def test_content_hash_is_stable_and_hex() -> None:
    h = email().content_hash()
    assert h == email().content_hash()
    assert len(h) == 64
    int(h, 16)


@pytest.mark.parametrize(
    "change",
    [
        {"subject": "Login fixed"},
        {"sender": EmailAddress(name="Jo", address="other@example.com")},
        {"sender": None},
        {"body_text": "Users cannot log in!"},
        {"attachments": [AttachmentMeta(name="other.txt")]},
        {"attachments": []},
    ],
)
def test_content_hash_sensitive_to_analysis_relevant_fields(change: dict[str, Any]) -> None:
    assert email(**change).content_hash() != email().content_hash()


@pytest.mark.parametrize(
    "change",
    [
        {"is_read": True},
        {"change_key": "ck-2"},
        {"body_preview": "different preview"},
        {"sender": EmailAddress(name="Different Name", address="jo@example.com")},
        {"attachments": [AttachmentMeta(name="log.txt", size=99, content_type="text/plain")]},
        {"importance": "high"},
        {"web_link": "https://outlook.example/x"},
    ],
)
def test_content_hash_ignores_metadata(change: dict[str, Any]) -> None:
    assert email(**change).content_hash() == email().content_hash()


def test_normalized_email_validation() -> None:
    with pytest.raises(ValidationError):
        email(provider_message_id="")
    with pytest.raises(ValidationError):
        email(provider_message_id="x" * 513)
    with pytest.raises(ValidationError):
        email(original_body_content_type="rtf")
    address = EmailAddress(address="a@example.com")
    with pytest.raises(ValidationError):
        address.address = "b@example.com"  # type: ignore[misc]  # frozen


def test_injection_flag_limits() -> None:
    assert InjectionFlag(rule="r", severity="high", excerpt="x" * 200).severity == "high"
    with pytest.raises(ValidationError):
        InjectionFlag(rule="r", severity="critical", excerpt="x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        InjectionFlag(rule="r", severity="low", excerpt="x" * 201)


def classification(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "category": "incident",
        "rationale": "r",
        "urgency": "high",
        "priority": "high",
        "action_recommended": True,
        "needs_more_info": False,
        "missing_information": [],
        "extraction_warnings": [],
        "confidence": 0.9,
    }
    data.update(overrides)
    return data


def test_llm_models_are_strict() -> None:
    c = ClassificationOutput.model_validate(classification())
    assert c.category is EmailCategory.INCIDENT
    assert c.urgency is Urgency.HIGH
    with pytest.raises(ValidationError):
        ClassificationOutput.model_validate(classification(extra_field=1))
    with pytest.raises(ValidationError):
        ClassificationOutput.model_validate(classification(category="made_up"))


def test_llm_schemas_are_strict_mode_compatible() -> None:
    # Strict structured outputs require additionalProperties=false on every object and no free-form dicts.
    for model in (ClassificationOutput, ExtractionOutput, *(args for _, args in PLANNING_TOOLS.values())):
        schema = model.model_json_schema()
        assert schema.get("additionalProperties") is False, model.__name__
        for definition in schema.get("$defs", {}).values():
            if definition.get("type") == "object":
                assert definition.get("additionalProperties") is False
        text = json.dumps(schema)
        assert "maxLength" not in text
        assert '"format"' not in text


def test_planning_tools_registry() -> None:
    assert {action for action, _ in PLANNING_TOOLS.values()} == set(ActionType)
    call = PlannedToolCall(
        tool_name="propose_no_action", action_type=ActionType.MARK_NO_ACTION, arguments={"reason": "fyi"}
    )
    assert call.arguments == {"reason": "fyi"}
    with pytest.raises(ValidationError):
        PlannedToolCall(tool_name="delete_all", action_type=ActionType.MARK_NO_ACTION, arguments={})  # type: ignore[arg-type]


def test_planning_context_defaults() -> None:
    ctx = PlanningContext(
        allowed_action_types=[ActionType.MARK_NO_ACTION], allowed_work_item_types=[], default_work_item_type="Task"
    )
    assert ctx.azure_devops_available is True
    assert ctx.already_completed_actions == []
    assert ctx.category_work_item_type is None


def test_validated_models_enforce_limits() -> None:
    eid = uuid.uuid4()
    with pytest.raises(ValidationError):
        ValidatedExtraction(source_email_id=eid, summary="x" * 256, description="")
    extraction = ValidatedExtraction(source_email_id=eid, summary="s", description="d")
    va = ValidatedAnalysis(
        classification=ClassificationOutput.model_validate(classification()),
        extraction=extraction,
        confidence_score=0.5,
        confidence_bucket=ConfidenceBucket.MEDIUM,
    )
    assert va.confidence_method == "heuristic-v1"
    with pytest.raises(ValidationError):
        ValidatedAnalysis(
            classification=va.classification, extraction=extraction, confidence_score=1.5, confidence_bucket="high"
        )


def test_api_request_models() -> None:
    assert DemoLoginRequest().persona == "admin"
    with pytest.raises(ValidationError):
        DemoLoginRequest(persona="root")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ApproveRequest(version=1, payload_hash="short")
    with pytest.raises(ValidationError):
        ApproveRequest(version=0, payload_hash="a" * 64)
    assert ApproveRequest(version=1, payload_hash="a" * 64).comment is None
    with pytest.raises(ValidationError):
        RejectRequest(reason="   ")  # stripped → empty
    with pytest.raises(ValidationError):
        ActionUpdateRequest.model_validate({"expected_version": 1, "payload": {}, "approve": True})


def test_page_and_error_response() -> None:
    page = Page[int](items=[1, 2], total=10, limit=2, offset=0)
    assert page.model_dump() == {"items": [1, 2], "total": 10, "limit": 2, "offset": 0}
    err = ErrorResponse.model_validate({"error": {"code": "c", "message": "m"}})
    assert err.error.details == []
    assert err.error.correlation_id is None
    assert Priority.LOW.value == "low"
