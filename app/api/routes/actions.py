from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import or_, select

from app.api.deps import ActorDep, Offset, SessionDep, SettingsDep, rate_limit
from app.api.presenters import action_detail, action_out, audit_out
from app.db.models import ActionExecution, AuditEvent, ProposedAction
from app.domain.enums import ActionStatus, ActionType
from app.repositories.scoping import paginate, visible
from app.schemas.api import (
    ActionDetail,
    ActionOut,
    ActionUpdateRequest,
    ApproveRequest,
    AuditEventOut,
    CancelRequest,
    ExecuteAccepted,
    ExecuteRequest,
    ExecutionOut,
    Page,
    RejectRequest,
)
from app.services.approval_service import ApprovalService, load_action
from app.services.execution_service import ExecutionService
from app.services.integration_clients import make_ado_client_factory
from app.services.workflow_service import WorkflowService

router = APIRouter(prefix="/actions", tags=["actions"])

_review_limit = Depends(rate_limit("review", 120))


@router.get("", response_model=Page[ActionOut])
async def list_actions(
    actor: ActorDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Offset = 0,
    status_filter: Annotated[list[ActionStatus] | None, Query(alias="status")] = None,
    action_type: ActionType | None = None,
    workflow_run_id: uuid.UUID | None = None,
    *,
    settings: SettingsDep,
) -> Page[ActionOut]:
    stmt = visible(select(ProposedAction), ProposedAction, actor)
    if status_filter:
        stmt = stmt.where(or_(*[ProposedAction.status == s for s in status_filter]))
    if action_type:
        stmt = stmt.where(ProposedAction.action_type == action_type)
    if workflow_run_id:
        stmt = stmt.where(ProposedAction.workflow_run_id == workflow_run_id)
    rows, total = await paginate(session, stmt.order_by(ProposedAction.created_at.desc()), limit=limit, offset=offset)
    return Page[ActionOut](
        items=[await action_out(session, actor, a, settings=settings) for a in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{action_id}", response_model=ActionDetail)
async def get_action(action_id: uuid.UUID, actor: ActorDep, session: SessionDep, settings: SettingsDep) -> ActionDetail:
    return await action_detail(session, actor, await load_action(session, actor, action_id), settings=settings)


@router.patch("/{action_id}", response_model=ActionDetail, dependencies=[_review_limit])
async def update_action(
    action_id: uuid.UUID, body: ActionUpdateRequest, actor: ActorDep, session: SessionDep, settings: SettingsDep
) -> ActionDetail:
    action = await ApprovalService(settings).update(
        session, actor, action_id, expected_version=body.expected_version, payload=body.payload
    )
    return await action_detail(session, actor, action, settings=settings)


@router.post("/{action_id}/approve", response_model=ActionDetail, dependencies=[_review_limit])
async def approve(
    action_id: uuid.UUID, body: ApproveRequest, actor: ActorDep, session: SessionDep, settings: SettingsDep
) -> ActionDetail:
    action = await ApprovalService(settings).approve(
        session, actor, action_id, version=body.version, payload_hash=body.payload_hash, comment=body.comment
    )
    return await action_detail(session, actor, action, settings=settings)


@router.post("/{action_id}/reject", response_model=ActionDetail, dependencies=[_review_limit])
async def reject(
    action_id: uuid.UUID, body: RejectRequest, actor: ActorDep, session: SessionDep, settings: SettingsDep
) -> ActionDetail:
    action = await ApprovalService(settings).reject(session, actor, action_id, reason=body.reason)
    return await action_detail(session, actor, action, settings=settings)


@router.post("/{action_id}/cancel", response_model=ActionDetail, dependencies=[_review_limit])
async def cancel(
    action_id: uuid.UUID, body: CancelRequest, actor: ActorDep, session: SessionDep, settings: SettingsDep
) -> ActionDetail:
    action = await ApprovalService(settings).cancel(session, actor, action_id, reason=body.reason)
    return await action_detail(session, actor, action, settings=settings)


@router.post(
    "/{action_id}/execute",
    response_model=ExecuteAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("execute", 30))],
)
async def execute(
    action_id: uuid.UUID, body: ExecuteRequest, actor: ActorDep, session: SessionDep, settings: SettingsDep
) -> ExecuteAccepted:
    service = ExecutionService(settings, make_ado_client_factory(settings))
    action = await service.preflight(session, actor, action_id, version=body.version, payload_hash=body.payload_hash)
    job = await WorkflowService(settings).queue_execution(session, actor, action)
    return ExecuteAccepted(action=await action_out(session, actor, action, settings=settings), job_id=job.id)


@router.post(
    "/{action_id}/reconcile",
    response_model=ExecuteAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("execute", 30))],
)
async def reconcile(
    action_id: uuid.UUID, actor: ActorDep, session: SessionDep, settings: SettingsDep
) -> ExecuteAccepted:
    # Uncertain actions, and executing ones whose worker is gone (stale lease); a live execution is a 409.
    service = ExecutionService(settings, make_ado_client_factory(settings))
    action = await service.reconcile_preflight(session, actor, action_id)
    job = await WorkflowService(settings).queue_execution(session, actor, action, reconcile=True)
    return ExecuteAccepted(action=await action_out(session, actor, action, settings=settings), job_id=job.id)


@router.get("/{action_id}/executions", response_model=list[ExecutionOut])
async def executions(action_id: uuid.UUID, actor: ActorDep, session: SessionDep) -> list[ExecutionOut]:
    action = await load_action(session, actor, action_id)
    rows = (
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
    return [ExecutionOut.model_validate(r) for r in rows]


@router.get("/{action_id}/history", response_model=list[AuditEventOut])
async def history(action_id: uuid.UUID, actor: ActorDep, session: SessionDep) -> list[AuditEventOut]:
    action = await load_action(session, actor, action_id)
    rows = (
        (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == actor.tenant_id, AuditEvent.action_id == action.id)
                .order_by(AuditEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    return await audit_out(session, rows)
