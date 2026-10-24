from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select

from app.api.deps import ActorDep, SessionDep, SettingsDep, rate_limit
from app.api.presenters import email_detail, run_out
from app.db.models import EmailMessage
from app.domain.enums import EmailAnalysisStatus, EmailCategory, Priority
from app.repositories.scoping import get_visible, paginate, visible
from app.schemas.api import EmailDetail, EmailListItem, JobAccepted, Page, StartAnalysisOut, SyncRequest
from app.services.authz import Perm, require
from app.services.email_sync_service import EmailSyncService
from app.services.workflow_service import WorkflowService

router = APIRouter(prefix="/emails", tags=["emails"])

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


@router.get("", response_model=Page[EmailListItem])
async def list_emails(
    actor: ActorDep,
    session: SessionDep,
    limit: Limit = 25,
    offset: Offset = 0,
    q: Annotated[str | None, Query(max_length=200)] = None,
    category: EmailCategory | None = None,
    priority: Priority | None = None,
    analysis_status: EmailAnalysisStatus | None = None,
    connected_account_id: uuid.UUID | None = None,
) -> Page[EmailListItem]:
    require(actor, Perm.EMAIL_READ, resource_type="email")
    stmt = visible(select(EmailMessage), EmailMessage, actor).where(EmailMessage.deleted_at.is_(None))
    if q:
        pattern = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(
            or_(
                EmailMessage.subject.ilike(pattern, escape="\\"),
                EmailMessage.sender_address.ilike(pattern, escape="\\"),
                EmailMessage.sender_name.ilike(pattern, escape="\\"),
            )
        )
    if category:
        stmt = stmt.where(EmailMessage.category == category)
    if priority:
        stmt = stmt.where(EmailMessage.priority == priority)
    if analysis_status:
        stmt = stmt.where(EmailMessage.analysis_status == analysis_status)
    if connected_account_id:
        stmt = stmt.where(EmailMessage.connected_account_id == connected_account_id)
    rows, total = await paginate(
        session, stmt.order_by(EmailMessage.received_at.desc(), EmailMessage.id), limit=limit, offset=offset
    )
    return Page[EmailListItem](
        items=[EmailListItem.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{email_id}", response_model=EmailDetail)
async def get_email(email_id: uuid.UUID, actor: ActorDep, session: SessionDep) -> EmailDetail:
    require(actor, Perm.EMAIL_READ, resource_type="email")
    return email_detail(await get_visible(session, EmailMessage, actor, email_id, label="Email"))


@router.post(
    "/sync",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("sync", 10))],
)
async def sync(body: SyncRequest, actor: ActorDep, session: SessionDep, settings: SettingsDep) -> JobAccepted:
    job_ids = await EmailSyncService(settings).request_sync(session, actor, body.connected_account_id)
    return JobAccepted(job_ids=job_ids, message=f"Mailbox synchronization queued for {len(job_ids)} account(s).")


@router.post(
    "/{email_id}/analyze",
    response_model=StartAnalysisOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("analyze", 60))],
)
async def analyze(
    email_id: uuid.UUID, actor: ActorDep, session: SessionDep, settings: SettingsDep
) -> StartAnalysisOut | JSONResponse:
    run, created, job = await WorkflowService(settings).start_analysis(session, actor, email_id)
    email = await session.get(EmailMessage, email_id)
    out = StartAnalysisOut(
        workflow_run=run_out(run, email.subject if email else None), created=created, job_id=job.id if job else None
    )
    if not created:
        return JSONResponse(status_code=status.HTTP_200_OK, content=out.model_dump(mode="json"))
    return out
