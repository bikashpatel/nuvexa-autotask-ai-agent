from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import set_correlation_id
from app.core.errors import Conflict, InvalidStateTransition, NotFound
from app.core.timeutil import utcnow
from app.db.models import ActionApproval, EmailMessage, Job, ProposedAction, WorkflowRun
from app.domain.enums import (
    ActionStatus,
    ActorType,
    EmailAnalysisStatus,
    JobStatus,
    JobType,
    UserRole,
    WorkflowStatus,
    WorkflowTrigger,
)
from app.services import workflow_service
from app.services.authz import AuditedPermissionDenied, Perm
from app.services.workflow_service import WorkflowService, active_run_for_email
from tests.unit.services.conftest import FOLLOW_UP, NO_ACTION, REPLY, WI, Factory, Fresh, World

TERMINAL = [WorkflowStatus.COMPLETED, WorkflowStatus.REJECTED, WorkflowStatus.CANCELLED]
IN_PROGRESS = [
    WorkflowStatus.RECEIVED,
    WorkflowStatus.NORMALIZED,
    WorkflowStatus.CLASSIFIED,
    WorkflowStatus.EXTRACTED,
    WorkflowStatus.VALIDATED,
    WorkflowStatus.ACTION_PROPOSED,
    WorkflowStatus.EXECUTING,
]
REVIEWABLE = [WorkflowStatus.PENDING_APPROVAL, WorkflowStatus.APPROVED, WorkflowStatus.FAILED]


# ---------------------------------------------------------------------------------------------
# active_run_for_email / create_run
# ---------------------------------------------------------------------------------------------


async def test_active_run_for_email_ignores_terminal_runs(
    session: AsyncSession, factory: Factory, world: World
) -> None:
    active = await active_run_for_email(session, world.email.id)
    assert active is not None
    assert active.id == world.run.id

    email = await factory.email(world.account)
    for status in TERMINAL:
        await factory.run(email, status)
    assert await active_run_for_email(session, email.id) is None


async def test_create_run_returns_existing_active_run(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh
) -> None:
    run, created, job = await WorkflowService(settings).create_run(
        session, world.email, requested_by=world.owner.id, trigger=WorkflowTrigger.MANUAL
    )
    assert (run.id, created, job) == (world.run.id, False, None)
    await session.commit()
    assert await fresh.jobs() == []


