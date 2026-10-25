"""SQLAlchemy ORM models. Every business row carries `tenant_id`; repositories always filter on it."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
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
    JobStatus,
    JobType,
    OAuthPurpose,
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


def _partial(where: str) -> dict[str, Any]:
    clause = text(where)
    return {"postgresql_where": clause, "sqlite_where": clause}


def _tenant_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)


# ---------------------------------------------------------------------------------------------
# Tenancy and identity
# ---------------------------------------------------------------------------------------------


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    entra_tenant_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email"),
        UniqueConstraint("tenant_id", "entra_object_id"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(200))
    entra_object_id: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[UserRole] = mapped_column(str_enum(UserRole), default=UserRole.MEMBER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None]

    tenant: Mapped[Tenant] = relationship(lazy="raise")


class UserSession(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))


class OAuthState(UUIDPrimaryKeyMixin, Base):
    """Server-side storage of an in-flight MSAL auth-code flow (contains the PKCE verifier → encrypted)."""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(128), unique=True)
    purpose: Mapped[OAuthPurpose] = mapped_column(str_enum(OAuthPurpose))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    browser_binding_hash: Mapped[str] = mapped_column(String(64))
    flow_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    redirect_after: Mapped[str] = mapped_column(String(500), default="/")
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]


class ConnectedAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "connected_accounts"
    __table_args__ = (UniqueConstraint("user_id", "provider", "external_account_id"),)

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[ConnectionProvider] = mapped_column(str_enum(ConnectionProvider))
    external_account_id: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(200))
    granted_scopes: Mapped[list[str]] = mapped_column(default=list)
    status: Mapped[ConnectionStatus] = mapped_column(str_enum(ConnectionStatus), default=ConnectionStatus.ACTIVE)
    last_error: Mapped[str | None] = mapped_column(Text)
    connected_at: Mapped[datetime | None]
    disconnected_at: Mapped[datetime | None]


class CredentialRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Encrypted token material, deliberately separated from business tables."""

    __tablename__ = "credential_store"

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    connected_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connected_accounts.id", ondelete="CASCADE"), unique=True
    )
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary)


# ---------------------------------------------------------------------------------------------
# Email ingestion
# ---------------------------------------------------------------------------------------------


class EmailSyncCheckpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "email_sync_checkpoints"
    __table_args__ = (UniqueConstraint("connected_account_id", "folder"),)

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    connected_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connected_accounts.id", ondelete="CASCADE"))
    folder: Mapped[str] = mapped_column(String(64), default="inbox")
    delta_link: Mapped[str | None] = mapped_column(Text)
    last_status: Mapped[SyncStatus] = mapped_column(str_enum(SyncStatus), default=SyncStatus.IDLE)
    last_sync_started_at: Mapped[datetime | None]
    last_sync_completed_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    messages_ingested_total: Mapped[int] = mapped_column(Integer, default=0)


class EmailMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint("connected_account_id", "provider_message_id"),
        Index("ix_email_messages_owner_received", "tenant_id", "owner_user_id", "received_at"),
        Index("ix_email_messages_tenant_status", "tenant_id", "analysis_status"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    connected_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connected_accounts.id", ondelete="CASCADE"))
    provider_message_id: Mapped[str] = mapped_column(String(512))
    internet_message_id: Mapped[str | None] = mapped_column(String(998))
    conversation_id: Mapped[str | None] = mapped_column(String(512))
    change_key: Mapped[str | None] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(Text, default="")
    sender_name: Mapped[str | None] = mapped_column(String(320))
    sender_address: Mapped[str | None] = mapped_column(String(320))
    to_recipients: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    cc_recipients: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    received_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]
    body_text: Mapped[str | None] = mapped_column(Text)
    body_preview: Mapped[str] = mapped_column(String(512), default="")
    body_content_type: Mapped[str] = mapped_column(String(16), default="text")
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(default=list)  # metadata only
    importance: Mapped[str | None] = mapped_column(String(16))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    web_link: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    # Denormalized from the latest analysis for list filtering.
    analysis_status: Mapped[EmailAnalysisStatus] = mapped_column(
        str_enum(EmailAnalysisStatus), default=EmailAnalysisStatus.NOT_ANALYZED
    )
    category: Mapped[EmailCategory | None] = mapped_column(str_enum(EmailCategory))
    priority: Mapped[Priority | None] = mapped_column(str_enum(Priority))
    latest_workflow_run_id: Mapped[uuid.UUID | None]
    content_changed_since_analysis: Mapped[bool] = mapped_column(Boolean, default=False)
    body_purged_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]  # removed from the mailbox (delta @removed)


