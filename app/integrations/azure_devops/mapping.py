"""Approved `AzureDevOpsWorkItemPayload` → `WorkItemCreateRequest` (deterministic, no I/O)."""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

from app.core.timeutil import ensure_utc
from app.integrations.azure_devops.base import WorkItemCreateRequest
from app.schemas.actions import AzureDevOpsWorkItemPayload, SourceEmailReference

IDEMPOTENCY_TAG_PREFIX = "nuvexa-"
IDEMPOTENCY_TAG_RE = re.compile(r"^nuvexa-[0-9a-f]{16}$")
_KEY_RE = re.compile(r"^[0-9a-fA-F]{16,}$")

FIELD_TITLE = "System.Title"
FIELD_DESCRIPTION = "System.Description"
FIELD_PRIORITY = "Microsoft.VSTS.Common.Priority"
FIELD_AREA_PATH = "System.AreaPath"
FIELD_ITERATION_PATH = "System.IterationPath"
FIELD_ASSIGNED_TO = "System.AssignedTo"
FIELD_TAGS = "System.Tags"

# Set from dedicated payload attributes only; `additional_fields` may not override them.
RESERVED_FIELDS = frozenset(
    {
        FIELD_TITLE,
        FIELD_DESCRIPTION,
        FIELD_PRIORITY,
        FIELD_AREA_PATH,
        FIELD_ITERATION_PATH,
        FIELD_ASSIGNED_TO,
        FIELD_TAGS,
    }
)
# Rich-text (HTML) fields: plain-text values are escaped before they reach Azure DevOps.
HTML_FIELDS = frozenset(
    {
        FIELD_DESCRIPTION,
        "Microsoft.VSTS.TCM.ReproSteps",
        "Microsoft.VSTS.TCM.SystemInfo",
        "Microsoft.VSTS.Common.AcceptanceCriteria",
        "Microsoft.VSTS.Common.Resolution",
        "System.History",
    }
)


def idempotency_tag(idempotency_key: str) -> str:
    """`nuvexa-` + the first 16 hex characters of the proposal's idempotency key (lower-case)."""
    key = idempotency_key.strip()
    if not _KEY_RE.match(key):
        raise ValueError("idempotency_key must be a hex string of at least 16 characters")
    return f"{IDEMPOTENCY_TAG_PREFIX}{key[:16].lower()}"


def text_to_html(value: str) -> str:
    """Escape untrusted plain text for an Azure DevOps HTML field, preserving line breaks."""
    return html.escape(value.replace("\r\n", "\n").replace("\r", "\n"), quote=True).replace("\n", "<br>")


def _format_received(value: datetime | None) -> str:
    return ensure_utc(value).strftime("%Y-%m-%d %H:%M UTC") if value is not None else "at an unknown time"


def provenance_footer(source: SourceEmailReference) -> str:
    """HTML footer (all interpolated values escaped) recording where the work item came from."""
    subject = source.subject or "(no subject)"
    sender = source.sender or "an unknown sender"
    text = (
        f'Created by NUVEXA from email "{subject}" from {sender}, received {_format_received(source.received_at)}. '
        "Approved in NUVEXA before creation."
    )
    return f"<em>{html.escape(text, quote=True)}</em>"


def build_description(payload: AzureDevOpsWorkItemPayload) -> str:
    footer = provenance_footer(payload.source_email)
    if not payload.description:
        return footer
    return f"{text_to_html(payload.description)}<br><br>{footer}"


def build_create_request(payload: AzureDevOpsWorkItemPayload, *, idempotency_key: str) -> WorkItemCreateRequest:
    """Resolve the exact create request for an approved payload. The same input always yields the same output."""
    tag = idempotency_tag(idempotency_key)
    fields: dict[str, Any] = {}
    for ref, value in payload.additional_fields.items():
        if ref in RESERVED_FIELDS:
            raise ValueError(f"{ref} cannot be set through additional_fields")
        fields[ref] = text_to_html(value) if ref in HTML_FIELDS and isinstance(value, str) else value
    fields[FIELD_TITLE] = payload.title
    fields[FIELD_DESCRIPTION] = build_description(payload)
    fields[FIELD_PRIORITY] = payload.priority
    if payload.area_path:
        fields[FIELD_AREA_PATH] = payload.area_path
    if payload.iteration_path:
        fields[FIELD_ITERATION_PATH] = payload.iteration_path
    if payload.assigned_to:
        fields[FIELD_ASSIGNED_TO] = str(payload.assigned_to)
    tags = list(dict.fromkeys([*payload.tags, tag]))
    return WorkItemCreateRequest(
        project=payload.project,
        work_item_type=payload.work_item_type,
        fields=fields,
        tags=tags,
        idempotency_tag=tag,
    )
