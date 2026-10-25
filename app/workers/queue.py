"""Durable PostgreSQL-backed job queue.

* `enqueue` deduplicates on `dedupe_key` while a job with that key is queued or running.
* `claim_next` uses `FOR UPDATE SKIP LOCKED` (PostgreSQL) and never runs two jobs with the same
  `concurrency_key` at once (enforced by a partial unique index, so it also holds across workers).
* Failed retryable jobs are re-queued with exponential backoff; expired leases are recovered.
* `complete`, `fail` and `heartbeat` are fenced by the lease holder (`locked_by` + `attempts`): a stalled worker
  whose lease was recovered and handed to another worker cannot release or extend the new holder's lease.
* Rows whose `job_type` this version does not know (written by a newer release during a rolling deploy) are
  never loaded: they stay queued for a worker that can decode them instead of blocking the queue.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.context import get_correlation_id
from app.core.timeutil import utcnow
from app.db.models import Job
from app.domain.enums import JobStatus, JobType
from app.observability.logging import get_logger

log = get_logger(__name__)

_ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING)
# Only rows this version can decode are ever loaded (an unknown job_type would raise while building the Job).
_KNOWN_TYPES = tuple(JobType)


async def enqueue(
    session: AsyncSession,
    *,
    job_type: JobType,
    payload: dict[str, Any],
    tenant_id: uuid.UUID | None,
    dedupe_key: str | None = None,
    concurrency_key: str | None = None,
    max_attempts: int = 3,
    delay_seconds: float = 0,
) -> Job:
    """Add a job to the caller's transaction. Returns the existing active job for the same dedupe key."""
    if dedupe_key:
        existing = await _active_by_dedupe(session, dedupe_key)
        if existing is not None:
            return existing
    job = Job(
        tenant_id=tenant_id,
        job_type=job_type,
        payload=payload,
        status=JobStatus.QUEUED,
        attempts=0,
        max_attempts=max_attempts,
        run_after=utcnow() + timedelta(seconds=delay_seconds),
        dedupe_key=dedupe_key,
        concurrency_key=concurrency_key,
        correlation_id=get_correlation_id(),
    )
    try:
        async with session.begin_nested():
            session.add(job)
    except IntegrityError:
        existing = await _active_by_dedupe(session, dedupe_key) if dedupe_key else None
        if existing is None:
            raise
        return existing
    return job


async def _active_by_dedupe(session: AsyncSession, dedupe_key: str) -> Job | None:
    stmt = select(Job).where(Job.dedupe_key == dedupe_key, Job.status.in_(_ACTIVE)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def claim_next(session: AsyncSession, worker_id: str) -> Job | None:
    running = aliased(Job)
    blocked = exists().where(and_(running.status == JobStatus.RUNNING, running.concurrency_key == Job.concurrency_key))
    stmt = (
        select(Job)
        .where(
            Job.status == JobStatus.QUEUED,
            Job.job_type.in_(_KNOWN_TYPES),
            Job.run_after <= utcnow(),
            or_(Job.concurrency_key.is_(None), ~blocked),
        )
        .order_by(Job.run_after, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True, of=Job)
    )
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        await session.commit()  # end the read transaction without expiring the caller's objects
        return None
    job.status = JobStatus.RUNNING
    job.locked_by = worker_id
    job.locked_at = utcnow()
    job.attempts += 1
    try:
        await session.commit()
    except IntegrityError:
        # Another worker started a job with the same concurrency key first.
        await session.rollback()
        return None
    return job


def _held_by(job: Job) -> ColumnElement[bool]:
    """The row is still RUNNING under the lease this worker claimed (same holder, same attempt)."""
    return and_(
        Job.id == job.id,
        Job.status == JobStatus.RUNNING,
        Job.locked_by == job.locked_by,
        Job.attempts == job.attempts,
    )


async def _release(session: AsyncSession, job: Job, operation: str, **values: Any) -> bool:
    result = await session.execute(
        update(Job).where(_held_by(job)).values(**values).execution_options(synchronize_session=False)
    )
    await session.commit()
    if getattr(result, "rowcount", 0) == 1:
        return True
    log.warning(
        "job_lease_lost",
        job_id=str(job.id),
        operation=operation,
        worker_id=job.locked_by,
        attempt=job.attempts,
    )
    return False


async def complete(session: AsyncSession, job: Job, result: dict[str, Any] | None = None) -> bool:
    """Mark the job succeeded. Returns False (and changes nothing) if this worker no longer holds its lease."""
    return await _release(
        session,
        job,
        "complete",
        status=JobStatus.SUCCEEDED,
        finished_at=utcnow(),
        result=result or {},
        locked_by=None,
    )


async def fail(session: AsyncSession, job: Job, error: str, *, retryable: bool) -> JobStatus | None:
    """Re-queue (with backoff) or finish a failed job. Returns None if this worker no longer holds its lease."""
    if retryable and job.attempts < job.max_attempts:
        delay = min(300, 5 * 2 ** (job.attempts - 1))
        status, run_after, finished = JobStatus.QUEUED, utcnow() + timedelta(seconds=delay), None
    else:
        status, run_after, finished = (JobStatus.DEAD if retryable else JobStatus.FAILED), job.run_after, utcnow()
    released = await _release(
        session,
        job,
        "fail",
        status=status,
        run_after=run_after,
        finished_at=finished,
        last_error=error[:2000],
        locked_by=None,
        locked_at=None,
    )
    return status if released else None


async def heartbeat(session: AsyncSession, job: Job) -> bool:
    """Extend this worker's lease. Returns False if the lease was lost (recovered and possibly re-claimed)."""
    return await _release(session, job, "heartbeat", locked_at=utcnow())


async def recover_expired_leases(session: AsyncSession, lease_seconds: int) -> int:
    """Re-queue jobs whose worker died. Handlers must be safe to re-run (they are idempotent)."""
    cutoff = utcnow() - timedelta(seconds=lease_seconds)
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.RUNNING, Job.job_type.in_(_KNOWN_TYPES), Job.locked_at < cutoff)
        .with_for_update(skip_locked=True)
    )
    jobs = (await session.execute(stmt)).scalars().all()
    for job in jobs:
        job.locked_by = None
        job.locked_at = None
        if job.attempts < job.max_attempts:
            job.status = JobStatus.QUEUED
            job.last_error = "lease expired; re-queued"
        else:
            job.status = JobStatus.DEAD
            job.finished_at = utcnow()
            job.last_error = "lease expired; attempts exhausted"
    await session.commit()
    return len(jobs)