# ---------------------------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------------------------

_ACTIVE_RUN_WHERE = "status NOT IN ('completed', 'rejected', 'cancelled')"


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("uq_workflow_runs_active_email", "email_message_id", unique=True, **_partial(_ACTIVE_RUN_WHERE)),
        Index("ix_workflow_runs_tenant_status", "tenant_id", "status"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    email_message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email_messages.id", ondelete="CASCADE"), index=True)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[WorkflowStatus] = mapped_column(str_enum(WorkflowStatus), default=WorkflowStatus.RECEIVED)
    current_step: Mapped[str | None] = mapped_column(String(64))
    trigger: Mapped[WorkflowTrigger] = mapped_column(str_enum(WorkflowTrigger), default=WorkflowTrigger.MANUAL)
    analysis_attempt: Mapped[int] = mapped_column(Integer, default=0)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    model_provider: Mapped[str | None] = mapped_column(String(40))
    model_name: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    schema_version: Mapped[str | None] = mapped_column(String(40))
    error_category: Mapped[FailureCategory | None] = mapped_column(str_enum(FailureCategory))
    error_message: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    awaiting_review_since: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]


class WorkflowEvent(UUIDPrimaryKeyMixin, Base):
    """Step-by-step timeline (sanitized: no email content, no secrets, no chain-of-thought)."""

    __tablename__ = "workflow_events"
    __table_args__ = (Index("ix_workflow_events_run_created", "workflow_run_id", "created_at"),)

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    step: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[WorkflowEventType] = mapped_column(str_enum(WorkflowEventType))
    status: Mapped[str] = mapped_column(String(32), default="success")
    message: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime]


class EmailAnalysis(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "email_analyses"
    __table_args__ = (UniqueConstraint("workflow_run_id", "attempt"),)

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    email_message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email_messages.id", ondelete="CASCADE"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    classification: Mapped[dict[str, Any]] = mapped_column(default=dict)
    extraction: Mapped[dict[str, Any]] = mapped_column(default=dict)
    category: Mapped[EmailCategory] = mapped_column(str_enum(EmailCategory))
    priority: Mapped[Priority] = mapped_column(str_enum(Priority))
    urgency: Mapped[Urgency] = mapped_column(str_enum(Urgency))
    action_recommended: Mapped[bool] = mapped_column(Boolean)
    needs_more_info: Mapped[bool] = mapped_column(Boolean)
    confidence_score: Mapped[float] = mapped_column(Float)
    confidence_bucket: Mapped[ConfidenceBucket] = mapped_column(str_enum(ConfidenceBucket))
    confidence_method: Mapped[str] = mapped_column(String(64))
    injection_flags: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    validation_warnings: Mapped[list[str]] = mapped_column(default=list)
    prompt_version: Mapped[str] = mapped_column(String(40))
    schema_version: Mapped[str] = mapped_column(String(40))
    model_provider: Mapped[str] = mapped_column(String(40))
    model_name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime]


# ---------------------------------------------------------------------------------------------
# Actions, approvals, execution
# ---------------------------------------------------------------------------------------------

_KEY_HOLDING_WHERE = "status NOT IN ('rejected', 'cancelled', 'superseded')"


