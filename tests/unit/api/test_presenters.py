"""ORM → API conversion and per-user action permission flags."""

from __future__ import annotations

import itertools
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.presenters import (
    action_detail,
    action_out,
    action_permissions,
    audit_out,
    email_detail,
    run_out,
    user_names,
)
from app.core.security import compute_payload_hash
from app.core.timeutil import utcnow
from app.db.models import ActionApproval, ActionExecution, AuditEvent, ProposedAction, Tenant, User, WorkflowRun
from app.db.session import Database
from app.domain.enums import (
    ActionStatus,
    ActionType,
    ActorType,
    ApprovalDecision,
    AuditOutcome,
    ExecutionStatus,
    TargetIntegration,
    UserRole,
)
from app.services.authz import Actor
from app.services.identity import actor_from_user
from tests.conftest import UserFactory
from tests.unit.workflows.fakes import add_account, add_email, add_run

A = ActionStatus
OWNER_ID = uuid.uuid4()


def _actor(role: UserRole, user_id: uuid.UUID) -> Actor:
    return Actor.build(user_id=user_id, tenant_id=uuid.uuid4(), role=role, email="a@example.com", display_name="A")


def _action(status: ActionStatus, errors: list[str]) -> ProposedAction:
    return ProposedAction(owner_user_id=OWNER_ID, status=status, validation_errors=errors)


CASES = list(itertools.product(UserRole, [True, False], ActionStatus, [False, True], [False, True]))


@pytest.mark.parametrize(("role", "own", "status", "has_errors", "approved"), CASES)
def test_action_permissions_truth_table(
    role: UserRole, own: bool, status: ActionStatus, has_errors: bool, approved: bool
) -> None:
    actor = _actor(role, OWNER_ID if own else uuid.uuid4())
    perms = action_permissions(actor, _action(status, ["Missing project"] if has_errors else []), approved)
    # Oracle from the role model: viewers never act; members act on their own actions; approvers/admins on any.
    may_act = role in (UserRole.APPROVER, UserRole.ADMIN) or (role is UserRole.MEMBER and own)
    assert perms.can_edit is (may_act and status in (A.PENDING_APPROVAL, A.APPROVED, A.FAILED))
    assert perms.can_approve is (may_act and status is A.PENDING_APPROVAL and not has_errors)
    assert perms.can_reject is (may_act and status in (A.PENDING_APPROVAL, A.APPROVED))
    assert perms.can_cancel is (may_act and status in (A.PENDING_APPROVAL, A.APPROVED, A.FAILED))
    assert perms.can_execute is (may_act and status in (A.APPROVED, A.FAILED) and approved)
    assert perms.can_reconcile is (may_act and status is A.UNCERTAIN)


@pytest.mark.parametrize("status", [A.COMPLETED, A.REJECTED, A.CANCELLED, A.SUPERSEDED, A.EXECUTING])
def test_finished_or_running_actions_offer_no_mutations(status: ActionStatus) -> None:
    perms = action_permissions(_actor(UserRole.ADMIN, OWNER_ID), _action(status, []), True)
    assert not any(perms.model_dump().values())


async def _world(db: Database, session: AsyncSession, tenant: Tenant, make_user: UserFactory) -> dict[str, object]:
    owner = await make_user(tenant, UserRole.MEMBER)
    approver = await make_user(tenant, UserRole.APPROVER)
    account = await add_account(session, owner)
    email = await add_email(db, account, subject="Printer on fire")
    run = await add_run(db, email)
    payload = {"kind": "mark_no_action", "reason": "x", "source_email": {"email_id": str(email.id)}}
    async with db.session() as s:
        action = ProposedAction(
            tenant_id=tenant.id,
            owner_user_id=owner.id,
            workflow_run_id=run.id,
            email_message_id=email.id,
            action_type=ActionType.MARK_NO_ACTION,
            target_integration=TargetIntegration.NONE,
            status=A.APPROVED,
            version=2,
            payload=payload,
            payload_hash=compute_payload_hash("mark_no_action", "none", payload),
            title="Mark as no action required",
            idempotency_key="k" * 64,
        )
        s.add(action)
        await s.flush()
        now = utcnow()
        stale = ActionApproval(
            tenant_id=tenant.id,
            action_id=action.id,
            decision=ApprovalDecision.APPROVED,
            decided_by_user_id=owner.id,
            action_version=1,
            payload_hash="0" * 64,
            created_at=now - timedelta(minutes=5),
            invalidated_at=now - timedelta(minutes=4),
            invalidated_reason="edited",
        )
        current = ActionApproval(
            tenant_id=tenant.id,
            action_id=action.id,
            decision=ApprovalDecision.APPROVED,
            decided_by_user_id=approver.id,
            action_version=2,
            payload_hash=action.payload_hash,
            created_at=now,
        )
        s.add_all([stale, current])
        await s.flush()
        for attempt, status in ((2, ExecutionStatus.SUCCEEDED), (1, ExecutionStatus.FAILED)):
            s.add(
                ActionExecution(
                    tenant_id=tenant.id,
                    action_id=action.id,
                    approval_id=current.id,
                    requested_by_user_id=owner.id,
                    attempt_number=attempt,
                    status=status,
                    action_version=2,
                    payload_hash=action.payload_hash,
                    started_at=now,
                )
            )
        await s.commit()
    return {"owner": owner, "approver": approver, "email": email, "run": run, "action": action}


