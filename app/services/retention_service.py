"""Data retention: purge email bodies and old operational records according to tenant settings."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.timeutil import utcnow
from app.db.models import AuditEvent, EmailMessage, Tenant, WorkflowEvent
from app.services.audit import record_audit
from app.services.settings_service import get_tenant_settings


async def purge_tenant(session: AsyncSession, tenant_id: uuid.UUID, settings: Settings) -> dict[str, int]:
    ts = await get_tenant_settings(session, tenant_id, settings)
    now = utcnow()
    bodies = await session.execute(
        update(EmailMessage)
        .where(
            EmailMessage.tenant_id == tenant_id,
            EmailMessage.received_at < now - timedelta(days=ts.retention.email_body_days),
            EmailMessage.body_text.is_not(None),
        )
        .values(body_text=None, body_preview="", body_purged_at=now)
    )
    events = await session.execute(
        delete(WorkflowEvent).where(
            WorkflowEvent.tenant_id == tenant_id,
            WorkflowEvent.created_at < now - timedelta(days=ts.retention.workflow_event_days),
        )
    )
    if (await session.connection()).dialect.name == "postgresql":
        # The append-only trigger on audit_events only allows deletes inside an explicit retention purge.
        await session.execute(text("SELECT set_config('nuvexa.audit_purge', 'on', true)"))
    audits = await session.execute(
        delete(AuditEvent).where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.created_at < now - timedelta(days=ts.retention.audit_days),
        )
    )
    counts = {
        "email_bodies_purged": int(getattr(bodies, "rowcount", 0) or 0),
        "workflow_events_deleted": int(getattr(events, "rowcount", 0) or 0),
        "audit_events_deleted": int(getattr(audits, "rowcount", 0) or 0),
    }
    record_audit(
        session,
        tenant_id=tenant_id,
        event_type="retention_purge",
        resource_type="tenant",
        resource_id=tenant_id,
        details=counts,
    )
    await session.commit()
    return counts


async def tenant_ids(session: AsyncSession) -> list[uuid.UUID]:
    return list((await session.execute(select(Tenant.id))).scalars().all())