async def test_create_run_creates_received_run_and_analysis_job(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    email = await factory.email(world.account)
    set_correlation_id("corr-create-run-1")
    run, created, job = await WorkflowService(settings).create_run(
        session, email, requested_by=world.approver.id, trigger=WorkflowTrigger.AUTO
    )
    await session.commit()

    assert created is True
    assert job is not None
    stored = await fresh.get(WorkflowRun, run.id)
    assert stored is not None
    assert stored.status is WorkflowStatus.RECEIVED
    assert stored.current_step == "received"
    assert stored.trigger is WorkflowTrigger.AUTO
    assert stored.tenant_id == world.tenant.id
    assert stored.owner_user_id == world.owner.id  # owner comes from the email, not the requester
    assert stored.requested_by_user_id == world.approver.id
    assert stored.correlation_id == "corr-create-run-1"
    assert stored.metrics == {}

    stored_email = await fresh.get(EmailMessage, email.id)
    assert stored_email is not None
    assert stored_email.analysis_status is EmailAnalysisStatus.QUEUED
    assert stored_email.latest_workflow_run_id == run.id

    [stored_job] = await fresh.jobs()
    assert stored_job.id == job.id
    assert stored_job.job_type is JobType.ANALYZE_EMAIL
    assert stored_job.status is JobStatus.QUEUED
    assert stored_job.payload == {"workflow_run_id": str(run.id)}
    assert stored_job.dedupe_key == f"analyze:{run.id}"
    assert stored_job.concurrency_key == f"run:{run.id}"
    assert stored_job.tenant_id == world.tenant.id


async def test_create_run_integrity_race_returns_concurrent_run(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[uuid.UUID] = []

    async def racing(s: AsyncSession, email_id: uuid.UUID) -> WorkflowRun | None:
        calls.append(email_id)
        if len(calls) == 1:
            return None  # the competing insert has not become visible yet
        return await active_run_for_email(s, email_id)

    monkeypatch.setattr(workflow_service, "active_run_for_email", racing)
    run, created, job = await WorkflowService(settings).create_run(
        session, world.email, requested_by=world.owner.id, trigger=WorkflowTrigger.MANUAL
    )
    assert (run.id, created, job) == (world.run.id, False, None)
    assert calls == [world.email.id, world.email.id]
    await session.commit()  # the savepoint was rolled back; the session is still usable
    assert len(await fresh.all(WorkflowRun)) == 1
    assert await fresh.jobs() == []
    stored_email = await fresh.get(EmailMessage, world.email.id)
    assert stored_email is not None
    assert stored_email.analysis_status is EmailAnalysisStatus.NOT_ANALYZED


async def test_create_run_reraises_integrity_error_without_active_run(
    session: AsyncSession, settings: Settings, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(_s: AsyncSession, _email_id: uuid.UUID) -> WorkflowRun | None:
        return None

    monkeypatch.setattr(workflow_service, "active_run_for_email", never)
    with pytest.raises(IntegrityError):
        await WorkflowService(settings).create_run(
            session, world.email, requested_by=None, trigger=WorkflowTrigger.MANUAL
        )
    await session.rollback()


# ---------------------------------------------------------------------------------------------
# start_analysis
# ---------------------------------------------------------------------------------------------


async def test_start_analysis_creates_run_and_audits_once(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    email = await factory.email(world.account)
    service = WorkflowService(settings)
    actor = world.actor(world.owner, "203.0.113.9")

    run, created, job = await service.start_analysis(session, actor, email.id)
    assert created is True
    assert job is not None
    [event] = await fresh.audits("analysis_requested")
    assert event.resource_type == "email"
    assert event.resource_id == str(email.id)
    assert event.workflow_run_id == run.id
    assert event.actor_user_id == world.owner.id
    assert event.actor_type is ActorType.USER
    assert event.ip_address == "203.0.113.9"
    stored = await fresh.get(WorkflowRun, run.id)
    assert stored is not None
    assert stored.trigger is WorkflowTrigger.MANUAL
    assert stored.requested_by_user_id == world.owner.id

    again, created_again, job_again = await service.start_analysis(session, actor, email.id)
    assert (again.id, created_again, job_again) == (run.id, False, None)
    assert len(await fresh.audits("analysis_requested")) == 1
    assert len(await fresh.jobs(job_type=JobType.ANALYZE_EMAIL)) == 1


async def test_start_analysis_refuses_deleted_email(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    email = await factory.email(world.account, deleted_at=utcnow())
    with pytest.raises(Conflict, match="removed from the mailbox"):
        await WorkflowService(settings).start_analysis(session, world.actor(world.owner), email.id)
    assert await active_run_for_email(session, email.id) is None
    assert await fresh.audits("analysis_requested") == []


async def test_start_analysis_viewer_owned_email_is_denied(
    session: AsyncSession, settings: Settings, factory: Factory, world: World
) -> None:
    account = await factory.account(world.viewer)
    email = await factory.email(account)
    with pytest.raises(AuditedPermissionDenied) as info:
        await WorkflowService(settings).start_analysis(session, world.actor(world.viewer), email.id)
    assert info.value.permission == Perm.WORKFLOW_RUN
    assert info.value.resource_type == "email"
    assert info.value.resource_id == str(email.id)


async def test_start_analysis_other_members_email_is_invisible(
    session: AsyncSession, settings: Settings, factory: Factory, world: World
) -> None:
    email = await factory.email(world.account)
    with pytest.raises(NotFound, match="Email not found"):
        await WorkflowService(settings).start_analysis(session, world.actor(world.other), email.id)


async def test_start_analysis_cross_tenant_email_is_invisible(
    session: AsyncSession, settings: Settings, factory: Factory, world: World
) -> None:
    other_tenant = await factory.tenant()
    admin = await factory.user(other_tenant, UserRole.ADMIN)
    with pytest.raises(NotFound):
        await WorkflowService(settings).start_analysis(session, world.actor(admin), world.email.id)


async def test_start_analysis_approver_may_analyze_anyones_email(
    session: AsyncSession, settings: Settings, factory: Factory, world: World
) -> None:
    email = await factory.email(world.account)
    run, created, _job = await WorkflowService(settings).start_analysis(session, world.actor(world.approver), email.id)
    assert created is True
    assert run.owner_user_id == world.owner.id
    assert run.requested_by_user_id == world.approver.id


# ---------------------------------------------------------------------------------------------
# reanalyze
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", TERMINAL)
async def test_reanalyze_refuses_finished_runs(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, status: WorkflowStatus
) -> None:
    run = await factory.run(await factory.email(world.account), status)
    with pytest.raises(Conflict, match="has finished"):
        await WorkflowService(settings).reanalyze(session, world.actor(world.owner), run.id)


@pytest.mark.parametrize("status", IN_PROGRESS)
async def test_reanalyze_refuses_busy_runs(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh, status: WorkflowStatus
) -> None:
    run = await factory.run(await factory.email(world.account), status)
    with pytest.raises(Conflict, match="busy"):
        await WorkflowService(settings).reanalyze(session, world.actor(world.owner), run.id)
    assert await fresh.jobs() == []


@pytest.mark.parametrize("action_status", [ActionStatus.EXECUTING, ActionStatus.UNCERTAIN])
async def test_reanalyze_refuses_runs_with_unresolved_writes(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, action_status: ActionStatus
) -> None:
    await factory.action(world.run, WI, status=ActionStatus.COMPLETED)
    await factory.action(world.run, REPLY, status=action_status)
    with pytest.raises(Conflict, match="executing or uncertain"):
        await WorkflowService(settings).reanalyze(session, world.actor(world.owner), world.run.id)


@pytest.mark.parametrize("status", REVIEWABLE)
async def test_reanalyze_enqueues_resume_command_and_audits(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh, status: WorkflowStatus
) -> None:
    run = await factory.run(await factory.email(world.account), status)
    await factory.action(run, WI, status=ActionStatus.FAILED)
    actor = world.actor(world.owner)
    service = WorkflowService(settings)

    job = await service.reanalyze(session, actor, run.id)
    [stored] = await fresh.jobs()
    assert stored.id == job.id
    assert stored.job_type is JobType.RESUME_WORKFLOW
    assert stored.payload == {
        "workflow_run_id": str(run.id),
        "command": {"type": "reanalyze", "actor_user_id": str(world.owner.id)},
    }
    assert stored.dedupe_key == f"reanalyze:{run.id}"
    assert stored.concurrency_key == f"run:{run.id}"
    [event] = await fresh.audits("reanalysis_requested")
    assert event.resource_type == "workflow_run"
    assert event.resource_id == str(run.id)
    assert event.workflow_run_id == run.id
    assert event.actor_user_id == world.owner.id

    again = await service.reanalyze(session, actor, run.id)
    assert again.id == job.id  # deduplicated while the first request is queued
    assert len(await fresh.jobs()) == 1


async def test_reanalyze_permissions(session: AsyncSession, settings: Settings, factory: Factory, world: World) -> None:
    service = WorkflowService(settings)
    with pytest.raises(NotFound, match="Workflow run not found"):
        await service.reanalyze(session, world.actor(world.other), world.run.id)

    viewer_account = await factory.account(world.viewer)
    viewer_run = await factory.run(await factory.email(viewer_account))
    with pytest.raises(AuditedPermissionDenied) as info:
        await service.reanalyze(session, world.actor(world.viewer), viewer_run.id)
    assert info.value.permission == Perm.WORKFLOW_RUN
    assert info.value.resource_type == "workflow_run"

    job = await service.reanalyze(session, world.actor(world.approver), world.run.id)
    assert job.payload["command"] == {"type": "reanalyze", "actor_user_id": str(world.approver.id)}


# ---------------------------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------------------------


async def test_cancel_cancels_open_actions_and_invalidates_approvals(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    pending = await factory.action(world.run, WI)
    approved, approval = await factory.approved_action(world.run, world.approver, REPLY)
    failed = await factory.action(world.run, FOLLOW_UP, status=ActionStatus.FAILED)
    completed = await factory.action(world.run, NO_ACTION, status=ActionStatus.COMPLETED)

    run = await WorkflowService(settings).cancel(session, world.actor(world.owner), world.run.id)
    assert run.status is WorkflowStatus.CANCELLED

    stored_run = await fresh.get(WorkflowRun, world.run.id)
    assert stored_run is not None
    assert stored_run.status is WorkflowStatus.CANCELLED
    assert stored_run.current_step == "cancelled"
    for action in (pending, approved, failed):
        stored = await fresh.get(ProposedAction, action.id)
        assert stored is not None
        assert stored.status is ActionStatus.CANCELLED
    stored_completed = await fresh.get(ProposedAction, completed.id)
    assert stored_completed is not None
    assert stored_completed.status is ActionStatus.COMPLETED

    stored_approval = await fresh.get(ActionApproval, approval.id)
    assert stored_approval is not None
    assert stored_approval.invalidated_at is not None
    assert stored_approval.invalidated_reason == "workflow cancelled"

    stored_email = await fresh.get(EmailMessage, world.email.id)
    assert stored_email is not None
    assert stored_email.analysis_status is EmailAnalysisStatus.DONE

    [event] = await fresh.audits("workflow_cancelled")
    assert event.resource_id == str(world.run.id)
    assert event.workflow_run_id == world.run.id
    assert event.actor_user_id == world.owner.id
    assert sorted(event.details["cancelled_actions"]) == sorted(str(a.id) for a in (pending, approved, failed))

    [job] = await fresh.jobs(job_type=JobType.RESUME_WORKFLOW)
    assert job.payload == {"workflow_run_id": str(world.run.id), "command": {"type": "cancel"}}
    assert job.dedupe_key == f"cancel:{world.run.id}"
    assert job.concurrency_key == f"run:{world.run.id}"


@pytest.mark.parametrize("status", [*TERMINAL, WorkflowStatus.EXECUTING])
async def test_cancel_refuses_terminal_and_executing_runs(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh, status: WorkflowStatus
) -> None:
    run = await factory.run(await factory.email(world.account), status)
    action_id = (await factory.action(run, WI, status=ActionStatus.APPROVED)).id
    with pytest.raises(InvalidStateTransition):
        await WorkflowService(settings).cancel(session, world.actor(world.owner), run.id)
    await session.rollback()
    stored = await fresh.get(ProposedAction, action_id)
    assert stored is not None
    assert stored.status is ActionStatus.APPROVED
    assert await fresh.audits("workflow_cancelled") == []


async def test_cancel_permissions(session: AsyncSession, settings: Settings, factory: Factory, world: World) -> None:
    service = WorkflowService(settings)
    with pytest.raises(NotFound):
        await service.cancel(session, world.actor(world.other), world.run.id)

    viewer_account = await factory.account(world.viewer)
    viewer_run = await factory.run(await factory.email(viewer_account))
    with pytest.raises(AuditedPermissionDenied) as info:
        await service.cancel(session, world.actor(world.viewer), viewer_run.id)
    assert info.value.permission == Perm.ACTION_EDIT

    run = await service.cancel(session, world.actor(world.approver), world.run.id)
    assert run.status is WorkflowStatus.CANCELLED


# ---------------------------------------------------------------------------------------------
# queue_execution
# ---------------------------------------------------------------------------------------------


async def test_queue_execution_requires_execute_permission(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    action = await factory.action(world.run, WI, status=ActionStatus.APPROVED)
    with pytest.raises(AuditedPermissionDenied) as info:
        await WorkflowService(settings).queue_execution(session, world.actor(world.viewer), action)
    assert info.value.permission == Perm.ACTION_EXECUTE
    assert info.value.resource_type == "action"
    assert info.value.resource_id == str(action.id)
    assert await fresh.jobs() == []


async def test_queue_execution_requires_existing_run(session: AsyncSession, settings: Settings, world: World) -> None:
    orphan = ProposedAction(id=uuid.uuid4(), workflow_run_id=uuid.uuid4(), version=1, payload_hash="0" * 64)
    with pytest.raises(Conflict, match="Workflow run missing"):
        await WorkflowService(settings).queue_execution(session, world.actor(world.owner), orphan)


@pytest.mark.parametrize(("reconcile", "op"), [(False, "execute"), (True, "reconcile")])
async def test_queue_execution_enqueues_bound_command_and_audits(
    session: AsyncSession,
    settings: Settings,
    factory: Factory,
    world: World,
    fresh: Fresh,
    reconcile: bool,
    op: str,
) -> None:
    action = await factory.action(world.run, WI, status=ActionStatus.APPROVED, version=3)
    job = await WorkflowService(settings).queue_execution(
        session, world.actor(world.owner), action, reconcile=reconcile
    )

    [stored] = await fresh.jobs()
    assert stored.id == job.id
    assert stored.job_type is JobType.RESUME_WORKFLOW
    assert stored.payload == {
        "workflow_run_id": str(world.run.id),
        "command": {
            "type": op,
            "action_id": str(action.id),
            "actor_user_id": str(world.owner.id),
            "version": 3,
            "payload_hash": action.payload_hash,
        },
    }
    assert stored.dedupe_key == f"{op}:{action.id}:3"
    assert stored.concurrency_key == f"run:{world.run.id}"
    [event] = await fresh.audits(f"{op}_requested")
    assert event.resource_type == "action"
    assert event.action_id == action.id
    assert event.workflow_run_id == world.run.id
    assert event.details == {"version": 3, "job_id": str(job.id)}


async def test_queue_execution_dedupes_per_version(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    service = WorkflowService(settings)
    actor = world.actor(world.owner)
    action = await factory.action(world.run, WI, status=ActionStatus.APPROVED)

    first = await service.queue_execution(session, actor, action)
    assert (await service.queue_execution(session, actor, action)).id == first.id

    action.version = 2  # edited and re-approved: a new request must not be swallowed by the old one
    await session.commit()
    second = await service.queue_execution(session, actor, action)
    assert second.id != first.id
    assert second.payload["command"]["version"] == 2

    reconcile = await service.queue_execution(session, actor, action, reconcile=True)
    assert reconcile.id not in (first.id, second.id)

    jobs = await fresh.all(Job)
    assert sorted(j.dedupe_key or "" for j in jobs) == sorted(
        [f"execute:{action.id}:1", f"execute:{action.id}:2", f"reconcile:{action.id}:2"]
    )
    assert len(await fresh.audits("execute_requested")) == 3  # every request is audited, even deduplicated ones
