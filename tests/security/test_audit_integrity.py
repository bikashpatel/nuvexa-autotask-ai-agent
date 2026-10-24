"""Audit trail: append-only at the database level and tamper-evident (per-event HMAC)."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.timeutil import utcnow
from app.db.base import Base
from app.db.models import AuditEvent
from app.db.session import Database
from app.domain.enums import ActorType, AuditOutcome
from app.main import create_app
from app.services import audit as audit_module
from app.services.audit import compute_audit_integrity, configure_audit_signing, record_audit, verify_audit_trail
from app.services.retention_service import purge_tenant
from app.workers.runner import worker_context
from tests.conftest import make_settings
from tests.security.conftest import Harness

API = "/api/v1"


@pytest.fixture(autouse=True)
def _signing_key() -> None:
    configure_audit_signing(b"k" * 32)


async def _new_event(session, tenant_id: uuid.UUID) -> AuditEvent:  # type: ignore[no-untyped-def]
    event = record_audit(
        session,
        tenant_id=tenant_id,
        event_type="test_event",
        resource_type="test",
        resource_id="r-1",
        outcome=AuditOutcome.SUCCESS,
        details={"answer": 42},
    )
    await session.commit()
    return event


async def test_every_audit_event_carries_a_valid_integrity_hash(session, tenant) -> None:
    event = await _new_event(session, tenant.id)
    assert event.integrity_hash
    assert event.integrity_hash == compute_audit_integrity(event)


async def test_verification_detects_a_modified_event(session, tenant, settings: Settings) -> None:
    # Arrange
    good = await _new_event(session, tenant.id)
    bad = await _new_event(session, tenant.id)
    # Act: simulate tampering by someone who bypassed the application (computes no new HMAC)
    original = dict(bad.details)
    bad.details = {**original, "answer": 43}
    integrity_before = bad.integrity_hash
    # (no DB write: the append-only trigger would block it on PostgreSQL; verify the in-memory copy)
    report = verify_audit_trail([good, bad])
    # Assert
    assert report.checked == 2
    assert report.tampered == [bad.id]
    assert bad.integrity_hash == integrity_before


async def test_verification_with_a_different_key_fails_every_event(session, tenant) -> None:
    event = await _new_event(session, tenant.id)
    configure_audit_signing(b"x" * 32)
    assert verify_audit_trail([event]).tampered == [event.id]


@pytest.mark.postgres
async def test_audit_events_cannot_be_updated_in_the_database(session, tenant, settings: Settings) -> None:
    if not settings.is_postgres:
        pytest.skip("database triggers are verified on PostgreSQL")
    event = await _new_event(session, tenant.id)
    with pytest.raises(DBAPIError):
        await session.execute(update(AuditEvent).where(AuditEvent.id == event.id).values(event_type="forged"))
    await session.rollback()


@pytest.mark.postgres
async def test_audit_events_cannot_be_deleted_outside_retention(session, tenant, settings: Settings) -> None:
    if not settings.is_postgres:
        pytest.skip("database triggers are verified on PostgreSQL")
    event_id = (await _new_event(session, tenant.id)).id
    with pytest.raises(DBAPIError):
        await session.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": event_id})
    await session.rollback()
    assert (await session.execute(select(AuditEvent).where(AuditEvent.id == event_id))).scalar_one()


async def test_sqlite_also_blocks_updates(session, tenant, settings: Settings) -> None:
    if settings.is_postgres:
        pytest.skip("covered by the PostgreSQL test")
    event = await _new_event(session, tenant.id)
    with pytest.raises(DBAPIError):
        await session.execute(update(AuditEvent).where(AuditEvent.id == event.id).values(event_type="forged"))
    await session.rollback()


async def test_retention_purge_can_still_remove_expired_events(session, tenant, settings: Settings) -> None:
    # Arrange: an event older than the audit retention window
    old = AuditEvent(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        actor_type=ActorType.SYSTEM,
        event_type="ancient",
        resource_type="test",
        outcome=AuditOutcome.SUCCESS,
        details={},
        created_at=utcnow() - timedelta(days=4000),
    )
    old.integrity_hash = compute_audit_integrity(old)
    session.add(old)
    await session.commit()

    # Act
    counts = await purge_tenant(session, tenant.id, settings)

    # Assert
    assert counts["audit_events_deleted"] == 1
    assert (await session.execute(select(AuditEvent).where(AuditEvent.id == old.id))).scalar_one_or_none() is None


async def test_admin_can_verify_the_audit_trail_via_the_api(harness: Harness) -> None:
    # Arrange
    member_headers = await harness.login("member")
    await harness.client.post(f"{API}/emails/sync", json={}, headers=member_headers)
    async with harness.new_client() as admin:
        await admin.post(f"{API}/auth/demo-login", json={"persona": "admin"})
        # Act
        resp = await admin.get(f"{API}/audit-events/integrity")
    member_resp = await harness.client.get(f"{API}/audit-events/integrity")

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["checked"] >= 2
    assert body["tampered"] == []
    assert member_resp.status_code == 403


def _expected_hmac(key: bytes, event: AuditEvent) -> str:
    return hmac.new(key, audit_module._integrity_payload(event).encode("utf-8"), hashlib.sha256).hexdigest()


def _probe_event() -> AuditEvent:
    return AuditEvent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        actor_type=ActorType.SYSTEM,
        event_type="probe",
        resource_type="test",
        outcome=AuditOutcome.SUCCESS,
        details={},
        created_at=utcnow(),
    )


async def test_the_api_signs_with_the_configured_audit_key(database_url: str) -> None:
    configure_audit_signing(None)
    settings = make_settings(database_url, audit_hmac_key="api-key-" + "z" * 40)
    create_app(settings, Database(settings))
    event = _probe_event()
    assert compute_audit_integrity(event) == _expected_hmac(("api-key-" + "z" * 40).encode(), event)


async def test_the_worker_signs_with_the_configured_audit_key(database_url: str) -> None:
    configure_audit_signing(None)
    settings = make_settings(database_url, audit_hmac_key="worker-key-" + "w" * 40)
    db = Database(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with worker_context(db, settings):
        event = _probe_event()
        assert compute_audit_integrity(event) == _expected_hmac(("worker-key-" + "w" * 40).encode(), event)
    await db.dispose()
