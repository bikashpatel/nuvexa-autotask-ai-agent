"""HTTP request/response models (the frontend contract). Secrets never appear in these models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from app.domain.enums import (
    ActionStatus,
    ActionType,
    ActorType,
    ApprovalDecision,
    AuditOutcome,
    ConfidenceBucket,
    ConnectionProvider,
    ConnectionStatus,
    EmailAnalysisStatus,
    EmailCategory,
    ExecutionStatus,
    FailureCategory,
    Priority,
    SyncStatus,
    TargetIntegration,
    TaskStatus,
    Urgency,
    UserRole,
    WorkflowEventType,
    WorkflowStatus,
    WorkflowTrigger,
)
from app.integrations.azure_devops.base import ConnectionCheck, FieldIssue, ProjectInfo, WorkItemTypeInfo
from app.schemas.email import AttachmentMeta, InjectionFlag
from app.schemas.settings import TenantSettings


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _contains_nul(value: Any) -> bool:
    """True when any string in a decoded JSON value (keys included) contains a NUL character."""
    stack = [value]
    while stack:  # iterative: request bodies may be deeply nested
        item = stack.pop()
        if isinstance(item, str):
            if "\x00" in item:
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list | tuple):
            stack.extend(item)
    return False


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _reject_nul_characters(cls, value: Any) -> Any:
        # PostgreSQL text/jsonb cannot store NUL: reject it here (422) instead of failing at the database (500).
        if _contains_nul(value):
            raise PydanticCustomError("nul_character", "NUL characters are not allowed in text fields")
        return value


# --- Errors / pagination -------------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: str | None = None
    details: list[dict[str, Any]] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorBody


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class JobAccepted(BaseModel):
    job_ids: list[uuid.UUID]
    message: str


# --- Auth ----------------------------------------------------------------------------------------


class AuthConfigOut(BaseModel):
    app_name: str
    environment: str
    demo_mode: bool
    demo_login_enabled: bool
    entra_login_enabled: bool


class UserOut(ApiModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    tenant_id: uuid.UUID
    tenant_name: str


class MeOut(BaseModel):
    user: UserOut
    permissions: list[str]


class DemoLoginRequest(RequestModel):
    persona: Literal["admin", "approver", "member", "viewer"] = "admin"


# --- Connections / integrations -----------------------------------------------------------------


class SyncStateOut(ApiModel):
    folder: str
    last_status: SyncStatus
    last_sync_started_at: datetime | None
    last_sync_completed_at: datetime | None
    last_error: str | None
    messages_ingested_total: int


class ConnectionOut(ApiModel):
    id: uuid.UUID
    provider: ConnectionProvider
    status: ConnectionStatus
    email: str | None
    display_name: str | None
    granted_scopes: list[str]
    connected_at: datetime | None
    last_error: str | None
    sync: SyncStateOut | None = None


class MicrosoftIntegrationOut(BaseModel):
    mode: str
    entra_configured: bool
    required_scopes: list[str]
    connections: list[ConnectionOut]


class AzureDevOpsIntegrationOut(BaseModel):
    mode: str
    organization: str | None
    configured: bool
    requires_user_connection: bool
    user_connected: bool
    default_project: str | None
    default_work_item_type: str
    api_version: str


class LLMIntegrationOut(BaseModel):
    provider: str
    model: str
    structured_outputs: bool
    observability_enabled: bool


class IntegrationsOut(BaseModel):
    microsoft: MicrosoftIntegrationOut
    azure_devops: AzureDevOpsIntegrationOut
    llm: LLMIntegrationOut
    demo_mode: bool


class SyncRequest(RequestModel):
    connected_account_id: uuid.UUID | None = None


# --- Emails --------------------------------------------------------------------------------------


class EmailListItem(ApiModel):
    id: uuid.UUID
    connected_account_id: uuid.UUID
    subject: str
    sender_name: str | None
    sender_address: str | None
    received_at: datetime
    body_preview: str
    has_attachments: bool
    is_read: bool
    analysis_status: EmailAnalysisStatus
    category: EmailCategory | None
    priority: Priority | None
    latest_workflow_run_id: uuid.UUID | None
    content_changed_since_analysis: bool


class RecipientOut(BaseModel):
    name: str | None = None
    address: str | None = None


class EmailDetail(EmailListItem):
    internet_message_id: str | None
    conversation_id: str | None
    to_recipients: list[RecipientOut]
    cc_recipients: list[RecipientOut]
    body_text: str | None
    body_purged: bool
    attachments: list[AttachmentMeta]
    web_link: str | None
    importance: str | None


# --- Workflows -----------------------------------------------------------------------------------


class WorkflowRunOut(ApiModel):
    id: uuid.UUID
    email_message_id: uuid.UUID
    owner_user_id: uuid.UUID
    status: WorkflowStatus
    current_step: str | None
    trigger: WorkflowTrigger
    analysis_attempt: int
    correlation_id: str | None
    model_provider: str | None
    model_name: str | None
    prompt_version: str | None
    schema_version: str | None
    error_category: FailureCategory | None
    error_message: str | None
    metrics: dict[str, Any]
    awaiting_review_since: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    email_subject: str | None = None


class StartAnalysisOut(BaseModel):
    workflow_run: WorkflowRunOut
    created: bool
    job_id: uuid.UUID | None


class AnalysisOut(ApiModel):
    id: uuid.UUID
    workflow_run_id: uuid.UUID
    attempt: int
    category: EmailCategory
    priority: Priority
    urgency: Urgency
    action_recommended: bool
    needs_more_info: bool
    confidence_score: float
    confidence_bucket: ConfidenceBucket
    confidence_method: str
    classification: dict[str, Any]
    extraction: dict[str, Any]
    injection_flags: list[InjectionFlag]
    validation_warnings: list[str]
    model_provider: str
    model_name: str
    prompt_version: str
    schema_version: str
    created_at: datetime


class WorkflowEventOut(ApiModel):
    id: uuid.UUID
    step: str
    event_type: WorkflowEventType
    status: str
    message: str | None
    latency_ms: int | None
    details: dict[str, Any]
    created_at: datetime


# --- Actions -------------------------------------------------------------------------------------


class ApprovalOut(ApiModel):
    id: uuid.UUID
    decision: ApprovalDecision
    decided_by_user_id: uuid.UUID
    decided_by_name: str | None = None
    action_version: int
    payload_hash: str
    reason: str | None
    created_at: datetime
    invalidated_at: datetime | None
    invalidated_reason: str | None


class ExecutionOut(ApiModel):
    id: uuid.UUID
    attempt_number: int
    status: ExecutionStatus
    deduplicated: bool
    requested_by_user_id: uuid.UUID
    action_version: int
    started_at: datetime
    finished_at: datetime | None
    latency_ms: int | None
    retry_count: int
    external_id: str | None
    external_url: str | None
    provider_status_code: int | None
    error_category: FailureCategory | None
    error_message: str | None


class ActionPermissions(BaseModel):
    can_edit: bool
    can_approve: bool
    can_reject: bool
    can_cancel: bool
    can_execute: bool
    can_reconcile: bool


class ActionOut(ApiModel):
    id: uuid.UUID
    workflow_run_id: uuid.UUID
    email_message_id: uuid.UUID
    owner_user_id: uuid.UUID
    action_type: ActionType
    target_integration: TargetIntegration
    status: ActionStatus
    version: int
    payload: dict[str, Any]
    payload_hash: str
    title: str
    rationale: str
    expected_impact: str
    required_permissions: list[str]
    idempotency_key: str
    validation_errors: list[str]
    validation_warnings: list[str]
    source: str
    external_id: str | None
    external_url: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    executed_at: datetime | None
    current_approval: ApprovalOut | None = None
    permissions: ActionPermissions | None = None
    email_subject: str | None = None


class ActionDetail(ActionOut):
    approvals: list[ApprovalOut]
    executions: list[ExecutionOut]


class WorkflowDetail(BaseModel):
    run: WorkflowRunOut
    analysis: AnalysisOut | None
    actions: list[ActionOut]
    events: list[WorkflowEventOut]


class ActionUpdateRequest(RequestModel):
    expected_version: int = Field(ge=1)
    payload: dict[str, Any]


class ApproveRequest(RequestModel):
    version: int = Field(ge=1)
    payload_hash: str = Field(min_length=64, max_length=64)
    comment: str | None = Field(default=None, max_length=1_000)


class RejectRequest(RequestModel):
    reason: str = Field(min_length=1, max_length=1_000)


class CancelRequest(RequestModel):
    reason: str | None = Field(default=None, max_length=1_000)


class ExecuteRequest(RequestModel):
    version: int = Field(ge=1)
    payload_hash: str = Field(min_length=64, max_length=64)


class ExecuteAccepted(BaseModel):
    action: ActionOut
    job_id: uuid.UUID


# --- Azure DevOps --------------------------------------------------------------------------------


class AdoConnectionTestOut(ConnectionCheck):
    pass


class AdoProjectsOut(BaseModel):
    items: list[ProjectInfo]


class AdoWorkItemTypesOut(BaseModel):
    project: str
    items: list[WorkItemTypeInfo]


class AdoValidateRequest(RequestModel):
    payload: dict[str, Any]
    server_side: bool = True


class AdoValidateOut(BaseModel):
    valid: bool
    schema_issues: list[FieldIssue]
    policy_issues: list[str]
    server_issues: list[FieldIssue]
    server_validated: bool


# --- Audit / tasks / drafts / settings / dashboard ----------------------------------------------


class AuditEventOut(ApiModel):
    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    actor_name: str | None = None
    actor_type: ActorType
    event_type: str
    resource_type: str
    resource_id: str | None
    workflow_run_id: uuid.UUID | None
    action_id: uuid.UUID | None
    outcome: AuditOutcome
    correlation_id: str | None
    details: dict[str, Any]
    created_at: datetime


class AuditIntegrityOut(BaseModel):
    checked: int
    tampered: list[uuid.UUID]
    unsigned: list[uuid.UUID]
    verified_at: datetime


class FollowUpTaskOut(ApiModel):
    id: uuid.UUID
    title: str
    description: str
    due_at: datetime | None
    status: TaskStatus
    action_id: uuid.UUID | None
    email_message_id: uuid.UUID | None
    created_at: datetime
    completed_at: datetime | None


class TaskUpdateRequest(RequestModel):
    status: TaskStatus


class ReplyDraftOut(ApiModel):
    id: uuid.UUID
    to_recipients: list[str]
    subject: str
    body: str
    action_id: uuid.UUID | None
    email_message_id: uuid.UUID | None
    created_at: datetime


class SettingsOut(BaseModel):
    settings: TenantSettings
    version: int
    updated_at: datetime | None


class SettingsUpdateRequest(RequestModel):
    expected_version: int = Field(ge=1)
    settings: TenantSettings


class LLMUsageSummary(BaseModel):
    total_tokens: int
    estimated_cost_usd: float | None
    avg_latency_ms: float | None


class DashboardSummaryOut(BaseModel):
    emails_total: int
    emails_analyzed: int
    actions_proposed: int
    pending_approvals: int
    completed_actions: int
    failed_actions: int
    uncertain_actions: int
    rejected_actions: int
    workflows_by_status: dict[str, int]
    categories: dict[str, int]
    avg_approval_wait_seconds: float | None
    llm: LLMUsageSummary
    recent_activity: list[AuditEventOut]


class HealthOut(BaseModel):
    status: Literal["ok", "degraded", "error"]
    version: str
    checks: dict[str, str] = Field(default_factory=dict)
