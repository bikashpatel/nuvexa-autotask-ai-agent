from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.timeutil import utcnow
from app.db.models import AppSettings, AuditEvent, EmailMessage, Tenant, WorkflowEvent
from app.domain.enums import ActorType, AuditOutcome, UserRole, WorkflowEventType
from app.schemas.settings import RetentionConfig, TenantSettings
from app.services.retention_service import purge_tenant, tenant_ids
from tests.unit.services.conftest import Factory, Fresh, World


def _workflow_event(world: World, age: timedelta) -> WorkflowEvent:
    return WorkflowEvent(
        tenant_id=world.tenant.id,
        workflow_run_id=world.run.id,
        step="classify",
        event_type=WorkflowEventType.STEP_COMPLETED,
        created_at=utcnow() - age,
    )


def _audit(tenant: Tenant, age: timedelta, event_type: str = "old_event") -> AuditEvent:
    return AuditEvent(
        tenant_id=tenant.id,
        actor_type=ActorType.SYSTEM,
        event_type=event_type,
        resource_type="tenant",
        outcome=AuditOutcome.SUCCESS,
        details={},
        created_at=utcnow() - age,
    )


async def _seed(session: AsyncSession, factory: Factory, world: World) -> dict[str, object]:
    """Rows on both sides of the default windows (90 / 180 / 365 days)."""
    now = utcnow()
    old_email = await factory.email(world.account, received_at=now - timedelta(days=91))
    new_email = await factory.email(world.account, received_at=now - timedelta(days=89))
    already_purged = await factory.email(world.account, received_at=now - timedelta(days=200), body_text=None)
    session.add_all(
        [
            _workflow_event(world, timedelta(days=181)),
            _workflow_event(world, timedelta(days=179)),
            _audit(world.tenant, timedelta(days=366)),
            _audit(world.tenant, timedelta(days=364), "recent_event"),
        ]
    )
    await session.commit()
    return {"old": old_email, "new": new_email, "purged": already_purged}


async def test_purge_applies_default_windows_and_audits_counts(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    emails = await _seed(session, factory, world)
    counts = await purge_tenant(session, world.tenant.id, settings)
    assert counts == {"email_bodies_purged": 1, "workflow_events_deleted": 1, "audit_events_deleted": 1}

    old = await fresh.get(EmailMessage, emails["old"].id)  # type: ignore[attr-defined]
    assert old is not None
    assert old.body_text is None
    assert old.body_preview == ""
    assert old.body_purged_at is not None
    assert old.subject  # metadata is kept
    new = await fresh.get(EmailMessage, emails["new"].id)  # type: ignore[attr-defined]
    assert new is not None
    assert new.body_text is not None
    assert new.body_purged_at is None
    purged = await fresh.get(EmailMessage, emails["purged"].id)  # type: ignore[attr-defined]
    assert purged is not None
    assert purged.body_purged_at is None  # already body-less rows are not touched again

    assert len(await fresh.all(WorkflowEvent)) == 1
    assert {e.event_type for e in await fresh.audits()} == {"recent_event", "retention_purge"}
    [purge] = await fresh.audits("retention_purge")
    assert purge.actor_type is ActorType.SYSTEM
    assert purge.resource_type == "tenant"
    assert purge.resource_id == str(world.tenant.id)
    assert purge.details == counts


async def test_purge_uses_tenant_specific_windows(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    await _seed(session, factory, world)
    strict = TenantSettings(retention=RetentionConfig(email_body_days=1, workflow_event_days=1, audit_days=30))
    session.add(AppSettings(tenant_id=world.tenant.id, settings=strict.model_dump(mode="json"), version=1))
    await session.commit()
    counts = await purge_tenant(session, world.tenant.id, settings)
    # world.email (received 2026-10-01) is now also past the 1-day body window
    assert counts == {"email_bodies_purged": 3, "workflow_events_deleted": 2, "audit_events_deleted": 2}
    assert await fresh.all(WorkflowEvent) == []


async def test_purge_is_tenant_scoped(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    other_tenant = await factory.tenant()
    other_user = await factory.user(other_tenant, UserRole.MEMBER)
    other_account = await factory.account(other_user)
    other_email = await factory.email(other_account, received_at=utcnow() - timedelta(days=400))
    session.add(_audit(other_tenant, timedelta(days=400), "other_tenant_old"))
    await session.commit()

    counts = await purge_tenant(session, world.tenant.id, settings)
    assert counts == {"email_bodies_purged": 0, "workflow_events_deleted": 0, "audit_events_deleted": 0}
    kept = await fresh.get(EmailMessage, other_email.id)
    assert kept is not None
    assert kept.body_text is not None
    assert len(await fresh.audits("other_tenant_old")) == 1
    [purge] = await fresh.audits("retention_purge")
    assert purge.tenant_id == world.tenant.id


async def test_tenant_ids_lists_every_tenant(session: AsyncSession, factory: Factory, world: World) -> None:
    second = await factory.tenant()
    assert set(await tenant_ids(session)) == {world.tenant.id, second.id}
