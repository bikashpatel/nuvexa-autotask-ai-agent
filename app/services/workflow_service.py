"""Request-side workflow operations: start analysis, re-analyze, cancel, queue execution."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import get_correlation_id
from app.core.errors import Conflict
from app.db.models import EmailMessage, Job, ProposedAction, WorkflowRun
from app.domain.enums import (
    ACTIVE_WORKFLOW_STATUSES,
    ActionStatus,
    EmailAnalysisStatus,
    JobType,
    WorkflowStatus,
    WorkflowTrigger,
)
from app.domain.state_machine import ensure_action_transition, ensure_workflow_transition
from app.repositories.scoping import get_visible
from app.services.approval_service import invalidate_approvals
from app.services.audit import record_audit
from app.services.authz import Actor, Perm, require, require_owner_or
from app.workers.queue import enqueue

_ANALYSIS_IN_PROGRESS = (
    WorkflowStatus.RECEIVED,
    WorkflowStatus.NORMALIZED,
    WorkflowStatus.CLASSIFIED,
    WorkflowStatus.EXTRACTED,
    WorkflowStatus.VALIDATED,
    WorkflowStatus.ACTION_PROPOSED,
)
# An external write may be in flight or its outcome is unknown: only reconciliation may move these actions, and their
# approval must stay bound to them until it does (cancelling would leave a reconcilable, re-executable action behind).
_OUTCOME_PENDING = (ActionStatus.EXECUTING, ActionStatus.UNCERTAIN)
_CANCELLABLE = (ActionStatus.PENDING_APPROVAL, ActionStatus.APPROVED, ActionStatus.FAILED)


async def active_run_for_email(session: AsyncSession, email_id: uuid.UUID) -> WorkflowRun | None:
    stmt = select(WorkflowRun).where(
        WorkflowRun.email_message_id == email_id, WorkflowRun.status.in_(list(ACTIVE_WORKFLOW_STATUSES))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def enqueue_analysis(session: AsyncSession, run: WorkflowRun, email: EmailMessage) -> Job:
    return await enqueue(
        session,
        job_type=JobType.ANALYZE_EMAIL,
        payload={"workflow_run_id": str(run.id)},
        tenant_id=run.tenant_id,
        dedupe_key=f"analyze:{run.id}",
        concurrency_key=f"run:{run.id}",
    )


async def enqueue_resume(
    session: AsyncSession, run: WorkflowRun, command: dict[str, object], dedupe: str | None
) -> Job:
    return await enqueue(
        session,
        job_type=JobType.RESUME_WORKFLOW,
        payload={"workflow_run_id": str(run.id), "command": command},
        tenant_id=run.tenant_id,
        dedupe_key=dedupe,
        concurrency_key=f"run:{run.id}",
    )


class WorkflowService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def create_run(
        self,
        session: AsyncSession,
        email: EmailMessage,
        *,
        requested_by: uuid.UUID | None,
        trigger: WorkflowTrigger,
    ) -> tuple[WorkflowRun, bool, Job | None]:
        existing = await active_run_for_email(session, email.id)
        if existing is not None:
            return existing, False, None
        run = WorkflowRun(
            tenant_id=email.tenant_id,
            owner_user_id=email.owner_user_id,
            email_message_id=email.id,
            requested_by_user_id=requested_by,
            status=WorkflowStatus.RECEIVED,
            current_step="received",
            trigger=trigger,
            correlation_id=get_correlation_id(),
            metrics={},
        )
        try:
            async with session.begin_nested():
                session.add(run)
        except IntegrityError:
            existing = await active_run_for_email(session, email.id)
            if existing is None:
                raise
            return existing, False, None
        email.analysis_status = EmailAnalysisStatus.QUEUED
        email.latest_workflow_run_id = run.id
        job = await enqueue_analysis(session, run, email)
        return run, True, job

    async def start_analysis(
        self, session: AsyncSession, actor: Actor, email_id: uuid.UUID
    ) -> tuple[WorkflowRun, bool, Job | None]:
        email = await get_visible(session, EmailMessage, actor, email_id, label="Email")
        require_owner_or(
            actor,
            email.owner_user_id,
            Perm.WORKFLOW_RUN,
            Perm.ACTION_APPROVE_ANY,
            resource_type="email",
            resource_id=str(email.id),
        )
        if email.deleted_at is not None:
            raise Conflict("The email was removed from the mailbox.")
        run, created, job = await self.create_run(
            session, email, requested_by=actor.user_id, trigger=WorkflowTrigger.MANUAL
        )
        if created:
            record_audit(
                session,
                tenant_id=actor.tenant_id,
                actor=actor,
                event_type="analysis_requested",
                resource_type="email",
                resource_id=email.id,
                workflow_run_id=run.id,
            )
        await session.commit()
        return run, created, job

    async def reanalyze(self, session: AsyncSession, actor: Actor, run_id: uuid.UUID) -> Job:
        run = await get_visible(session, WorkflowRun, actor, run_id, label="Workflow run")
        require_owner_or(
            actor,
            run.owner_user_id,
            Perm.WORKFLOW_RUN,
            Perm.ACTION_APPROVE_ANY,
            resource_type="workflow_run",
            resource_id=str(run.id),
        )
        if run.status in (WorkflowStatus.COMPLETED, WorkflowStatus.REJECTED, WorkflowStatus.CANCELLED):
            raise Conflict("This workflow has finished. Start a new analysis from the email instead.")
        if run.status is WorkflowStatus.EXECUTING or run.status in _ANALYSIS_IN_PROGRESS:
            raise Conflict("The workflow is busy. Try again when it is waiting for review.")
        busy = (
            await session.execute(
                select(ProposedAction.id).where(
                    ProposedAction.tenant_id == run.tenant_id,
                    ProposedAction.workflow_run_id == run.id,
                    ProposedAction.status.in_(_OUTCOME_PENDING),
                )
            )
        ).first()
        if busy is not None:
            raise Conflict("Resolve executing or uncertain actions before re-analysis.")
        job = await enqueue_resume(
            session, run, {"type": "reanalyze", "actor_user_id": str(actor.user_id)}, dedupe=f"reanalyze:{run.id}"
        )
        record_audit(
            session,
            tenant_id=actor.tenant_id,
            actor=actor,
            event_type="reanalysis_requested",
            resource_type="workflow_run",
            resource_id=run.id,
            workflow_run_id=run.id,
        )
        await session.commit()
        return job

    async def cancel(self, session: AsyncSession, actor: Actor, run_id: uuid.UUID) -> WorkflowRun:
        run = await get_visible(session, WorkflowRun, actor, run_id, for_update=True, label="Workflow run")
        require_owner_or(
            actor,
            run.owner_user_id,
            Perm.ACTION_EDIT,
            Perm.ACTION_APPROVE_ANY,
            resource_type="workflow_run",
            resource_id=str(run.id),
        )
        ensure_workflow_transition(run.status, WorkflowStatus.CANCELLED)
        # Lock the run's actions: an executor claiming one (approved → executing) either commits first and is seen
        # here, or waits for this transaction and then finds the action cancelled (PostgreSQL row locks).
        actions = (
            (
                await session.execute(
                    select(ProposedAction)
                    .where(
                        ProposedAction.tenant_id == run.tenant_id,
                        ProposedAction.workflow_run_id == run.id,
                        ProposedAction.status.in_(_CANCELLABLE + _OUTCOME_PENDING),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if any(action.status in _OUTCOME_PENDING for action in actions):
            raise Conflict("Resolve executing or uncertain actions before cancelling the workflow.")
        for action in actions:
            ensure_action_transition(action.status, ActionStatus.CANCELLED)
            await invalidate_approvals(session, action, "workflow cancelled")
            action.status = ActionStatus.CANCELLED
        run.status = WorkflowStatus.CANCELLED
        run.current_step = "cancelled"
        email = await session.get(EmailMessage, run.email_message_id)
        if email is not None:
            email.analysis_status = EmailAnalysisStatus.DONE
        record_audit(
            session,
            tenant_id=actor.tenant_id,
            actor=actor,
            event_type="workflow_cancelled",
            resource_type="workflow_run",
            resource_id=run.id,
            workflow_run_id=run.id,
            details={"cancelled_actions": [str(a.id) for a in actions]},
        )
        await enqueue_resume(session, run, {"type": "cancel"}, dedupe=f"cancel:{run.id}")
        await session.commit()
        return run

    async def queue_execution(
        self, session: AsyncSession, actor: Actor, action: ProposedAction, *, reconcile: bool = False
    ) -> Job:
        require(actor, Perm.ACTION_EXECUTE, resource_type="action", resource_id=str(action.id))
        run = await session.get(WorkflowRun, action.workflow_run_id)
        if run is None:
            raise Conflict("Workflow run missing.")
        command: dict[str, object] = {
            "type": "reconcile" if reconcile else "execute",
            "action_id": str(action.id),
            "actor_user_id": str(actor.user_id),
            "version": action.version,
            "payload_hash": action.payload_hash,
        }
        op = "reconcile" if reconcile else "execute"
        job = await enqueue_resume(session, run, command, dedupe=f"{op}:{action.id}:{action.version}")
        record_audit(
            session,
            tenant_id=actor.tenant_id,
            actor=actor,
            event_type=f"{op}_requested",
            resource_type="action",
            resource_id=action.id,
            action_id=action.id,
            workflow_run_id=run.id,
            details={"version": action.version, "job_id": str(job.id)},
        )
        await session.commit()
        return job
