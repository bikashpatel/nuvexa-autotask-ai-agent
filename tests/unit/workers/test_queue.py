"""Durable job queue: enqueue/dedupe, claiming, completion, failure backoff, heartbeats, lease recovery."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.context import set_correlation_id
from app.core.timeutil import utcnow
from app.db.models import Job, Tenant
from app.db.session import Database
from app.domain.enums import JobStatus, JobType
from app.workers import queue
from tests.unit.workers.conftest import JobFactory, reload_job

PURGE = JobType.RETENTION_PURGE


async def _enqueue(db: Database, **kwargs: object) -> Job:
    async with db.session() as s:
        job = await queue.enqueue(s, job_type=kwargs.pop("job_type", PURGE), payload={}, tenant_id=None, **kwargs)  # type: ignore[arg-type]
        await s.commit()
        return job


async def _count(db: Database) -> int:
    async with db.session() as s:
        return len((await s.execute(select(Job.id))).all())


# --------------------------------------------------------------------------- enqueue


async def test_enqueue_sets_defaults_and_correlation_id(db: Database) -> None:
    set_correlation_id("corr-enqueue-1")
    try:
        before = utcnow()
        async with db.session() as s:
            job = await queue.enqueue(
                s,
                job_type=JobType.ANALYZE_EMAIL,
                payload={"workflow_run_id": "r"},
                tenant_id=None,
                concurrency_key="run:r",
                max_attempts=5,
                delay_seconds=30,
            )
            await s.commit()
    finally:
        set_correlation_id(None)
    stored = await reload_job(db, job)
    assert stored.status is JobStatus.QUEUED
    assert stored.attempts == 0
    assert stored.max_attempts == 5
    assert stored.payload == {"workflow_run_id": "r"}
    assert stored.correlation_id == "corr-enqueue-1"
    assert stored.concurrency_key == "run:r"
    assert stored.dedupe_key is None
    assert before + timedelta(seconds=29) <= stored.run_after <= utcnow() + timedelta(seconds=31)


async def test_enqueue_is_rolled_back_with_the_callers_changes(db: Database) -> None:
    async with db.session() as s:
        s.add(Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}"))
        await s.flush()  # the caller's own change comes first (the normal pattern in services)
        await queue.enqueue(s, job_type=PURGE, payload={}, tenant_id=None, dedupe_key="tx")
        await s.rollback()
    assert await _count(db) == 0


async def test_enqueue_as_first_statement_is_transactional(db: Database, settings: Settings) -> None:
    if not settings.is_postgres:
        # Known SQLite-only caveat (pysqlite legacy transactions): a SAVEPOINT that opens the transaction
        # commits on release, so this job would survive the rollback. PostgreSQL is the system of record.
        pytest.skip("pysqlite commits a transaction-opening SAVEPOINT on release")
    async with db.session() as s:
        await queue.enqueue(s, job_type=PURGE, payload={}, tenant_id=None)
        await s.rollback()
    assert await _count(db) == 0


@pytest.mark.parametrize("active", [JobStatus.QUEUED, JobStatus.RUNNING])
async def test_dedupe_returns_the_active_job(db: Database, add_job: JobFactory, active: JobStatus) -> None:
    existing = await add_job(status=active, dedupe_key="k")
    again = await _enqueue(db, dedupe_key="k")
    assert again.id == existing.id
    assert await _count(db) == 1


@pytest.mark.parametrize("finished", [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.DEAD, JobStatus.CANCELLED])
async def test_dedupe_ignores_finished_jobs(db: Database, add_job: JobFactory, finished: JobStatus) -> None:
    old = await add_job(status=finished, dedupe_key="k")
    new = await _enqueue(db, dedupe_key="k")
    assert new.id != old.id
    assert await _count(db) == 2


async def test_without_dedupe_key_every_enqueue_creates_a_job(db: Database) -> None:
    a = await _enqueue(db)
    b = await _enqueue(db)
    assert a.id != b.id


async def test_dedupe_race_is_resolved_by_the_unique_index(
    db: Database, add_job: JobFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    winner = await add_job(dedupe_key="race")
    real = queue._active_by_dedupe
    calls: list[str] = []

    async def stale_first_lookup(session, key):  # type: ignore[no-untyped-def]
        calls.append(key)
        return None if len(calls) == 1 else await real(session, key)  # the other worker had not committed yet

    monkeypatch.setattr(queue, "_active_by_dedupe", stale_first_lookup)
    async with db.session() as s:
        job = await queue.enqueue(s, job_type=PURGE, payload={}, tenant_id=None, dedupe_key="race")
        assert job.id == winner.id
        await s.commit()  # the session is still usable after the savepoint rollback
    assert calls == ["race", "race"]
    assert await _count(db) == 1


@pytest.mark.parametrize("dedupe_key", [None, "fk-violation"])
async def test_other_integrity_errors_propagate(db: Database, dedupe_key: str | None) -> None:
    async with db.session() as s:
        with pytest.raises(IntegrityError):
            await queue.enqueue(s, job_type=PURGE, payload={}, tenant_id=uuid.uuid4(), dedupe_key=dedupe_key)


# --------------------------------------------------------------------------- claim


async def test_claim_marks_running_and_counts_attempt(db: Database, add_job: JobFactory) -> None:
    job = await add_job(attempts=1)
    async with db.session() as s:
        claimed = await queue.claim_next(s, "worker-1")
    assert claimed is not None
    assert claimed.id == job.id
    stored = await reload_job(db, job)
    assert stored.status is JobStatus.RUNNING
    assert stored.locked_by == "worker-1"
    assert stored.locked_at is not None
    assert stored.attempts == 2


async def test_claim_orders_by_run_after(db: Database, add_job: JobFactory) -> None:
    later = await add_job(run_after_offset=-10)
    earliest = await add_job(run_after_offset=-20)
    now = await add_job(run_after_offset=0)
    order = []
    for _ in range(3):
        async with db.session() as s:
            job = await queue.claim_next(s, "w")
        assert job is not None
        order.append(job.id)
    assert order == [earliest.id, later.id, now.id]
    async with db.session() as s:
        assert await queue.claim_next(s, "w") is None


async def test_future_jobs_and_non_queued_jobs_are_not_claimed(db: Database, add_job: JobFactory) -> None:
    await add_job(run_after_offset=3600)
    for status in (JobStatus.RUNNING, JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.DEAD):
        await add_job(status=status)
    async with db.session() as s:
        assert await queue.claim_next(s, "w") is None


async def test_concurrency_key_blocks_while_a_job_runs(db: Database, add_job: JobFactory) -> None:
    first = await add_job(concurrency_key="run:1", run_after_offset=-2)
    second = await add_job(concurrency_key="run:1", run_after_offset=-1)
    unrelated = await add_job(concurrency_key=None)
    async with db.session() as s:
        assert (await queue.claim_next(s, "w")).id == first.id  # type: ignore[union-attr]
    async with db.session() as s:
        assert (await queue.claim_next(s, "w")).id == unrelated.id  # type: ignore[union-attr]
    async with db.session() as s:
        assert await queue.claim_next(s, "w") is None  # second is blocked by first
    async with db.session() as s:
        await queue.complete(s, first.id)
    async with db.session() as s:
        assert (await queue.claim_next(s, "w")).id == second.id  # type: ignore[union-attr]


async def test_claim_race_on_concurrency_key_returns_none(db: Database, add_job: JobFactory) -> None:
    job = await add_job(concurrency_key="run:race")
    async with db.session() as s:
        real_commit = s.commit
        raced = False

        async def racing_commit() -> None:
            nonlocal raced
            if not raced:
                raced = True
                async with db.session() as other:  # another worker starts a job with the same key first
                    other.add(
                        Job(
                            job_type=PURGE,
                            payload={},
                            status=JobStatus.RUNNING,
                            run_after=utcnow(),
                            concurrency_key="run:race",
                        )
                    )
                    await other.commit()
            await real_commit()

        s.commit = racing_commit  # type: ignore[method-assign]
        assert await queue.claim_next(s, "w") is None
    assert raced
    stored = await reload_job(db, job)
    assert stored.status is JobStatus.QUEUED
    assert stored.attempts == 0


# --------------------------------------------------------------------------- complete / fail


async def test_complete(db: Database, add_job: JobFactory) -> None:
    job = await add_job(status=JobStatus.RUNNING, locked_by="w")
    async with db.session() as s:
        await queue.complete(s, job.id, {"inserted": 3})
    stored = await reload_job(db, job)
    assert (stored.status, stored.result, stored.locked_by) == (JobStatus.SUCCEEDED, {"inserted": 3}, None)
    assert stored.finished_at is not None
    other = await add_job(status=JobStatus.RUNNING)
    async with db.session() as s:
        await queue.complete(s, other.id)
    assert (await reload_job(db, other)).result == {}


@pytest.mark.parametrize(
    ("attempts", "delay"), [(1, 5), (2, 10), (3, 20), (4, 40), (5, 80), (6, 160), (7, 300), (8, 300), (12, 300)]
)
async def test_retryable_failure_is_requeued_with_backoff(
    db: Database, add_job: JobFactory, attempts: int, delay: int
) -> None:
    job = await add_job(status=JobStatus.RUNNING, attempts=attempts, max_attempts=20, locked_by="w", locked_at=utcnow())
    before = utcnow()
    async with db.session() as s:
        status = await queue.fail(s, job, "transient", retryable=True)
    assert status is JobStatus.QUEUED
    stored = await reload_job(db, job)
    assert stored.status is JobStatus.QUEUED
    assert (stored.locked_by, stored.locked_at, stored.finished_at) == (None, None, None)
    assert stored.last_error == "transient"
    assert before + timedelta(seconds=delay - 1) <= stored.run_after <= utcnow() + timedelta(seconds=delay + 1)


async def test_retryable_failure_after_last_attempt_is_dead(db: Database, add_job: JobFactory) -> None:
    job = await add_job(status=JobStatus.RUNNING, attempts=3, max_attempts=3)
    original_run_after = job.run_after
    async with db.session() as s:
        assert await queue.fail(s, job, "x" * 3000, retryable=True) is JobStatus.DEAD
    stored = await reload_job(db, job)
    assert stored.status is JobStatus.DEAD
    assert stored.finished_at is not None
    assert stored.run_after == original_run_after
    assert stored.last_error == "x" * 2000


async def test_non_retryable_failure_is_failed_immediately(db: Database, add_job: JobFactory) -> None:
    job = await add_job(status=JobStatus.RUNNING, attempts=1, max_attempts=5)
    async with db.session() as s:
        assert await queue.fail(s, job, "bad input", retryable=False) is JobStatus.FAILED
    stored = await reload_job(db, job)
    assert (stored.status, stored.last_error) == (JobStatus.FAILED, "bad input")
    assert stored.finished_at is not None


# --------------------------------------------------------------------------- heartbeat / leases


async def test_heartbeat_refreshes_running_jobs_only(db: Database, add_job: JobFactory) -> None:
    old = utcnow() - timedelta(minutes=10)
    running = await add_job(status=JobStatus.RUNNING, locked_at=old)
    done = await add_job(status=JobStatus.SUCCEEDED, locked_at=old)
    async with db.session() as s:
        await queue.heartbeat(s, running.id)
        await queue.heartbeat(s, done.id)
    assert (await reload_job(db, running)).locked_at > old  # type: ignore[operator]
    assert (await reload_job(db, done)).locked_at == old


async def test_recover_expired_leases(db: Database, add_job: JobFactory) -> None:
    expired = utcnow() - timedelta(seconds=400)
    requeue = await add_job(status=JobStatus.RUNNING, attempts=1, max_attempts=3, locked_by="dead", locked_at=expired)
    exhausted = await add_job(status=JobStatus.RUNNING, attempts=3, max_attempts=3, locked_by="dead", locked_at=expired)
    fresh = await add_job(status=JobStatus.RUNNING, attempts=1, locked_by="alive", locked_at=utcnow())
    queued = await add_job(status=JobStatus.QUEUED, locked_at=expired)

    async with db.session() as s:
        assert await queue.recover_expired_leases(s, 300) == 2

    r = await reload_job(db, requeue)
    assert (r.status, r.locked_by, r.locked_at, r.last_error) == (
        JobStatus.QUEUED,
        None,
        None,
        "lease expired; re-queued",
    )
    assert r.finished_at is None
    e = await reload_job(db, exhausted)
    assert (e.status, e.last_error) == (JobStatus.DEAD, "lease expired; attempts exhausted")
    assert e.finished_at is not None
    assert (await reload_job(db, fresh)).status is JobStatus.RUNNING
    assert (await reload_job(db, queued)).status is JobStatus.QUEUED
    async with db.session() as s:
        assert await queue.recover_expired_leases(s, 300) == 0
