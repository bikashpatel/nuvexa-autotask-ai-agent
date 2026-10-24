"""Worker test fixtures: a tenant-scoped job builder on the shared test database."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest

from app.core.timeutil import utcnow
from app.db.models import Job
from app.db.session import Database
from app.domain.enums import JobStatus, JobType

JobFactory = Callable[..., Awaitable[Job]]


@pytest.fixture
def add_job(db: Database) -> JobFactory:
    """Insert a job row directly (bypassing enqueue) with arbitrary state."""

    async def _add(
        *,
        job_type: JobType = JobType.RETENTION_PURGE,
        status: JobStatus = JobStatus.QUEUED,
        run_after_offset: float = 0,
        payload: dict[str, Any] | None = None,
        **fields: Any,
    ) -> Job:
        async with db.session() as s:
            job = Job(
                job_type=job_type,
                payload=payload or {},
                status=status,
                run_after=utcnow() + timedelta(seconds=run_after_offset),
                **fields,
            )
            s.add(job)
            await s.commit()
            return job

    return _add


async def reload_job(db: Database, job: Job) -> Job:
    async with db.session() as s:
        fresh = await s.get(Job, job.id)
        assert fresh is not None
        return fresh
