from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Conflict, InvalidStateTransition, NotFound, StaleVersion, ValidationFailed
from app.core.timeutil import utcnow
from app.db.models import ActionApproval, AppSettings, Job, ProposedAction, User, WorkflowRun
from app.domain.enums import ActionStatus, ActorType, ApprovalDecision, AuditOutcome, JobType, UserRole
from app.schemas.settings import TenantSettings
from app.services.approval_service import (
    ApprovalService,
    current_approval,
    enqueue_workflow_refresh,
    invalidate_approvals,
    load_action,
    supersede_open_actions,
)
from app.services.authz import AuditedPermissionDenied, Perm
from tests.unit.services.conftest import FOLLOW_UP, NO_ACTION, REPLY, WI, Factory, Fresh, World, hash_of, payload_for

S = ActionStatus


@pytest.fixture
def svc(settings: Settings) -> ApprovalService:
    return ApprovalService(settings)


async def _viewer_owned_run(factory: Factory, world: World) -> WorkflowRun:
    account = await factory.account(world.viewer)
    return await factory.run(await factory.email(account))


async def _restrict_projects(session: AsyncSession, world: World, allowed: list[str]) -> None:
    ts = TenantSettings.model_validate({"azure_devops": {"project": "NUVEXA-Demo", "allowed_projects": allowed}})
    session.add(AppSettings(tenant_id=world.tenant.id, settings=ts.model_dump(mode="json"), version=1))
    await session.commit()


def _edited(action: ProposedAction, **changes: Any) -> dict[str, Any]:
    return {**action.payload, **changes}


async def _refresh_jobs(fresh: Fresh, run: WorkflowRun) -> list[Job]:
    return await fresh.jobs(job_type=JobType.RESUME_WORKFLOW, dedupe_key=f"refresh:{run.id}")


# ---------------------------------------------------------------------------------------------
# Helpers: load_action, current_approval, invalidate_approvals, enqueue_workflow_refresh
# ---------------------------------------------------------------------------------------------


async def test_load_action_is_owner_and_tenant_scoped(session: AsyncSession, factory: Factory, world: World) -> None:
    action = await factory.action(world.run)
    assert (await load_action(session, world.actor(world.owner), action.id)).id == action.id
    assert (await load_action(session, world.actor(world.approver), action.id, for_update=True)).id == action.id
    with pytest.raises(NotFound, match="Action not found"):
        await load_action(session, world.actor(world.other), action.id)
    outsider_tenant = await factory.tenant()
    outsider = await factory.user(outsider_tenant, UserRole.ADMIN)
    with pytest.raises(NotFound):
        await load_action(session, world.actor(outsider), action.id)


async def test_current_approval_requires_matching_live_approved_decision(
    session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run, status=S.APPROVED, version=2)
    assert await current_approval(session, action) is None
    await factory.approval(action, world.approver, invalidated=True)
    await factory.approval(action, world.approver, version=1)
    await factory.approval(action, world.approver, payload_hash="f" * 64)
    await factory.approval(action, world.approver, decision=ApprovalDecision.REJECTED)
    assert await current_approval(session, action) is None

    valid = await factory.approval(action, world.approver)
    found = await current_approval(session, action)
    assert found is not None
    assert found.id == valid.id


async def test_current_approval_picks_latest(session: AsyncSession, factory: Factory, world: World) -> None:
    action = await factory.action(world.run, status=S.APPROVED)
    now = utcnow()
    await factory.approval(action, world.owner, created_at=now - timedelta(minutes=5))
    latest = await factory.approval(action, world.approver, created_at=now)
    await factory.approval(action, world.admin, created_at=now - timedelta(minutes=1))
    found = await current_approval(session, action)
    assert found is not None
    assert found.id == latest.id


async def test_current_approval_ignores_rows_from_another_tenant(
    session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run, status=S.APPROVED)
    other_tenant = await factory.tenant()
    session.add(
        ActionApproval(
            tenant_id=other_tenant.id,
            action_id=action.id,
            decision=ApprovalDecision.APPROVED,
            decided_by_user_id=world.approver.id,
            action_version=action.version,
            payload_hash=action.payload_hash,
            created_at=utcnow(),
        )
    )
    await session.commit()
    assert await current_approval(session, action) is None


