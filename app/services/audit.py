"""Audit trail and workflow timeline recording (sanitized details only)."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.context import get_correlation_id
from app.core.security import canonical_json
from app.core.timeutil import ensure_utc, utcnow
from app.db.models import AuditEvent, WorkflowEvent
from app.domain.enums import ActorType, AuditOutcome, WorkflowEventType
from app.observability.logging import get_logger, redact_value
from app.services.authz import Actor, AuditedPermissionDenied

log = get_logger(__name__)

# Development/test fallback only; production requires AUDIT_HMAC_KEY (enforced by Settings).
_DEV_AUDIT_KEY = b"nuvexa-development-audit-key-not-for-production"
_audit_key: bytes = _DEV_AUDIT_KEY


def configure_audit_signing(key: bytes | str | SecretStr | None) -> None:
    """Set the process-wide HMAC key for audit events (called at API/worker start-up)."""
    global _audit_key  # noqa: PLW0603 — process-wide signing configuration
    if isinstance(key, SecretStr):
        key = key.get_secret_value()
    if isinstance(key, str):
        key = key.encode("utf-8")
    if not key:
        log.warning("audit_signing_using_development_key")
    _audit_key = key or _DEV_AUDIT_KEY


def _integrity_payload(event: AuditEvent) -> str:
    def opt(value: object) -> str | None:
        return None if value is None else str(value)

    return canonical_json(
        {
            "id": opt(event.id),
            "tenant_id": opt(event.tenant_id),
            "actor_user_id": opt(event.actor_user_id),
            "actor_type": event.actor_type.value,
            "event_type": event.event_type,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "workflow_run_id": opt(event.workflow_run_id),
            "action_id": opt(event.action_id),
            "outcome": event.outcome.value,
            "correlation_id": event.correlation_id,
            "ip_address": event.ip_address,
            "details": event.details,
            "created_at": ensure_utc(event.created_at).isoformat(),
        }
    )


def compute_audit_integrity(event: AuditEvent) -> str:
    """HMAC-SHA256 over the event's canonical content. Any change to a stored event changes it."""
    return hmac.new(_audit_key, _integrity_payload(event).encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass
class AuditVerification:
    checked: int = 0
    tampered: list[uuid.UUID] = field(default_factory=list)
    unsigned: list[uuid.UUID] = field(default_factory=list)


def verify_audit_trail(events: Iterable[AuditEvent]) -> AuditVerification:
    report = AuditVerification()
    for event in events:
        report.checked += 1
        if not event.integrity_hash:
            report.unsigned.append(event.id)
        elif not hmac.compare_digest(event.integrity_hash, compute_audit_integrity(event)):
            report.tampered.append(event.id)
    return report


async def verify_tenant_audit_trail(session: AsyncSession, tenant_id: uuid.UUID) -> AuditVerification:
    report = AuditVerification()
    stmt = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id).execution_options(yield_per=500)
    async for partition in (await session.stream_scalars(stmt)).partitions():
        part = verify_audit_trail(partition)
        report.checked += part.checked
        report.tampered.extend(part.tampered)
        report.unsigned.extend(part.unsigned)
    return report


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
        id=uuid.uuid4(),
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
    event.integrity_hash = compute_audit_integrity(event)
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
