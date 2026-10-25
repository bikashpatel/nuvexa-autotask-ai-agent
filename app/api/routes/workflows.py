from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from app.api.deps import ActorDep, Offset, SessionDep, SettingsDep, rate_limit
from app.api.presenters import action_out, run_out
from app.core.errors import NotFound
from app.db.models import EmailAnalysis, EmailMessage, ProposedAction, WorkflowEvent, WorkflowRun
from app.domain.enums import WorkflowStatus
from app.repositories.scoping import count_rows, get_visible, visible
from app.schemas.api import (
    ActionOut,
    AnalysisOut,
    JobAccepted,
    Page,
    WorkflowDetail,
    WorkflowEventOut,
    WorkflowRunOut,
)
from app.services.authz import Actor
from app.services.workflow_service import WorkflowService

router = APIRouter(prefix="/workflows", tags=["workflows"])


async def _run(session: SessionDep, actor: Actor, run_id: uuid.UUID) -> WorkflowRun:
    return await get_visible(session, WorkflowRun, actor, run_id, label="Workflow run")


async def _latest_analysis(session: SessionDep, run: WorkflowRun) -> EmailAnalysis | None:
    return (
        await session.execute(
            select(EmailAnalysis)
            .where(EmailAnalysis.workflow_run_id == run.id, EmailAnalysis.tenant_id == run.tenant_id)
            .order_by(EmailAnalysis.attempt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.get("", response_model=Page[WorkflowRunOut])
async def list_runs(
    actor: ActorDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Offset = 0,
    status_filter: Annotated[WorkflowStatus | None, Query(alias="status")] = None,
    email_id: uuid.UUID | None = None,
) -> Page[WorkflowRunOut]:
    stmt = visible(
        select(WorkflowRun, EmailMessage.subject).join(EmailMessage, EmailMessage.id == WorkflowRun.email_message_id),
        WorkflowRun,
        actor,
    )
    if status_filter:
        stmt = stmt.where(WorkflowRun.status == status_filter)
    if email_id:
        stmt = stmt.where(WorkflowRun.email_message_id == email_id)
    total = await count_rows(session, stmt)
    rows = (await session.execute(stmt.order_by(WorkflowRun.created_at.desc()).limit(limit).offset(offset))).all()
    return Page[WorkflowRunOut](items=[run_out(r[0], r[1]) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{run_id}", response_model=WorkflowDetail)
async def get_run(run_id: uuid.UUID, actor: ActorDep, session: SessionDep, settings: SettingsDep) -> WorkflowDetail:
    run = await _run(session, actor, run_id)
    email = await session.get(EmailMessage, run.email_message_id)
    analysis = await _latest_analysis(session, run)
    actions = (
        (
            await session.execute(
                select(ProposedAction)
                .where(ProposedAction.workflow_run_id == run.id)
                .order_by(ProposedAction.created_at)
            )
        )
        .scalars()
        .all()
    )
    events = (
        (
            await session.execute(
                select(WorkflowEvent).where(WorkflowEvent.workflow_run_id == run.id).order_by(WorkflowEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    return WorkflowDetail(
        run=run_out(run, email.subject if email else None),
        analysis=AnalysisOut.model_validate(analysis) if analysis else None,
        actions=[await action_out(session, actor, a, settings=settings) for a in actions],
        events=[WorkflowEventOut.model_validate(e) for e in events],
    )


@router.get("/{run_id}/analysis", response_model=AnalysisOut)
async def get_analysis(run_id: uuid.UUID, actor: ActorDep, session: SessionDep) -> AnalysisOut:
    run = await _run(session, actor, run_id)
    analysis = await _latest_analysis(session, run)
    if analysis is None:
        raise NotFound("No analysis is available yet.")
    return AnalysisOut.model_validate(analysis)


@router.get("/{run_id}/actions", response_model=list[ActionOut])
async def get_actions(
    run_id: uuid.UUID, actor: ActorDep, session: SessionDep, settings: SettingsDep
) -> list[ActionOut]:
    run = await _run(session, actor, run_id)
    actions = (
        (
            await session.execute(
                select(ProposedAction)
                .where(ProposedAction.workflow_run_id == run.id)
                .order_by(ProposedAction.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [await action_out(session, actor, a, settings=settings) for a in actions]


@router.get("/{run_id}/events", response_model=list[WorkflowEventOut])
async def get_events(run_id: uuid.UUID, actor: ActorDep, session: SessionDep) -> list[WorkflowEventOut]:
    run = await _run(session, actor, run_id)
    events = (
        (
            await session.execute(
                select(WorkflowEvent).where(WorkflowEvent.workflow_run_id == run.id).order_by(WorkflowEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [WorkflowEventOut.model_validate(e) for e in events]


@router.post(
    "/{run_id}/reanalyze",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("analyze", 60))],
)
async def reanalyze(run_id: uuid.UUID, actor: ActorDep, session: SessionDep, settings: SettingsDep) -> JobAccepted:
    job = await WorkflowService(settings).reanalyze(session, actor, run_id)
    return JobAccepted(job_ids=[job.id], message="Re-analysis queued. Open proposals will be superseded.")


@router.post("/{run_id}/cancel", response_model=WorkflowRunOut)
async def cancel(run_id: uuid.UUID, actor: ActorDep, session: SessionDep, settings: SettingsDep) -> WorkflowRunOut:
    run = await WorkflowService(settings).cancel(session, actor, run_id)
    email = await session.get(EmailMessage, run.email_message_id)
    return run_out(run, email.subject if email else None)
