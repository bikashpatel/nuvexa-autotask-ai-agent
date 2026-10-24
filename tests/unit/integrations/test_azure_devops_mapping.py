from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.integrations.azure_devops.client import build_patch_document
from app.integrations.azure_devops.mapping import (
    FIELD_DESCRIPTION,
    build_create_request,
    idempotency_tag,
    provenance_footer,
    text_to_html,
)
from app.schemas.actions import AzureDevOpsWorkItemPayload, SourceEmailReference

KEY = "ABCDEF0123456789" + "0" * 48


def make_payload(**overrides) -> AzureDevOpsWorkItemPayload:
    values: dict[str, Any] = {
        "project": "NUVEXA-Demo",
        "work_item_type": "Task",
        "title": "Prepare Q4 capacity report",
        "description": "Line 1 <b>bold?</b>\r\nLine 2 & more",
        "priority": 2,
        "source_email": SourceEmailReference(
            email_id=uuid.uuid4(),
            subject='Report "Q4" <urgent>',
            sender="Dana <dana@example.com>",
            received_at=datetime(2026, 10, 25, 14, 3, 59, tzinfo=UTC),
        ),
    }
    values.update(overrides)
    return AzureDevOpsWorkItemPayload(**values)


def test_idempotency_tag_format():
    assert idempotency_tag(KEY) == "nuvexa-abcdef0123456789"
    with pytest.raises(ValueError, match="hex"):
        idempotency_tag("not-hex-at-all-xxxxxxxx")
    with pytest.raises(ValueError, match="hex"):
        idempotency_tag("abc")


def test_description_is_escaped_and_has_provenance_footer():
    request = build_create_request(make_payload(), idempotency_key=KEY)
    description = request.fields[FIELD_DESCRIPTION]
    assert description.startswith("Line 1 &lt;b&gt;bold?&lt;/b&gt;<br>Line 2 &amp; more<br><br><em>")
    assert "<b>" not in description
    assert (
        "Created by NUVEXA from email &quot;Report &quot;Q4&quot; &lt;urgent&gt;&quot; from "
        "Dana &lt;dana@example.com&gt;, received 2026-10-25 14:03 UTC. Approved in NUVEXA before creation."
    ) in description


def test_footer_handles_missing_source_details():
    footer = provenance_footer(SourceEmailReference(email_id=uuid.uuid4()))
    assert "(no subject)" in footer
    assert "an unknown sender" in footer
    assert "at an unknown time" in footer
    request = build_create_request(make_payload(description=""), idempotency_key=KEY)
    assert request.fields[FIELD_DESCRIPTION].startswith("<em>Created by NUVEXA")


def test_fields_and_tags():
    request = build_create_request(
        make_payload(
            tags=["email", "capacity"],
            area_path="NUVEXA-Demo\\Platform",
            iteration_path="NUVEXA-Demo\\Sprint 12",
            assigned_to="riley.approver@example.com",
            additional_fields={"Microsoft.VSTS.Common.Severity": "2 - High", "Microsoft.VSTS.TCM.ReproSteps": "a<b>"},
        ),
        idempotency_key=KEY,
    )
    assert request.project == "NUVEXA-Demo"
    assert request.work_item_type == "Task"
    assert request.idempotency_tag == "nuvexa-abcdef0123456789"
    assert request.tags == ["email", "capacity", "nuvexa-abcdef0123456789"]
    assert request.fields["System.Title"] == "Prepare Q4 capacity report"
    assert request.fields["Microsoft.VSTS.Common.Priority"] == 2
    assert request.fields["System.AreaPath"] == "NUVEXA-Demo\\Platform"
    assert request.fields["System.IterationPath"] == "NUVEXA-Demo\\Sprint 12"
    assert request.fields["System.AssignedTo"] == "riley.approver@example.com"
    assert request.fields["Microsoft.VSTS.Common.Severity"] == "2 - High"
    assert request.fields["Microsoft.VSTS.TCM.ReproSteps"] == "a&lt;b&gt;"  # HTML field → escaped
    assert "System.Tags" not in request.fields

    ops = build_patch_document(request)
    tags = next(op["value"] for op in ops if op["path"] == "/fields/System.Tags")
    assert tags == "email; capacity; nuvexa-abcdef0123456789"


def test_optional_fields_are_omitted_when_unset():
    request = build_create_request(make_payload(), idempotency_key=KEY)
    for ref in ("System.AreaPath", "System.IterationPath", "System.AssignedTo"):
        assert ref not in request.fields
    assert request.tags == ["nuvexa-abcdef0123456789"]


def test_additional_fields_cannot_override_core_fields():
    with pytest.raises(ValueError, match=r"System\.Title"):
        build_create_request(make_payload(additional_fields={"System.Title": "pwned"}), idempotency_key=KEY)


def test_mapping_is_deterministic():
    payload = make_payload()
    assert build_create_request(payload, idempotency_key=KEY) == build_create_request(payload, idempotency_key=KEY)
    assert text_to_html("a\nb\rc") == "a<br>b<br>c"
