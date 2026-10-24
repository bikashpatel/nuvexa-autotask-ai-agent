"""Contracts for Azure DevOps work-item operations. Implementations: REST client (PAT / Entra) and mock."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.domain.enums import FailureCategory


class ProjectInfo(BaseModel):
    id: str
    name: str
    state: str | None = None
    url: str | None = None


class WorkItemTypeInfo(BaseModel):
    name: str
    reference_name: str | None = None
    description: str | None = None
    is_disabled: bool = False


class ConnectionCheck(BaseModel):
    ok: bool
    mode: str
    organization: str | None
    message: str
    latency_ms: int | None = None


class FieldIssue(BaseModel):
    field: str | None = None
    message: str


class WorkItemValidationResult(BaseModel):
    valid: bool
    issues: list[FieldIssue] = Field(default_factory=list)


class WorkItemCreateRequest(BaseModel):
    """Fully resolved create request. Built by deterministic code from an approved payload only."""

    project: str
    work_item_type: str
    # Field reference name → value. Must include System.Title. Tags are passed separately.
    fields: dict[str, Any]
    tags: list[str] = Field(default_factory=list)
    # Tag used for duplicate detection (e.g. "nuvexa-1a2b3c4d5e6f7a8b"); always added to System.Tags.
    idempotency_tag: str


class WorkItemRef(BaseModel):
    id: int
    url: str  # human-facing (html) URL when available
    rev: int | None = None
    project: str | None = None
    work_item_type: str | None = None
    title: str | None = None


class CreateOutcomeStatus(StrEnum):
    CREATED = "created"
    EXISTING = "existing"  # found by idempotency tag; nothing new created
    FAILED = "failed"  # confirmed not created
    UNCERTAIN = "uncertain"  # could not determine whether it was created


class CreateOutcome(BaseModel):
    status: CreateOutcomeStatus
    work_item: WorkItemRef | None = None
    retry_count: int = 0
    latency_ms: int = 0
    http_status: int | None = None
    error_category: FailureCategory | None = None
    error_message: str | None = None
    # Sanitized provider response summary (ids, rev, state) — never headers or tokens.
    response_summary: dict[str, Any] = Field(default_factory=dict)


class AzureDevOpsClient(Protocol):
    mode: str
    organization: str | None

    async def test_connection(self) -> ConnectionCheck: ...

    async def list_projects(self) -> list[ProjectInfo]: ...

    async def list_work_item_types(self, project: str) -> list[WorkItemTypeInfo]: ...

    async def validate_work_item(self, request: WorkItemCreateRequest) -> WorkItemValidationResult:
        """Server-side validation (`validateOnly=true`) without creating anything."""
        ...

    async def find_by_idempotency_tag(self, project: str, tag: str) -> WorkItemRef | None: ...

    async def create_work_item_idempotent(self, request: WorkItemCreateRequest) -> CreateOutcome:
        """Create exactly once, as far as Azure DevOps allows.

        1. Look up `idempotency_tag`; if found → EXISTING.
        2. POST create. Retry connect errors / 429 / 5xx with bounded exponential backoff + jitter
           (honouring Retry-After). Never retry 400/401/403/404.
        3. On a timeout or connection drop *after the request may have been sent*, look the tag up
           again: found → EXISTING (or CREATED), confirmed absent → retry within the bound,
           lookup failed → UNCERTAIN.
        Returns an outcome instead of raising for provider errors.
        """
        ...

    async def aclose(self) -> None: ...
