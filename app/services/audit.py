"""Audit trail and workflow timeline recording (sanitized details only)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.context import get_correlation_id
from app.core.timeutil import utcnow
from app.db.models import AuditEvent, WorkflowEvent
from app.domain.enums import ActorType, AuditOutcome, WorkflowEventType
from app.observability.logging import get_logger, redact_value
from app.services.authz import Actor, AuditedPermissionDenied

log = get_logger(__name__)


def record_audit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    event_type: str,
    resource_type: str,
    resource_id: uuid.UUID | str | None,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    actor: Actor | None = None,
    actor_type: ActorType | None = None,
    workflow_run_id: uuid.UUID | None = None,
    action_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Add an audit event to the caller's transaction (committed together with the change it describes)."""
    event = AuditEvent(
        tenant_id=tenant_id,
        actor_user_id=actor.user_id if actor else None,
        actor_type=actor_type or (ActorType.USER if actor else ActorType.SYSTEM),
        event_type=event_type,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        workflow_run_id=workflow_run_id,
        action_id=action_id,
        outcome=outcome,
        correlation_id=get_correlation_id(),
        ip_address=actor.ip_address if actor else None,
        details=redact_value(details or {}),
        created_at=utcnow(),
    )
    session.add(event)
    log.info(
        "audit_event",
        audit_event_type=event_type,
        resource_type=resource_type,
        resource_id=event.resource_id,
        outcome=outcome.value,
        tenant_id=str(tenant_id) if tenant_id else None,
    )
    return event


async def record_permission_denied(
    session_factory: async_sessionmaker[AsyncSession], exc: AuditedPermissionDenied
) -> None:
    """Persist a denial in its own transaction (the request's transaction is rolled back)."""
    try:
        async with session_factory() as session:
            record_audit(
                session,
                tenant_id=exc.actor.tenant_id,
                actor=exc.actor,
                event_type="permission_denied",
                resource_type=exc.resource_type,
                resource_id=exc.resource_id,
                outcome=AuditOutcome.DENIED,
                action_id=_as_uuid(exc.resource_id) if exc.resource_type == "action" else None,
                details={"permission": exc.permission, "reason": exc.message},
            )
            await session.commit()
    except Exception:  # audit must never mask the original error
        log.exception("audit_write_failed")


def _as_uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def record_workflow_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_run_id: uuid.UUID,
    step: str,
    event_type: WorkflowEventType,
    status: str = "success",
    message: str | None = None,
    latency_ms: int | None = None,
    details: dict[str, Any] | None = None,
) -> WorkflowEvent:
    event = WorkflowEvent(
        tenant_id=tenant_id,
        workflow_run_id=workflow_run_id,
        step=step,
        event_type=event_type,
        status=status,
        message=message,
        latency_ms=latency_ms,
        details=redact_value(details or {}),
        created_at=utcnow(),
    )
    session.add(event)
    return event
