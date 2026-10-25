"""Tasks, reply drafts, audit log, tenant settings and dashboard summary."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import ActorDep, Offset, SessionDep, SettingsDep
from app.api.presenters import audit_out
from app.core.timeutil import utcnow
from app.db.models import (
    ActionApproval,
    AuditEvent,
    EmailMessage,
    FollowUpTask,
    ProposedAction,
    ReplyDraft,
    WorkflowRun,
)
from app.domain.enums import (
    ActionStatus,
    AuditOutcome,
    EmailAnalysisStatus,
    EmailCategory,
    TaskStatus,
    WorkflowStatus,
)
from app.observability.logging import get_logger
from app.repositories.scoping import get_visible, paginate, visible
from app.schemas.api import (
    AuditEventOut,
    AuditIntegrityOut,
    DashboardSummaryOut,
    FollowUpTaskOut,
    LLMUsageSummary,
    Page,
    ReplyDraftOut,
    SettingsOut,
    SettingsUpdateRequest,
    TaskUpdateRequest,
)
from app.schemas.settings import TenantSettings
from app.services.audit import record_audit, verify_tenant_audit_trail
from app.services.authz import Perm, require, require_owner_or
from app.services.settings_service import load_settings_row, update_tenant_settings

router = APIRouter(tags=["workspace"])
log = get_logger(__name__)

Limit = Annotated[int, Query(ge=1, le=100)]


@router.get("/tasks", response_model=Page[FollowUpTaskOut])
async def list_tasks(
    actor: ActorDep,
    session: SessionDep,
    limit: Limit = 25,
    offset: Offset = 0,
    status: TaskStatus | None = None,
) -> Page[FollowUpTaskOut]:
    stmt = visible(select(FollowUpTask), FollowUpTask, actor)
    if status:
        stmt = stmt.where(FollowUpTask.status == status)
    rows, total = await paginate(session, stmt.order_by(FollowUpTask.created_at.desc()), limit=limit, offset=offset)
    return Page[FollowUpTaskOut](
        items=[FollowUpTaskOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.patch("/tasks/{task_id}", response_model=FollowUpTaskOut)
async def update_task(
    task_id: uuid.UUID, body: TaskUpdateRequest, actor: ActorDep, session: SessionDep
) -> FollowUpTaskOut:
    task = await get_visible(session, FollowUpTask, actor, task_id, for_update=True, label="Task")
    require_owner_or(
        actor,
        task.owner_user_id,
        Perm.ACTION_EDIT,
        Perm.ACTION_APPROVE_ANY,
        resource_type="follow_up_task",
        resource_id=str(task.id),
    )
    task.status = body.status
    task.completed_at = utcnow() if body.status is not TaskStatus.OPEN else None
    record_audit(
        session,
        tenant_id=actor.tenant_id,
        actor=actor,
        event_type="task_updated",
        resource_type="follow_up_task",
        resource_id=task.id,
        details={"status": body.status.value},
    )
    await session.commit()
    return FollowUpTaskOut.model_validate(task)


@router.get("/reply-drafts", response_model=Page[ReplyDraftOut])
async def list_drafts(
    actor: ActorDep, session: SessionDep, limit: Limit = 25, offset: Offset = 0
) -> Page[ReplyDraftOut]:
    stmt = visible(select(ReplyDraft), ReplyDraft, actor).order_by(ReplyDraft.created_at.desc())
    rows, total = await paginate(session, stmt, limit=limit, offset=offset)
    return Page[ReplyDraftOut](
        items=[ReplyDraftOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.get("/audit-events/integrity", response_model=AuditIntegrityOut)
async def verify_audit_integrity(actor: ActorDep, session: SessionDep) -> AuditIntegrityOut:
    """Recompute every audit event's HMAC for the tenant; any mismatch means the stored row was altered."""
    require(actor, Perm.AUDIT_READ_ALL, resource_type="audit")
    report = await verify_tenant_audit_trail(session, actor.tenant_id)
    if report.tampered:
        log.error("audit_integrity_violation", tampered=len(report.tampered), tenant_id=str(actor.tenant_id))
    return AuditIntegrityOut(
        checked=report.checked, tampered=report.tampered, unsigned=report.unsigned, verified_at=utcnow()
    )