async def test_invalidate_approvals_only_touches_live_approvals(
    session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action = await factory.action(world.run, status=S.APPROVED)
    live_a = await factory.approval(action, world.owner)
    live_b = await factory.approval(action, world.approver, version=0)
    rejected = await factory.approval(action, world.approver, decision=ApprovalDecision.REJECTED)
    old = await factory.approval(action, world.approver, invalidated=True)

    assert await invalidate_approvals(session, action, "r" * 300) == 2
    await session.commit()
    rows = {a.id: a for a in await fresh.all(ActionApproval)}
    for live in (live_a, live_b):
        assert rows[live.id].invalidated_at is not None
        assert rows[live.id].invalidated_reason == "r" * 255
    assert rows[rejected.id].invalidated_at is None
    assert rows[old.id].invalidated_reason == "test"
    assert await invalidate_approvals(session, action, "again") == 0


async def test_enqueue_workflow_refresh_is_deduplicated(
    session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action = await factory.action(world.run)
    await enqueue_workflow_refresh(session, action)
    await enqueue_workflow_refresh(session, action)
    await session.commit()
    [job] = await _refresh_jobs(fresh, world.run)
    assert job.payload == {"workflow_run_id": str(world.run.id), "command": {"type": "refresh"}}
    assert job.concurrency_key == f"run:{world.run.id}"
    assert job.tenant_id == world.tenant.id


# ---------------------------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------------------------


async def test_update_creates_new_version(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action = await factory.action(world.run, validation_errors=["stale error"], last_error="boom")
    old_hash = action.payload_hash
    updated = await svc.update(
        session,
        world.actor(world.owner),
        action.id,
        expected_version=1,
        payload=_edited(action, title="Fix login now", priority=2),
    )
    assert updated.version == 2
    assert updated.payload_hash != old_hash
    assert updated.payload_hash == hash_of(WI, updated.payload)

    stored = await fresh.get(ProposedAction, action.id)
    assert stored is not None
    assert stored.version == 2
    assert stored.status is S.PENDING_APPROVAL
    assert stored.payload["title"] == "Fix login now"
    assert stored.payload["priority"] == 2
    assert stored.title == "Bug: Fix login now"
    assert stored.validation_errors == []
    assert stored.source == "user_edited"
    assert stored.updated_by_user_id == world.owner.id
    assert stored.last_error is None

    [audit] = await fresh.audits("action_edited", action_id=action.id)
    assert audit.details == {"from_version": 1, "to_version": 2, "changed_fields": ["priority", "title"]}
    assert audit.actor_user_id == world.owner.id
    assert audit.workflow_run_id == world.run.id
    assert await fresh.audits("approval_invalidated") == []
    assert len(await _refresh_jobs(fresh, world.run)) == 1


async def test_update_of_approved_action_invalidates_approval(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action, approval = await factory.approved_action(world.run, world.approver)
    await svc.update(
        session, world.actor(world.owner), action.id, expected_version=1, payload=_edited(action, title="Changed")
    )
    assert action.status is S.PENDING_APPROVAL
    assert await current_approval(session, action) is None
    stored_approval = await fresh.get(ActionApproval, approval.id)
    assert stored_approval is not None
    assert stored_approval.invalidated_at is not None
    assert stored_approval.invalidated_reason == "payload edited (v1→v2)"
    [audit] = await fresh.audits("approval_invalidated", action_id=action.id)
    assert audit.details == {"reason": "payload edited", "invalidated_approvals": 1}


async def test_update_of_failed_action_returns_it_to_review(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run, status=S.FAILED)
    await svc.update(
        session, world.actor(world.owner), action.id, expected_version=1, payload=_edited(action, title="Retry")
    )
    assert action.status is S.PENDING_APPROVAL
    assert action.version == 2


async def test_update_cannot_change_provenance(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run)
    original_source = action.payload["source_email"]
    forged = _edited(
        action,
        title="New title",
        kind="propose_email_reply",
        source_email={**original_source, "sender": "attacker@example.net", "subject": "forged"},
    )
    await svc.update(session, world.actor(world.owner), action.id, expected_version=1, payload=forged)
    assert action.payload["kind"] == "create_azure_devops_work_item"
    assert action.payload["source_email"] == original_source
    assert action.payload["title"] == "New title"


async def test_update_cannot_change_reply_recipients(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run, REPLY)
    edited = _edited(
        action,
        to=["attacker@example.net"],
        in_reply_to_internet_message_id="<forged@example.net>",
        body="Updated body",
    )
    await svc.update(session, world.actor(world.owner), action.id, expected_version=1, payload=edited)
    assert action.payload["to"] == ["requester@example.com"]
    assert action.payload["in_reply_to_internet_message_id"] == "<incident-42@example.com>"
    assert action.payload["body"] == "Updated body"
    assert action.title == "Reply draft: Re: Production login failures"


async def test_update_with_identical_payload_is_a_no_op(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action, approval = await factory.approved_action(world.run, world.approver)
    result = await svc.update(
        session, world.actor(world.owner), action.id, expected_version=1, payload=dict(action.payload)
    )
    assert result.version == 1
    assert result.status is S.APPROVED
    assert await fresh.audits() == []
    assert await fresh.jobs() == []
    assert (await fresh.get(ActionApproval, approval.id)).invalidated_at is None  # type: ignore[union-attr]


@pytest.mark.parametrize("status", [S.REJECTED, S.CANCELLED, S.EXECUTING, S.COMPLETED, S.UNCERTAIN, S.SUPERSEDED])
async def test_update_refused_for_non_editable_status(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, status: ActionStatus
) -> None:
    action = await factory.action(world.run, status=status)
    with pytest.raises(Conflict) as info:
        await svc.update(session, world.actor(world.owner), action.id, expected_version=1, payload=_edited(action))
    assert type(info.value) is Conflict
    assert f"status '{status}' cannot be edited" in info.value.message


async def test_update_rejects_stale_version(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run, version=3)
    with pytest.raises(StaleVersion):
        await svc.update(session, world.actor(world.owner), action.id, expected_version=2, payload=_edited(action))


async def test_update_schema_errors_are_reported_with_locations(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action = await factory.action(world.run)
    with pytest.raises(ValidationFailed) as info:
        await svc.update(
            session, world.actor(world.owner), action.id, expected_version=1, payload=_edited(action, priority=9)
        )
    assert info.value.status_code == 422
    assert info.value.message == "The edited action is invalid."
    [detail] = info.value.details
    assert detail["loc"] == ["create_azure_devops_work_item", "priority"]
    assert "less than or equal to 4" in detail["msg"]
    stored = await fresh.get(ProposedAction, action.id)
    assert stored is not None
    assert stored.version == 1


async def test_update_rejects_unknown_fields(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run)
    with pytest.raises(ValidationFailed) as info:
        await svc.update(
            session,
            world.actor(world.owner),
            action.id,
            expected_version=1,
            payload=_edited(action, organization="evil-org", url="https://evil.example"),
        )
    assert {tuple(d["loc"])[-1] for d in info.value.details} == {"organization", "url"}


async def test_update_reports_kind_mismatch_of_stored_payload(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    task_payload = payload_for(FOLLOW_UP, world.email)
    action = await factory.action(world.run, WI, payload=task_payload)
    with pytest.raises(ValidationFailed) as info:
        await svc.update(session, world.actor(world.owner), action.id, expected_version=1, payload=task_payload)
    assert info.value.details == [{"msg": "Payload kind does not match the action type."}]


async def test_update_rejects_policy_violations(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run)
    with pytest.raises(ValidationFailed) as info:
        await svc.update(
            session,
            world.actor(world.owner),
            action.id,
            expected_version=1,
            payload=_edited(action, work_item_type="Spike", additional_fields={"Custom.Field": "x"}),
        )
    assert info.value.message == "The edited action violates policy."
    assert info.value.details == [
        {"msg": "Work item type 'Spike' is not allowed."},
        {"msg": "Field 'Custom.Field' is not in the allowed field list."},
    ]


async def test_update_permissions(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action = await factory.action(world.run)
    with pytest.raises(NotFound):
        await svc.update(session, world.actor(world.other), action.id, expected_version=1, payload=_edited(action))

    viewer_action = await factory.action(await _viewer_owned_run(factory, world))
    with pytest.raises(AuditedPermissionDenied) as info:
        await svc.update(
            session, world.actor(world.viewer), viewer_action.id, expected_version=1, payload=_edited(viewer_action)
        )
    assert info.value.permission == Perm.ACTION_EDIT
    assert info.value.resource_id == str(viewer_action.id)

    await svc.update(
        session,
        world.actor(world.approver),
        action.id,
        expected_version=1,
        payload=_edited(action, title="By approver"),
    )
    stored = await fresh.get(ProposedAction, action.id)
    assert stored is not None
    assert stored.updated_by_user_id == world.approver.id


# ---------------------------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------------------------


async def test_owner_approves_own_action(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action = await factory.action(world.run)
    result = await svc.approve(
        session,
        world.actor(world.owner, ip="192.0.2.1"),
        action.id,
        version=1,
        payload_hash=action.payload_hash,
        comment="LGTM",
    )
    assert result.status is S.APPROVED

    [approval] = await fresh.all(ActionApproval)
    assert approval.decision is ApprovalDecision.APPROVED
    assert approval.decided_by_user_id == world.owner.id
    assert approval.action_version == 1
    assert approval.payload_hash == action.payload_hash
    assert approval.reason == "LGTM"
    assert approval.invalidated_at is None

    stored = await fresh.get(ProposedAction, action.id)
    assert stored is not None
    assert stored.status is S.APPROVED
    assert stored.updated_by_user_id == world.owner.id
    [audit] = await fresh.audits("action_approved", action_id=action.id)
    assert audit.details == {
        "approval_id": str(approval.id),
        "version": 1,
        "payload_hash": action.payload_hash,
        "action_type": WI.value,
        "own_action": True,
    }
    assert audit.ip_address == "192.0.2.1"
    assert len(await _refresh_jobs(fresh, world.run)) == 1
    assert (await current_approval(session, action)).id == approval.id  # type: ignore[union-attr]


async def test_approver_approves_someone_elses_action(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action = await factory.action(world.run, REPLY)
    await svc.approve(
        session, world.actor(world.approver), action.id, version=1, payload_hash=action.payload_hash, comment=None
    )
    [audit] = await fresh.audits("action_approved")
    assert audit.details["own_action"] is False
    assert audit.details["action_type"] == REPLY.value
    assert audit.actor_user_id == world.approver.id


async def test_approve_permissions(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action = await factory.action(world.run)
    with pytest.raises(NotFound):
        await svc.approve(
            session, world.actor(world.other), action.id, version=1, payload_hash=action.payload_hash, comment=None
        )
    viewer_action = await factory.action(await _viewer_owned_run(factory, world))
    with pytest.raises(AuditedPermissionDenied) as info:
        await svc.approve(
            session,
            world.actor(world.viewer),
            viewer_action.id,
            version=1,
            payload_hash=viewer_action.payload_hash,
            comment=None,
        )
    assert info.value.permission == Perm.ACTION_APPROVE
    assert await fresh.all(ActionApproval) == []


@pytest.mark.parametrize(("version", "use_hash"), [(2, True), (1, False)])
async def test_approve_rejects_stale_version_or_hash(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, version: int, use_hash: bool
) -> None:
    action = await factory.action(world.run)
    with pytest.raises(StaleVersion, match="Review the latest version before approving"):
        await svc.approve(
            session,
            world.actor(world.owner),
            action.id,
            version=version,
            payload_hash=action.payload_hash if use_hash else "0" * 64,
            comment=None,
        )


async def test_approve_detects_tampered_payload(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action = await factory.action(world.run)
    action.payload = {**action.payload, "project": "Attacker-Project"}  # hash left untouched
    await session.commit()
    with pytest.raises(Conflict, match="does not match its hash"):
        await svc.approve(
            session, world.actor(world.owner), action.id, version=1, payload_hash=action.payload_hash, comment=None
        )
    assert await fresh.all(ActionApproval) == []


async def test_re_approving_the_same_version_is_idempotent(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action, approval = await factory.approved_action(world.run, world.approver)
    result = await svc.approve(
        session, world.actor(world.owner), action.id, version=1, payload_hash=action.payload_hash, comment="again"
    )
    assert result.status is S.APPROVED
    assert [a.id for a in await fresh.all(ActionApproval)] == [approval.id]
    assert await fresh.audits() == []


async def test_approved_status_without_live_approval_cannot_be_re_approved(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run, status=S.APPROVED)
    await factory.approval(action, world.approver, invalidated=True)
    with pytest.raises(InvalidStateTransition):
        await svc.approve(
            session, world.actor(world.owner), action.id, version=1, payload_hash=action.payload_hash, comment=None
        )


@pytest.mark.parametrize(
    "status", [S.REJECTED, S.CANCELLED, S.COMPLETED, S.FAILED, S.EXECUTING, S.UNCERTAIN, S.SUPERSEDED]
)
async def test_approve_refused_from_non_pending_status(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, status: ActionStatus
) -> None:
    action = await factory.action(world.run, status=status)
    with pytest.raises(InvalidStateTransition):
        await svc.approve(
            session, world.actor(world.owner), action.id, version=1, payload_hash=action.payload_hash, comment=None
        )
    assert await fresh.all(ActionApproval) == []


async def test_approve_blocked_by_validation_errors(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run, validation_errors=["No Azure DevOps project is configured."])
    with pytest.raises(Conflict) as info:
        await svc.approve(
            session, world.actor(world.owner), action.id, version=1, payload_hash=action.payload_hash, comment=None
        )
    assert info.value.message == "Resolve validation errors before approving."
    assert info.value.details == [{"msg": "No Azure DevOps project is configured."}]


@pytest.mark.parametrize("kind_mismatch", [False, True])
async def test_approve_blocked_by_invalid_stored_payload(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, kind_mismatch: bool
) -> None:
    # The stored hash is consistent with the payload, but the payload itself is not valid for the action.
    payload = payload_for(NO_ACTION, world.email) if kind_mismatch else {**payload_for(WI, world.email), "priority": 9}
    action = await factory.action(world.run, WI, payload=payload)
    with pytest.raises(Conflict, match="payload is invalid"):
        await svc.approve(
            session, world.actor(world.owner), action.id, version=1, payload_hash=action.payload_hash, comment=None
        )


async def test_approve_blocked_by_current_policy(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run)
    await _restrict_projects(session, world, ["Customer-Portal"])  # tightened after the proposal was made
    with pytest.raises(Conflict) as info:
        await svc.approve(
            session, world.actor(world.owner), action.id, version=1, payload_hash=action.payload_hash, comment=None
        )
    assert info.value.message == "The action violates policy; edit it before approving."
    assert info.value.details == [{"msg": "Project 'NUVEXA-Demo' is not in the allowed project list."}]


async def test_approve_blocked_when_reply_recipient_was_altered(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    payload = payload_for(REPLY, world.email, to=["attacker@example.net"])
    action = await factory.action(world.run, REPLY, payload=payload)
    with pytest.raises(Conflict) as info:
        await svc.approve(
            session, world.actor(world.approver), action.id, version=1, payload_hash=action.payload_hash, comment=None
        )
    assert info.value.details == [{"msg": "Reply recipients must be exactly the original sender."}]


# ---------------------------------------------------------------------------------------------
# reject / cancel
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [S.PENDING_APPROVAL, S.APPROVED])
async def test_reject_records_decision_and_invalidates_approvals(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, status: ActionStatus
) -> None:
    action = await factory.action(world.run, status=status)
    prior = await factory.approval(action, world.owner) if status is S.APPROVED else None
    result = await svc.reject(session, world.actor(world.approver), action.id, reason="Not our team")
    assert result.status is S.REJECTED

    stored = await fresh.get(ProposedAction, action.id)
    assert stored is not None
    assert stored.status is S.REJECTED
    assert stored.updated_by_user_id == world.approver.id
    rejection = [a for a in await fresh.all(ActionApproval) if a.decision is ApprovalDecision.REJECTED]
    assert len(rejection) == 1
    assert rejection[0].reason == "Not our team"
    assert rejection[0].decided_by_user_id == world.approver.id
    assert rejection[0].action_version == 1
    if prior is not None:
        invalidated = await fresh.get(ActionApproval, prior.id)
        assert invalidated is not None
        assert invalidated.invalidated_reason == "action rejected"
    [audit] = await fresh.audits("action_rejected", action_id=action.id)
    assert audit.details == {"version": 1, "reason": "Not our team"}
    assert len(await _refresh_jobs(fresh, world.run)) == 1


@pytest.mark.parametrize(
    "status", [S.FAILED, S.COMPLETED, S.EXECUTING, S.UNCERTAIN, S.CANCELLED, S.REJECTED, S.SUPERSEDED]
)
async def test_reject_refused_for_other_statuses(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, status: ActionStatus
) -> None:
    action = await factory.action(world.run, status=status)
    with pytest.raises(Conflict, match="cannot be rejected"):
        await svc.reject(session, world.actor(world.owner), action.id, reason="no")
    assert await fresh.audits() == []


async def test_reject_requires_approve_permission(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    viewer_action = await factory.action(await _viewer_owned_run(factory, world))
    with pytest.raises(AuditedPermissionDenied) as info:
        await svc.reject(session, world.actor(world.viewer), viewer_action.id, reason="no")
    assert info.value.permission == Perm.ACTION_APPROVE
    assert (info.value.resource_type, info.value.resource_id) == ("action", str(viewer_action.id))  # audited target


@pytest.mark.parametrize("status", [S.PENDING_APPROVAL, S.APPROVED, S.FAILED])
async def test_cancel_invalidates_approvals(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, status: ActionStatus
) -> None:
    action = await factory.action(world.run, status=status)
    approval = await factory.approval(action, world.approver)
    result = await svc.cancel(session, world.actor(world.owner), action.id, reason=None)
    assert result.status is S.CANCELLED
    stored = await fresh.get(ProposedAction, action.id)
    assert stored is not None
    assert stored.status is S.CANCELLED
    assert stored.updated_by_user_id == world.owner.id
    invalidated = await fresh.get(ActionApproval, approval.id)
    assert invalidated is not None
    assert invalidated.invalidated_reason == "action cancelled"
    [audit] = await fresh.audits("action_cancelled", action_id=action.id)
    assert audit.details == {"version": 1, "reason": None}
    assert len(await _refresh_jobs(fresh, world.run)) == 1


@pytest.mark.parametrize("status", [S.COMPLETED, S.REJECTED, S.CANCELLED, S.EXECUTING, S.UNCERTAIN, S.SUPERSEDED])
async def test_cancel_refused_for_terminal_or_running_status(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, status: ActionStatus
) -> None:
    action = await factory.action(world.run, status=status)
    with pytest.raises(InvalidStateTransition):
        await svc.cancel(session, world.actor(world.owner), action.id, reason="stop")


async def test_cancel_requires_edit_permission(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World
) -> None:
    viewer_action = await factory.action(await _viewer_owned_run(factory, world))
    with pytest.raises(AuditedPermissionDenied) as info:
        await svc.cancel(session, world.actor(world.viewer), viewer_action.id, reason=None)
    assert info.value.permission == Perm.ACTION_EDIT
    other_action = await factory.action(world.run)
    await svc.cancel(session, world.actor(world.admin), other_action.id, reason="admin override")
    assert other_action.status is S.CANCELLED


# ---------------------------------------------------------------------------------------------
# supersede_open_actions (re-analysis)
# ---------------------------------------------------------------------------------------------


async def test_supersede_open_actions_releases_open_proposals(
    session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    pending = await factory.action(world.run, WI, status=S.PENDING_APPROVAL)
    approved, approval = await factory.approved_action(world.run, world.approver, REPLY)
    failed = await factory.action(world.run, FOLLOW_UP, status=S.FAILED)
    completed = await factory.action(world.run, NO_ACTION, status=S.COMPLETED)
    rejected = await factory.action(world.run, WI, status=S.REJECTED, slot=1)
    uncertain = await factory.action(world.run, WI, status=S.UNCERTAIN, slot=2)

    ids = await supersede_open_actions(session, world.run, "re-analysis requested")
    assert set(ids) == {pending.id, approved.id, failed.id}
    assert (await fresh.get(ProposedAction, pending.id)).status is S.PENDING_APPROVAL  # type: ignore[union-attr]
    await session.commit()  # the caller owns the transaction

    statuses = {a.id: a.status for a in await fresh.all(ProposedAction)}
    assert statuses == {
        pending.id: S.SUPERSEDED,
        approved.id: S.SUPERSEDED,
        failed.id: S.SUPERSEDED,
        completed.id: S.COMPLETED,
        rejected.id: S.REJECTED,
        uncertain.id: S.UNCERTAIN,
    }
    stored_approval = await fresh.get(ActionApproval, approval.id)
    assert stored_approval is not None
    assert stored_approval.invalidated_reason == "re-analysis requested"
    audits = await fresh.audits("action_superseded")
    assert {a.action_id for a in audits} == set(ids)
    assert all(a.actor_type is ActorType.SYSTEM and a.outcome is AuditOutcome.SUCCESS for a in audits)
    assert all(a.details == {"reason": "re-analysis requested"} and a.workflow_run_id == world.run.id for a in audits)
    assert all((a.resource_type, a.resource_id) == ("action", str(a.action_id)) for a in audits)


async def test_supersede_with_nothing_open(session: AsyncSession, factory: Factory, world: World) -> None:
    await factory.action(world.run, status=S.COMPLETED)
    assert await supersede_open_actions(session, world.run, "noop") == []


async def test_admin_of_another_tenant_cannot_review_the_action(
    svc: ApprovalService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    outsider_tenant = await factory.tenant()
    outsider: User = await factory.user(outsider_tenant, UserRole.ADMIN)
    action = await factory.action(world.run)
    with pytest.raises(NotFound):  # invisible, not forbidden: no information leak across tenants
        await svc.approve(
            session, world.actor(outsider), action.id, version=1, payload_hash=action.payload_hash, comment=None
        )
    with pytest.raises(NotFound):
        await svc.reject(session, world.actor(outsider), action.id, reason="x")
    assert await fresh.all(ActionApproval) == []
