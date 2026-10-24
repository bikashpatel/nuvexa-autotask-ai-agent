"""Domain enumerations shared by persistence, schemas, services and the workflow graph."""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    VIEWER = "viewer"
    MEMBER = "member"
    APPROVER = "approver"
    ADMIN = "admin"


class ConnectionProvider(StrEnum):
    MICROSOFT_GRAPH = "microsoft_graph"
    AZURE_DEVOPS = "azure_devops"
    DEMO_MAILBOX = "demo_mailbox"


class ConnectionStatus(StrEnum):
    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class OAuthPurpose(StrEnum):
    LOGIN = "login"
    CONNECT_MAILBOX = "connect_mailbox"
    CONNECT_AZURE_DEVOPS = "connect_azure_devops"


class SyncStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EmailAnalysisStatus(StrEnum):
    NOT_ANALYZED = "not_analyzed"
    QUEUED = "queued"
    ANALYZING = "analyzing"
    AWAITING_REVIEW = "awaiting_review"
    DONE = "done"
    FAILED = "failed"


class EmailCategory(StrEnum):
    INCIDENT = "incident"
    BUG = "bug"
    SERVICE_REQUEST = "service_request"
    TASK_REQUEST = "task_request"
    FEATURE_REQUEST = "feature_request"
    FOLLOW_UP = "follow_up"
    INFORMATION_ONLY = "information_only"
    SPAM_OR_IRRELEVANT = "spam_or_irrelevant"
    OTHER = "other"


class Urgency(StrEnum):
    IMMEDIATE = "immediate"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class Priority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ConfidenceBucket(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class WorkflowStatus(StrEnum):
    RECEIVED = "received"
    NORMALIZED = "normalized"
    CLASSIFIED = "classified"
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    ACTION_PROPOSED = "action_proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_WORKFLOW_STATUSES = frozenset({WorkflowStatus.COMPLETED, WorkflowStatus.REJECTED, WorkflowStatus.CANCELLED})
ACTIVE_WORKFLOW_STATUSES = frozenset(set(WorkflowStatus) - TERMINAL_WORKFLOW_STATUSES)


class WorkflowTrigger(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"


class WorkflowEventType(StrEnum):
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    INTERRUPT = "interrupt"
    RESUME = "resume"
    INTEGRATION_CALL = "integration_call"
    STATUS_CHANGED = "status_changed"


class ActionType(StrEnum):
    CREATE_AZURE_DEVOPS_WORK_ITEM = "create_azure_devops_work_item"
    PROPOSE_EMAIL_REPLY = "propose_email_reply"
    CREATE_FOLLOW_UP_TASK = "create_follow_up_task"
    MARK_NO_ACTION = "mark_no_action"


class TargetIntegration(StrEnum):
    AZURE_DEVOPS = "azure_devops"
    REPLY_DRAFTS = "reply_drafts"  # stored in-app; never sent
    INTERNAL_TASKS = "internal_tasks"
    NONE = "none"


ACTION_TARGETS: dict[ActionType, TargetIntegration] = {
    ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM: TargetIntegration.AZURE_DEVOPS,
    ActionType.PROPOSE_EMAIL_REPLY: TargetIntegration.REPLY_DRAFTS,
    ActionType.CREATE_FOLLOW_UP_TASK: TargetIntegration.INTERNAL_TASKS,
    ActionType.MARK_NO_ACTION: TargetIntegration.NONE,
}


class ActionStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    SUPERSEDED = "superseded"


# Proposals in these states no longer hold their idempotency key.
RELEASED_ACTION_STATUSES = frozenset({ActionStatus.REJECTED, ActionStatus.CANCELLED, ActionStatus.SUPERSEDED})
OPEN_ACTION_STATUSES = frozenset(
    {ActionStatus.PENDING_APPROVAL, ActionStatus.APPROVED, ActionStatus.FAILED, ActionStatus.UNCERTAIN}
)


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ExecutionStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class FailureCategory(StrEnum):
    VALIDATION = "validation"
    AUTHORIZATION = "authorization"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    AMBIGUOUS = "ambiguous"
    DELTA_EXPIRED = "delta_expired"
    CONFIGURATION = "configuration"
    LLM_OUTPUT = "llm_output"
    LLM_PROVIDER = "llm_provider"
    INTERNAL = "internal"


class TaskStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    DISMISSED = "dismissed"


class ActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"
    AGENT = "agent"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    SYNC_MAILBOX = "sync_mailbox"
    ANALYZE_EMAIL = "analyze_email"
    RESUME_WORKFLOW = "resume_workflow"
    RETENTION_PURGE = "retention_purge"
