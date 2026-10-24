from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import set_correlation_id
from app.db.models import AuditEvent, WorkflowEvent
from app.db.session import Database
from app.domain.enums import ActorType, AuditOutcome, WorkflowEventType
from app.observability.logging import REDACTED
from app.services.audit import record_audit, record_permission_denied, record_workflow_event
from app.services.authz import AuditedPermissionDenied, Perm
from tests.unit.services.conftest import Fresh, World


async def test_record_audit_for_user_actor_is_added_to_callers_transaction(
    session: AsyncSession, world: World, fresh: Fresh
) -> None:
    actor = world.actor(world.owner, ip="192.0.2.10")
    run_id, action_id = world.run.id, uuid.uuid4()
    set_correlation_id("corr-1234567890")
    event = record_audit(
        session,
        tenant_id=world.tenant.id,
        event_type="action_edited",
        resource_type="action",
        resource_id=action_id,
        actor=actor,
        workflow_run_id=run_id,
        action_id=action_id,
        details={"from_version": 1},
    )
    assert event in session.new
    assert await fresh.audits("action_edited") == []  # not committed by record_audit itself
    await session.commit()

    [stored] = await fresh.audits("action_edited")
    assert stored.actor_user_id == world.owner.id
    assert stored.actor_type is ActorType.USER
    assert stored.resource_id == str(action_id)
    assert stored.outcome is AuditOutcome.SUCCESS
    assert stored.correlation_id == "corr-1234567890"
    assert stored.ip_address == "192.0.2.10"
    assert stored.workflow_run_id == run_id
    assert stored.action_id == action_id
    assert stored.details == {"from_version": 1}
    assert stored.created_at is not None


async def test_record_audit_without_actor_is_a_system_event(session: AsyncSession, world: World) -> None:
    event = record_audit(
        session,
        tenant_id=None,
        event_type="retention_purge",
        resource_type="tenant",
        resource_id=None,
        outcome=AuditOutcome.FAILURE,
    )
    assert event.actor_type is ActorType.SYSTEM
    assert event.actor_user_id is None
    assert event.ip_address is None
    assert event.resource_id is None
    assert event.correlation_id is None
    assert event.details == {}
    assert event.outcome is AuditOutcome.FAILURE


async def test_record_audit_explicit_actor_type_wins(session: AsyncSession, world: World) -> None:
    agent = record_audit(
        session,
        tenant_id=world.tenant.id,
        event_type="actions_proposed",
        resource_type="workflow_run",
        resource_id="plain-string-id",
        actor_type=ActorType.AGENT,
    )
    assert agent.actor_type is ActorType.AGENT
    assert agent.resource_id == "plain-string-id"
    user_as_system = record_audit(
        session,
        tenant_id=world.tenant.id,
        event_type="x",
        resource_type="y",
        resource_id=None,
        actor=world.actor(world.admin),
        actor_type=ActorType.SYSTEM,
    )
    assert user_as_system.actor_type is ActorType.SYSTEM
    assert user_as_system.actor_user_id == world.admin.id


async def test_record_audit_redacts_sensitive_details(session: AsyncSession, world: World, fresh: Fresh) -> None:
    details: dict[str, Any] = {
        "access_token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXNpZw",
        "client_secret": "s3cr3t",
        "body": "Email body must never reach the audit log",
        "nested": {"password": "hunter2", "count": 3},
        "headers": ["Bearer abc.def.ghi"],
        "content_hash": "abc123",  # safe metadata despite the 'content' pattern
        "changed_fields": ["title"],
    }
    record_audit(
        session,
        tenant_id=world.tenant.id,
        event_type="redaction",
        resource_type="action",
        resource_id=None,
        details=details,
    )
    await session.commit()
    [stored] = await fresh.audits("redaction")
    assert stored.details["access_token"] == REDACTED
    assert stored.details["client_secret"] == REDACTED
    assert stored.details["body"] == REDACTED
    assert stored.details["nested"] == {"password": REDACTED, "count": 3}
    assert stored.details["headers"] == [f"Bearer {REDACTED}"]
    assert stored.details["content_hash"] == "abc123"
    assert stored.details["changed_fields"] == ["title"]
    assert "hunter2" not in str(stored.details)
    assert details["client_secret"] == "s3cr3t"  # caller's dict is not mutated