@router.get("/audit-events", response_model=Page[AuditEventOut])
async def list_audit(
    actor: ActorDep,
    session: SessionDep,
    limit: Limit = 50,
    offset: Offset = 0,
    event_type: Annotated[str | None, Query(max_length=64)] = None,
    resource_type: Annotated[str | None, Query(max_length=64)] = None,
    resource_id: Annotated[str | None, Query(max_length=64)] = None,
    action_id: uuid.UUID | None = None,
    outcome: AuditOutcome | None = None,
) -> Page[AuditEventOut]:
    stmt = select(AuditEvent).where(AuditEvent.tenant_id == actor.tenant_id)
    if not actor.has(Perm.AUDIT_READ_ALL):
        stmt = stmt.where(AuditEvent.actor_user_id == actor.user_id)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    if resource_type:
        stmt = stmt.where(AuditEvent.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(AuditEvent.resource_id == resource_id)
    if action_id:
        stmt = stmt.where(AuditEvent.action_id == action_id)
    if outcome:
        stmt = stmt.where(AuditEvent.outcome == outcome)
    rows, total = await paginate(session, stmt.order_by(AuditEvent.created_at.desc()), limit=limit, offset=offset)
    return Page[AuditEventOut](items=await audit_out(session, rows), total=total, limit=limit, offset=offset)


@router.get("/settings", response_model=SettingsOut)
async def get_settings_route(actor: ActorDep, session: SessionDep, settings: SettingsDep) -> SettingsOut:
    row = await load_settings_row(session, actor.tenant_id, settings)
    await session.commit()
    return SettingsOut(
        settings=TenantSettings.model_validate(row.settings), version=row.version, updated_at=row.updated_at
    )


@router.put("/settings", response_model=SettingsOut)
async def put_settings(
    body: SettingsUpdateRequest, actor: ActorDep, session: SessionDep, settings: SettingsDep
) -> SettingsOut:
    row = await update_tenant_settings(
        session, actor, expected_version=body.expected_version, new=body.settings, settings=settings
    )
    return SettingsOut(
        settings=TenantSettings.model_validate(row.settings), version=row.version, updated_at=row.updated_at
    )


@router.get("/dashboard/summary", response_model=DashboardSummaryOut)
async def dashboard(actor: ActorDep, session: SessionDep) -> DashboardSummaryOut:
    def scoped_count(model: type, *conds: object) -> object:
        return visible(select(func.count()).select_from(model), model, actor).where(*conds)  # type: ignore[arg-type]

    async def scalar(stmt: object) -> int:
        return int((await session.execute(stmt)).scalar_one() or 0)  # type: ignore[call-overload]

    # Email counters only include emails still in the mailbox (as listed by GET /emails).
    in_mailbox = EmailMessage.deleted_at.is_(None)
    emails_total = await scalar(scoped_count(EmailMessage, in_mailbox))
    emails_analyzed = await scalar(
        scoped_count(
            EmailMessage,
            in_mailbox,
            EmailMessage.analysis_status.in_([EmailAnalysisStatus.AWAITING_REVIEW, EmailAnalysisStatus.DONE]),
        )
    )
    action_rows = await session.execute(
        visible(select(ProposedAction.status, func.count()), ProposedAction, actor).group_by(ProposedAction.status)
    )
    action_counts: dict[ActionStatus, int] = {k: int(v) for k, v in action_rows}
    run_rows = await session.execute(
        visible(select(WorkflowRun.status, func.count()), WorkflowRun, actor).group_by(WorkflowRun.status)
    )
    run_counts: dict[WorkflowStatus, int] = {k: int(v) for k, v in run_rows}
    category_rows = await session.execute(
        visible(select(EmailMessage.category, func.count()), EmailMessage, actor)
        .where(EmailMessage.category.is_not(None), in_mailbox)
        .group_by(EmailMessage.category)
    )
    categories: dict[EmailCategory | None, int] = {k: int(v) for k, v in category_rows}
    runs = (await session.execute(visible(select(WorkflowRun.metrics), WorkflowRun, actor))).scalars().all()
    # Approval wait: proposal creation → first human decision (approve or reject), per action.
    decisions = await session.execute(
        visible(
            select(ProposedAction.id, ProposedAction.created_at, func.min(ActionApproval.created_at))
            .join(ActionApproval, ActionApproval.action_id == ProposedAction.id)
            .group_by(ProposedAction.id, ProposedAction.created_at),
            ProposedAction,
            actor,
        )
    )
    waits = [(decided - proposed).total_seconds() for _, proposed, decided in decisions if decided and proposed]
    tokens = sum(int(m.get("total_tokens", 0)) for m in runs if m)
    costs = [float(m["estimated_cost_usd"]) for m in runs if m and "estimated_cost_usd" in m]
    latencies = [int(m["llm_latency_ms"]) for m in runs if m and "llm_latency_ms" in m]
    audit_stmt = select(AuditEvent).where(AuditEvent.tenant_id == actor.tenant_id)
    if not actor.has(Perm.AUDIT_READ_ALL):
        audit_stmt = audit_stmt.where(AuditEvent.actor_user_id == actor.user_id)
    recent = (await session.execute(audit_stmt.order_by(AuditEvent.created_at.desc()).limit(10))).scalars().all()

    def n(status: ActionStatus) -> int:
        return int(action_counts.get(status, 0))

    return DashboardSummaryOut(
        emails_total=emails_total,
        emails_analyzed=emails_analyzed,
        actions_proposed=sum(int(v) for k, v in action_counts.items() if k is not ActionStatus.SUPERSEDED),
        pending_approvals=n(ActionStatus.PENDING_APPROVAL),
        completed_actions=n(ActionStatus.COMPLETED),
        failed_actions=n(ActionStatus.FAILED),
        uncertain_actions=n(ActionStatus.UNCERTAIN),
        rejected_actions=n(ActionStatus.REJECTED),
        workflows_by_status={k.value: int(v) for k, v in run_counts.items()},
        categories={k.value: int(v) for k, v in categories.items() if k is not None},
        avg_approval_wait_seconds=round(sum(waits) / len(waits), 1) if waits else None,
        llm=LLMUsageSummary(
            total_tokens=tokens,
            estimated_cost_usd=round(sum(costs), 4) if costs else None,
            avg_latency_ms=round(sum(latencies) / len(latencies), 1) if latencies else None,
        ),
        recent_activity=await audit_out(session, recent),
    )
