"""ORM → API model conversion, including per-user action permission flags for the UI."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ActionApproval, ActionExecution, AuditEvent, EmailMessage, ProposedAction, User, WorkflowRun
from app.domain.enums import ActionStatus
from app.domain.state_machine import EDITABLE_ACTION_STATUSES, EXECUTABLE_ACTION_STATUSES
from app.schemas.api import (
    ActionDetail,
    ActionOut,
    ActionPermissions,
    ApprovalOut,
    AuditEventOut,
    EmailDetail,
    ExecutionOut,
    RecipientOut,
    WorkflowRunOut,
)
from app.services.approval_service import current_approval
from app.services.authz import Actor, Perm
from app.services.execution_service import has_live_execution


async def user_names(session: AsyncSession, ids: Sequence[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = (await session.execute(select(User.id, User.display_name).where(User.id.in_(wanted)))).all()
    return {r[0]: r[1] for r in rows}


def action_permissions(
    actor: Actor, action: ProposedAction, has_valid_approval: bool, *, stale_execution: bool = False
) -> ActionPermissions:
    """`stale_execution`: the action is executing but no worker holds a live execution (it can be reconciled)."""
    own = action.owner_user_id == actor.user_id
    can_edit = (own and actor.has(Perm.ACTION_EDIT)) or actor.has(Perm.ACTION_APPROVE_ANY)
    can_decide = (own and actor.has(Perm.ACTION_APPROVE)) or actor.has(Perm.ACTION_APPROVE_ANY)
    can_exec = (own and actor.has(Perm.ACTION_EXECUTE)) or actor.has(Perm.ACTION_APPROVE_ANY)
    return ActionPermissions(
        can_edit=can_edit and action.status in EDITABLE_ACTION_STATUSES,
        can_approve=can_decide and action.status is ActionStatus.PENDING_APPROVAL and not action.validation_errors,
        can_reject=can_decide and action.status in (ActionStatus.PENDING_APPROVAL, ActionStatus.APPROVED),
        can_cancel=can_edit
        and action.status in (ActionStatus.PENDING_APPROVAL, ActionStatus.APPROVED, ActionStatus.FAILED),
        can_execute=can_exec and action.status in EXECUTABLE_ACTION_STATUSES and has_valid_approval,
        can_reconcile=can_exec
        and (action.status is ActionStatus.UNCERTAIN or (action.status is ActionStatus.EXECUTING and stale_execution)),
    )


def approval_out(a: ActionApproval, names: dict[uuid.UUID, str]) -> ApprovalOut:
    out = ApprovalOut.model_validate(a)
    out.decided_by_name = names.get(a.decided_by_user_id)
    return out


async def action_out(session: AsyncSession, actor: Actor, action: ProposedAction, *, settings: Settings) -> ActionOut:
    approval = await current_approval(session, action)
    names = await user_names(session, [approval.decided_by_user_id] if approval else [])
    out = ActionOut.model_validate(action)
    out.current_approval = approval_out(approval, names) if approval else None
    stale = action.status is ActionStatus.EXECUTING and not await has_live_execution(
        session, action, lease_seconds=settings.job_lease_seconds
    )
    out.permissions = action_permissions(actor, action, approval is not None, stale_execution=stale)
    email = await session.get(EmailMessage, action.email_message_id)
    out.email_subject = email.subject if email else None
    return out


async def action_detail(
    session: AsyncSession, actor: Actor, action: ProposedAction, *, settings: Settings
) -> ActionDetail:
    base = await action_out(session, actor, action, settings=settings)
    approvals = (
        (
            await session.execute(
                select(ActionApproval).where(ActionApproval.action_id == action.id).order_by(ActionApproval.created_at)
            )
        )
        .scalars()
        .all()
    )
    executions = (
        (
            await session.execute(
                select(ActionExecution)
                .where(ActionExecution.action_id == action.id)
                .order_by(ActionExecution.attempt_number)
            )
        )
        .scalars()
        .all()
    )
    names = await user_names(session, [a.decided_by_user_id for a in approvals])
    return ActionDetail(
        **base.model_dump(),
        approvals=[approval_out(a, names) for a in approvals],
        executions=[ExecutionOut.model_validate(e) for e in executions],
    )


def run_out(run: WorkflowRun, subject: str | None = None) -> WorkflowRunOut:
    out = WorkflowRunOut.model_validate(run)
    out.email_subject = subject
    return out


def email_detail(email: EmailMessage) -> EmailDetail:
    data = {c: getattr(email, c) for c in EmailDetail.model_fields if hasattr(email, c)}
    data["to_recipients"] = [RecipientOut(**r) for r in email.to_recipients]
    data["cc_recipients"] = [RecipientOut(**r) for r in email.cc_recipients]
    data["body_purged"] = email.body_purged_at is not None
    return EmailDetail.model_validate(data)


async def audit_out(session: AsyncSession, events: Sequence[AuditEvent]) -> list[AuditEventOut]:
    names = await user_names(session, [e.actor_user_id for e in events])
    result = []
    for e in events:
        out = AuditEventOut.model_validate(e)
        out.actor_name = names.get(e.actor_user_id) if e.actor_user_id else None
        result.append(out)
    return result