def _denial(world: World, *, resource_type: str, resource_id: str | None) -> AuditedPermissionDenied:
    return AuditedPermissionDenied(
        "The action has not been approved.",
        actor=world.actor(world.other, ip="203.0.113.9"),
        resource_type=resource_type,
        resource_id=resource_id,
        permission=Perm.ACTION_EXECUTE,
    )


async def test_permission_denied_is_persisted_in_its_own_transaction(
    session: AsyncSession, db: Database, world: World, fresh: Fresh
) -> None:
    action_id = uuid.uuid4()
    tenant_id, other_id = world.tenant.id, world.other.id
    record_audit(session, tenant_id=tenant_id, event_type="doomed", resource_type="x", resource_id=None)
    await record_permission_denied(
        db.session_factory, _denial(world, resource_type="action", resource_id=str(action_id))
    )
    await session.rollback()  # the request's own transaction is discarded (and expires `world` rows)

    assert await fresh.audits("doomed") == []
    [denied] = await fresh.audits("permission_denied")
    assert denied.outcome is AuditOutcome.DENIED
    assert denied.tenant_id == tenant_id
    assert denied.actor_user_id == other_id
    assert denied.ip_address == "203.0.113.9"
    assert denied.resource_type == "action"
    assert denied.resource_id == str(action_id)
    assert denied.action_id == action_id
    assert denied.details == {"permission": Perm.ACTION_EXECUTE, "reason": "The action has not been approved."}


async def test_permission_denied_action_id_only_for_action_uuid_resources(
    db: Database, world: World, fresh: Fresh
) -> None:
    await record_permission_denied(db.session_factory, _denial(world, resource_type="action", resource_id="not-a-uuid"))
    await record_permission_denied(db.session_factory, _denial(world, resource_type="action", resource_id=None))
    await record_permission_denied(
        db.session_factory, _denial(world, resource_type="email", resource_id=str(uuid.uuid4()))
    )
    events = await fresh.audits("permission_denied")
    assert len(events) == 3
    assert all(e.action_id is None for e in events)
    assert sorted(e.resource_type for e in events) == ["action", "action", "email"]


async def test_permission_denied_never_raises_when_session_factory_fails(world: World, fresh: Fresh) -> None:
    def broken_factory() -> Any:
        raise RuntimeError("database unavailable")

    await record_permission_denied(broken_factory, _denial(world, resource_type="api", resource_id=None))  # type: ignore[arg-type]
    assert await fresh.audits("permission_denied") == []


async def test_permission_denied_never_raises_when_commit_fails(db: Database, world: World, fresh: Fresh) -> None:
    failing: list[str] = []

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with db.session() as s:

            async def commit() -> None:
                failing.append("commit")
                raise RuntimeError("commit failed")

            s.commit = commit  # type: ignore[method-assign]
            yield s

    await record_permission_denied(factory, _denial(world, resource_type="action", resource_id=None))  # type: ignore[arg-type]
    assert failing == ["commit"]
    assert await fresh.audits("permission_denied") == []


async def test_record_workflow_event_defaults_and_redaction(session: AsyncSession, world: World, fresh: Fresh) -> None:
    event = record_workflow_event(
        session,
        tenant_id=world.tenant.id,
        workflow_run_id=world.run.id,
        step="classify",
        event_type=WorkflowEventType.LLM_CALL,
        details={"api_key": "sk-test", "attempt": 2},
    )
    assert event in session.new
    await session.commit()
    [stored] = await fresh.all(WorkflowEvent)
    assert stored.status == "success"
    assert stored.message is None
    assert stored.latency_ms is None
    assert stored.step == "classify"
    assert stored.event_type is WorkflowEventType.LLM_CALL
    assert stored.details == {"api_key": REDACTED, "attempt": 2}
    assert stored.workflow_run_id == world.run.id


async def test_record_workflow_event_explicit_fields(session: AsyncSession, world: World, fresh: Fresh) -> None:
    record_workflow_event(
        session,
        tenant_id=world.tenant.id,
        workflow_run_id=world.run.id,
        step="plan",
        event_type=WorkflowEventType.STEP_FAILED,
        status="error",
        message="Planner failed",
        latency_ms=42,
    )
    await session.commit()
    [stored] = await fresh.all(WorkflowEvent)
    assert (stored.status, stored.message, stored.latency_ms, stored.details) == ("error", "Planner failed", 42, {})
    assert await fresh.all(AuditEvent) == []  # timeline events are not audit events