class ProposedAction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "proposed_actions"
    __table_args__ = (
        Index("uq_proposed_actions_idempotency_live", "idempotency_key", unique=True, **_partial(_KEY_HOLDING_WHERE)),
        Index("ix_proposed_actions_tenant_status", "tenant_id", "status"),
        Index("ix_proposed_actions_owner_status", "tenant_id", "owner_user_id", "status"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    email_message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email_messages.id", ondelete="CASCADE"))
    analysis_attempt: Mapped[int] = mapped_column(Integer, default=1)
    action_type: Mapped[ActionType] = mapped_column(str_enum(ActionType))
    target_integration: Mapped[TargetIntegration] = mapped_column(str_enum(TargetIntegration))
    status: Mapped[ActionStatus] = mapped_column(str_enum(ActionStatus), default=ActionStatus.PENDING_APPROVAL)
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    rationale: Mapped[str] = mapped_column(Text, default="")
    expected_impact: Mapped[str] = mapped_column(Text, default="")
    required_permissions: Mapped[list[str]] = mapped_column(default=list)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    validation_errors: Mapped[list[str]] = mapped_column(default=list)
    validation_warnings: Mapped[list[str]] = mapped_column(default=list)
    source: Mapped[str] = mapped_column(String(16), default="agent")
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    external_id: Mapped[str | None] = mapped_column(String(255))
    external_url: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime | None]


class ActionApproval(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "action_approvals"
    __table_args__ = (Index("ix_action_approvals_action_created", "action_id", "created_at"),)

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    action_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("proposed_actions.id", ondelete="CASCADE"))
    decision: Mapped[ApprovalDecision] = mapped_column(str_enum(ApprovalDecision))
    decided_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    action_version: Mapped[int] = mapped_column(Integer)
    payload_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    invalidated_at: Mapped[datetime | None]
    invalidated_reason: Mapped[str | None] = mapped_column(String(255))


class ActionExecution(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "action_executions"
    __table_args__ = (
        Index("uq_action_executions_one_success", "action_id", unique=True, **_partial("status = 'succeeded'")),
        Index("uq_action_executions_one_running", "action_id", unique=True, **_partial("status = 'running'")),
        Index("ix_action_executions_action_started", "action_id", "started_at"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    action_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("proposed_actions.id", ondelete="CASCADE"))
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("action_approvals.id", ondelete="CASCADE"))
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[ExecutionStatus] = mapped_column(str_enum(ExecutionStatus))
    action_version: Mapped[int] = mapped_column(Integer)
    payload_hash: Mapped[str] = mapped_column(String(64))
    deduplicated: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    external_id: Mapped[str | None] = mapped_column(String(255))
    external_url: Mapped[str | None] = mapped_column(Text)
    provider_status_code: Mapped[int | None] = mapped_column(Integer)
    provider_response: Mapped[dict[str, Any]] = mapped_column(default=dict)  # sanitized
    error_category: Mapped[FailureCategory | None] = mapped_column(str_enum(FailureCategory))
    error_message: Mapped[str | None] = mapped_column(Text)


class FollowUpTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "follow_up_tasks"
    __table_args__ = (Index("ix_follow_up_tasks_owner_status", "tenant_id", "owner_user_id", "status"),)

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proposed_actions.id", ondelete="SET NULL"), unique=True
    )
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("email_messages.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[datetime | None]
    status: Mapped[TaskStatus] = mapped_column(str_enum(TaskStatus), default=TaskStatus.OPEN)
    completed_at: Mapped[datetime | None]


class ReplyDraft(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Approved reply proposals are stored here. The MVP never sends email."""

    __tablename__ = "reply_drafts"

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proposed_actions.id", ondelete="SET NULL"), unique=True
    )
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("email_messages.id", ondelete="SET NULL"))
    to_recipients: Mapped[list[str]] = mapped_column(default=list)
    subject: Mapped[str] = mapped_column(String(998))
    body: Mapped[str] = mapped_column(Text)


# ---------------------------------------------------------------------------------------------
# Audit, settings, jobs
# ---------------------------------------------------------------------------------------------


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_events_resource", "tenant_id", "resource_type", "resource_id"),
        Index("ix_audit_events_action", "action_id"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_type: Mapped[ActorType] = mapped_column(str_enum(ActorType))
    event_type: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    workflow_run_id: Mapped[uuid.UUID | None]
    action_id: Mapped[uuid.UUID | None]
    outcome: Mapped[AuditOutcome] = mapped_column(str_enum(AuditOutcome))
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime]
    # HMAC-SHA256 over the event's content (app.services.audit.compute_audit_integrity): tamper evidence.
    integrity_hash: Mapped[str | None] = mapped_column(String(64))


# Append-only audit trail enforced by the database (see docs/SECURITY_CONTROLS.md). The same statements are
# used by the Alembic migration; attaching them to the table also covers metadata.create_all (tests, demo).
AUDIT_APPEND_ONLY_POSTGRES = [
    """
    CREATE OR REPLACE FUNCTION nuvexa_audit_events_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' AND coalesce(current_setting('nuvexa.audit_purge', true), '') = 'on' THEN
            RETURN OLD;  -- retention purge (app.services.retention_service) sets this per transaction
        END IF;
        RAISE EXCEPTION 'audit_events is append-only (% blocked)', TG_OP USING ERRCODE = 'insufficient_privilege';
    END;
    $$
    """,
    """
    CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION nuvexa_audit_events_guard()
    """,
    """
    CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION nuvexa_audit_events_guard()
    """,
]
AUDIT_APPEND_ONLY_SQLITE = [
    """
    CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit_events is append-only (UPDATE blocked)'); END
    """,
]
# Minimum audit retention the database itself enforces (RetentionConfig.audit_days has the same floor; the SQL
# below spells the number out and tests/unit/db/test_privileges.py keeps them equal).
AUDIT_MIN_RETENTION_DAYS = 30
# The only sanctioned way to delete audit rows on PostgreSQL. The runtime role has no DELETE on audit_events
# (app.db.privileges); this owner-defined SECURITY DEFINER function deletes one tenant's rows older than the
# retention period and refuses to touch anything younger than AUDIT_MIN_RETENTION_DAYS, so a compromised API
# or worker can never erase recent audit history. search_path is pinned (schema, then pg_temp) so objects in
# other schemas or temporary tables cannot be substituted.
AUDIT_PURGE_FUNCTION_POSTGRES = [
    """
    DO $do$
    BEGIN
        EXECUTE format($f$
            CREATE OR REPLACE FUNCTION %1$I.nuvexa_purge_audit_events(p_tenant_id uuid, p_retention_days integer)
            RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = %1$I, pg_temp AS $body$
            DECLARE
                deleted bigint;
            BEGIN
                IF p_retention_days IS NULL OR p_retention_days < 30 THEN
                    RAISE EXCEPTION 'audit retention below the 30-day minimum'
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                PERFORM set_config('nuvexa.audit_purge', 'on', true);
                DELETE FROM audit_events
                WHERE tenant_id = p_tenant_id AND created_at < now() - make_interval(days => p_retention_days);
                GET DIAGNOSTICS deleted = ROW_COUNT;
                PERFORM set_config('nuvexa.audit_purge', 'off', true);
                RETURN deleted;
            END
            $body$$f$, current_schema());
        EXECUTE format(
            'REVOKE ALL ON FUNCTION %1$I.nuvexa_purge_audit_events(uuid, integer) FROM PUBLIC', current_schema()
        );
    END
    $do$
    """,
]
# DDL() applies Python %-formatting to its text, so literal "%" (PL/pgSQL RAISE placeholders) is doubled here.
for _statement in [*AUDIT_APPEND_ONLY_POSTGRES, *AUDIT_PURGE_FUNCTION_POSTGRES]:
    _ddl = DDL(_statement.replace("%", "%%")).execute_if(dialect="postgresql")  # type: ignore[no-untyped-call]
    event.listen(AuditEvent.__table__, "after_create", _ddl)
for _statement in AUDIT_APPEND_ONLY_SQLITE:
    _ddl = DDL(_statement.replace("%", "%%")).execute_if(dialect="sqlite")  # type: ignore[no-untyped-call]
    event.listen(AuditEvent.__table__, "after_create", _ddl)


class RateLimitCounter(Base):
    """Shared rate-limit counter (RATE_LIMIT_BACKEND=database, app.services.rate_limit): hits per bucket and fixed
    window. Deployment-level, not tenant data. `bucket` is a keyed HMAC-SHA256 of the limit name, the verified user
    id or client IP and the window length (never the identity itself); rows expire after two windows."""

    __tablename__ = "rate_limit_counters"
    __table_args__ = (Index("ix_rate_limit_counters_expires_at", "expires_at"),)

    bucket: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_index: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    hits: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime]


class AppSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "app_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), unique=True)
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Job(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable job queue row (claimed with SELECT … FOR UPDATE SKIP LOCKED on PostgreSQL)."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "run_after"),
        Index(
            "uq_jobs_dedupe_active",
            "dedupe_key",
            unique=True,
            **_partial("status IN ('queued', 'running') AND dedupe_key IS NOT NULL"),
        ),
        Index(
            "uq_jobs_concurrency_running",
            "concurrency_key",
            unique=True,
            **_partial("status = 'running' AND concurrency_key IS NOT NULL"),
        ),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    job_type: Mapped[JobType] = mapped_column(str_enum(JobType))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus), default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime]
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None]
    dedupe_key: Mapped[str | None] = mapped_column(String(255))
    concurrency_key: Mapped[str | None] = mapped_column(String(255))
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    finished_at: Mapped[datetime | None]