async def test_action_out_and_detail(
    db: Database, session: AsyncSession, tenant: Tenant, make_user: UserFactory
) -> None:
    w = await _world(db, session, tenant, make_user)
    owner, approver, action = w["owner"], w["approver"], w["action"]
    assert isinstance(owner, User)
    assert isinstance(approver, User)
    assert isinstance(action, ProposedAction)
    async with db.session() as s:
        out = await action_out(s, actor_from_user(owner), action)
        assert out.email_subject == "Printer on fire"
        assert out.current_approval is not None
        assert out.current_approval.action_version == 2
        assert out.current_approval.decided_by_name == approver.display_name
        assert out.permissions is not None
        assert out.permissions.can_execute is True
        assert out.permissions.can_approve is False

        detail = await action_detail(s, actor_from_user(owner), action)
        assert [a.action_version for a in detail.approvals] == [1, 2]  # chronological
        assert [a.decided_by_name for a in detail.approvals] == [owner.display_name, approver.display_name]
        assert detail.approvals[0].invalidated_reason == "edited"
        assert [e.attempt_number for e in detail.executions] == [1, 2]
        assert detail.id == action.id
        assert detail.permissions == out.permissions


async def test_action_out_without_approval_or_email(
    db: Database, session: AsyncSession, tenant: Tenant, make_user: UserFactory
) -> None:
    w = await _world(db, session, tenant, make_user)
    action = w["action"]
    assert isinstance(action, ProposedAction)
    async with db.session() as s:
        loaded = await s.get(ProposedAction, action.id)
        assert loaded is not None
        loaded.version = 3  # no approval matches the new version
        await s.commit()
        viewer = _actor(UserRole.VIEWER, uuid.uuid4())
        out = await action_out(s, viewer, loaded)
        assert out.current_approval is None
        assert out.permissions is not None
        assert not any(out.permissions.model_dump().values())
        loaded.email_message_id = uuid.uuid4()  # dangling reference is rendered without a subject
        assert (await action_out(s, viewer, loaded)).email_subject is None


async def test_email_detail(db: Database, session: AsyncSession, tenant: Tenant, make_user: UserFactory) -> None:
    owner = await make_user(tenant, UserRole.MEMBER)
    account = await add_account(session, owner)
    email = await add_email(db, account)
    detail = email_detail(email)
    assert [r.model_dump() for r in detail.to_recipients] == [
        {"name": "Support", "address": "support@example.com"},
        {"name": "No address", "address": None},
    ]
    assert [r.address for r in detail.cc_recipients] == ["ops@example.com"]
    assert detail.body_purged is False
    assert detail.body_text == email.body_text
    assert detail.attachments[0].name == "screenshot.png"

    purged = await add_email(db, account, purged=True)
    purged_detail = email_detail(purged)
    assert purged_detail.body_purged is True
    assert purged_detail.body_text is None


async def test_audit_out_resolves_actor_names(
    db: Database, session: AsyncSession, tenant: Tenant, make_user: UserFactory
) -> None:
    user = await make_user(tenant, UserRole.ADMIN)
    now = utcnow()
    events = [
        AuditEvent(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            actor_user_id=uid,
            actor_type=ActorType.USER if uid else ActorType.SYSTEM,
            event_type="e",
            resource_type="r",
            outcome=AuditOutcome.SUCCESS,
            details={},
            created_at=now,
        )
        for uid in (user.id, None, uuid.uuid4())
    ]
    out = await audit_out(session, events)
    assert [e.actor_name for e in out] == [user.display_name, None, None]
    assert await audit_out(session, []) == []


async def test_user_names(session: AsyncSession, tenant: Tenant, make_user: UserFactory) -> None:
    a = await make_user(tenant, UserRole.MEMBER)
    b = await make_user(tenant, UserRole.VIEWER)
    assert await user_names(session, []) == {}
    assert await user_names(session, [None, None]) == {}
    assert await user_names(session, [a.id, b.id, a.id, None, uuid.uuid4()]) == {
        a.id: a.display_name,
        b.id: b.display_name,
    }


async def test_run_out(db: Database, session: AsyncSession, tenant: Tenant, make_user: UserFactory) -> None:
    owner = await make_user(tenant, UserRole.MEMBER)
    run = await add_run(db, await add_email(db, await add_account(session, owner)))
    async with db.session() as s:
        loaded = await s.get(WorkflowRun, run.id)
        assert loaded is not None
        out = run_out(loaded, "Subject")
    assert (out.id, out.email_subject, out.status.value) == (run.id, "Subject", "received")
    assert run_out(loaded).email_subject is None
