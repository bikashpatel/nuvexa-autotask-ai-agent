"""Worker runtime: job handlers, failure classification, run loop, heartbeat, scheduler, worker_context."""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentOutputError, AgentProviderError
from app.agents.heuristic import HeuristicAnalysisAgent
from app.core.config import Settings
from app.core.context import get_correlation_id
from app.core.errors import AppError, IntegrationError, NotFound
from app.core.timeutil import utcnow
from app.db.models import ConnectedAccount, EmailMessage, Job, Tenant, User
from app.db.session import Database
from app.domain.enums import ConnectionProvider, ConnectionStatus, JobStatus, JobType, UserRole
from app.workers import queue, runner
from app.workers.runner import JobHandlers, Worker, _is_retryable, worker_context
from app.workflows.engine import WorkflowEngine
from tests.conftest import UserFactory, make_settings
from tests.unit.workers.conftest import JobFactory, reload_job
from tests.unit.workflows.fakes import FakeAgentWithClose, add_account, add_email, add_run


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def start(self, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.calls.append(("start", run_id, tenant_id))

    async def resume(self, run_id: uuid.UUID, tenant_id: uuid.UUID, decision: dict[str, Any]) -> None:
        self.calls.append(("resume", run_id, tenant_id, decision))


class FakeHandlers:
    """Replaces JobHandlers in Worker tests: scripted results/errors, records what ran."""

    def __init__(self) -> None:
        self.seen: list[tuple[uuid.UUID, str | None]] = []
        self.errors: dict[uuid.UUID, Exception] = {}
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def handle(self, job: Job) -> dict[str, Any]:
        self.seen.append((job.id, get_correlation_id()))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if job.id in self.errors:
                raise self.errors[job.id]
            return {"ok": str(job.id)}
        finally:
            self.active -= 1


@pytest.fixture
def fast_settings(database_url: str) -> Settings:
    return make_settings(database_url, worker_poll_interval_seconds=0.01, worker_concurrency=2)


def _worker(db: Database, settings: Settings, handlers: Any) -> Worker:
    return Worker(db, settings, handlers)


async def _wait_until(predicate: Any, limit: float = 5.0) -> None:
    """Poll a condition owned by the code under test (it exposes no event to wait on)."""
    async with asyncio.timeout(limit):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.005)


# --------------------------------------------------------------------------- retry classification


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AgentProviderError("down"), True),
        (AgentOutputError("refused"), False),
        (IntegrationError("x", category="validation", retryable=True), True),
        (IntegrationError("x", category="transient"), True),
        (IntegrationError("x", category="rate_limited"), True),
        (IntegrationError("x", category="timeout"), True),
        (IntegrationError("x", category="authorization"), False),
        (IntegrationError("x"), False),
        (NotFound(), False),
        (AppError(), False),
        (ValueError("bug"), True),
        (RuntimeError(), True),
    ],
)
def test_is_retryable(exc: Exception, expected: bool) -> None:
    assert _is_retryable(exc) is expected


# --------------------------------------------------------------------------- handlers


@pytest.fixture
async def run_ctx(db: Database, session: AsyncSession, tenant: Tenant, make_user: UserFactory) -> SimpleNamespace:
    owner = await make_user(tenant, UserRole.MEMBER)
    account = await add_account(session, owner)
    run = await add_run(db, await add_email(db, account))
    return SimpleNamespace(owner=owner, account=account, run=run, tenant=tenant)


def _handlers(db: Database, settings: Settings) -> tuple[JobHandlers, FakeEngine]:
    engine = FakeEngine()
    return JobHandlers(db, settings, engine), engine  # type: ignore[arg-type]


def _job(job_type: JobType, payload: dict[str, Any], tenant_id: uuid.UUID | None = None) -> Job:
    return Job(id=uuid.uuid4(), job_type=job_type, payload=payload, tenant_id=tenant_id, attempts=1)


async def test_analyze_handler_starts_the_engine(db: Database, settings: Settings, run_ctx: SimpleNamespace) -> None:
    handlers, engine = _handlers(db, settings)
    job = _job(JobType.ANALYZE_EMAIL, {"workflow_run_id": str(run_ctx.run.id)}, run_ctx.tenant.id)
    assert await handlers.handle(job) == {"workflow_run_id": str(run_ctx.run.id)}
    assert engine.calls == [("start", run_ctx.run.id, run_ctx.tenant.id)]


async def test_resume_handler_passes_command(db: Database, settings: Settings, run_ctx: SimpleNamespace) -> None:
    handlers, engine = _handlers(db, settings)
    command = {"type": "execute", "action_id": "a"}
    job = _job(JobType.RESUME_WORKFLOW, {"workflow_run_id": str(run_ctx.run.id), "command": command}, None)
    assert await handlers.handle(job) == {"workflow_run_id": str(run_ctx.run.id), "command": "execute"}
    assert engine.calls == [("resume", run_ctx.run.id, run_ctx.tenant.id, command)]


@pytest.mark.parametrize("payload_extra", [{}, {"command": None}, {"command": {}}])
async def test_resume_handler_defaults_to_refresh(
    db: Database, settings: Settings, run_ctx: SimpleNamespace, payload_extra: dict[str, Any]
) -> None:
    handlers, engine = _handlers(db, settings)
    job = _job(JobType.RESUME_WORKFLOW, {"workflow_run_id": str(run_ctx.run.id), **payload_extra})
    assert (await handlers.handle(job))["command"] == "refresh"
    assert engine.calls[0][3] == {"type": "refresh"}


async def test_run_lookup_guards_tenant_and_existence(
    db: Database, settings: Settings, run_ctx: SimpleNamespace
) -> None:
    handlers, engine = _handlers(db, settings)
    wrong_tenant = _job(JobType.ANALYZE_EMAIL, {"workflow_run_id": str(run_ctx.run.id)}, uuid.uuid4())
    with pytest.raises(AppError, match="Workflow run for job not found"):
        await handlers.handle(wrong_tenant)
    missing = _job(JobType.RESUME_WORKFLOW, {"workflow_run_id": str(uuid.uuid4())}, run_ctx.tenant.id)
    with pytest.raises(AppError, match="Workflow run for job not found"):
        await handlers.handle(missing)
    assert engine.calls == []


async def test_sync_handler_reports_counts(db: Database, settings: Settings, run_ctx: SimpleNamespace) -> None:
    handlers, _ = _handlers(db, settings)
    seen: list[uuid.UUID] = []

    async def fake_sync(session: AsyncSession, account_id: uuid.UUID) -> SimpleNamespace:
        seen.append(account_id)
        return SimpleNamespace(inserted=4, updated=2, removed=1)

    handlers.sync.sync_account = fake_sync  # type: ignore[method-assign]
    job = _job(JobType.SYNC_MAILBOX, {"connected_account_id": str(run_ctx.account.id)})
    assert await handlers.handle(job) == {"inserted": 4, "updated": 2, "removed": 1}
    assert seen == [run_ctx.account.id]


async def test_sync_handler_with_the_demo_mailbox(db: Database, settings: Settings, run_ctx: SimpleNamespace) -> None:
    handlers, _ = _handlers(db, settings)
    result = await handlers.handle(_job(JobType.SYNC_MAILBOX, {"connected_account_id": str(run_ctx.account.id)}))
    assert result["inserted"] > 0
    assert set(result) == {"inserted", "updated", "removed"}


async def test_retention_handler_sums_over_tenants(
    db: Database, settings: Settings, run_ctx: SimpleNamespace, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.add(Tenant(name="Second", slug=f"second-{uuid.uuid4().hex[:6]}"))
    await session.commit()
    purged: list[uuid.UUID] = []

    async def fake_purge(s: AsyncSession, tenant_id: uuid.UUID, cfg: Settings) -> dict[str, int]:
        purged.append(tenant_id)
        return {"email_bodies_purged": 2, "audit_events_deleted": 1}

    monkeypatch.setattr(runner, "purge_tenant", fake_purge)
    handlers, _ = _handlers(db, settings)
    assert await handlers.handle(_job(JobType.RETENTION_PURGE, {})) == {
        "email_bodies_purged": 4,
        "audit_events_deleted": 2,
    }
    assert len(purged) == 2
    assert run_ctx.tenant.id in purged


async def test_retention_handler_purges_old_bodies(db: Database, settings: Settings, run_ctx: SimpleNamespace) -> None:
    async with db.session() as s:
        email = await s.get(EmailMessage, run_ctx.run.email_message_id)
        assert email is not None
        email.received_at = utcnow() - timedelta(days=400)
        await s.commit()
    handlers, _ = _handlers(db, settings)
    totals = await handlers.handle(_job(JobType.RETENTION_PURGE, {}))
    assert totals["email_bodies_purged"] == 1
    async with db.session() as s:
        purged = await s.get(EmailMessage, run_ctx.run.email_message_id)
        assert purged is not None
        assert purged.body_text is None
        assert purged.body_purged_at is not None


# --------------------------------------------------------------------------- run_job / run_once


async def test_worker_id_format(db: Database, settings: Settings) -> None:
    worker = _worker(db, settings, FakeHandlers())
    assert re.fullmatch(r".+:\d+:[0-9a-f]{6}", worker.worker_id)
    assert worker.worker_id != _worker(db, settings, FakeHandlers()).worker_id


async def _claim(db: Database, worker: Worker) -> Job:
    async with db.session() as s:
        job = await queue.claim_next(s, worker.worker_id)
    assert job is not None
    return job


async def test_run_job_success_sets_correlation_id_and_completes(
    db: Database, settings: Settings, add_job: JobFactory
) -> None:
    handlers = FakeHandlers()
    worker = _worker(db, settings, handlers)
    job = await add_job(correlation_id="job-corr-1234")
    await worker.run_job(await _claim(db, worker))
    assert handlers.seen == [(job.id, "job-corr-1234")]
    stored = await reload_job(db, job)
    assert (stored.status, stored.result) == (JobStatus.SUCCEEDED, {"ok": str(job.id)})
    assert get_correlation_id() is None


async def test_run_job_generates_correlation_id_when_missing(
    db: Database, settings: Settings, add_job: JobFactory
) -> None:
    handlers = FakeHandlers()
    worker = _worker(db, settings, handlers)
    await add_job()
    await worker.run_job(await _claim(db, worker))
    cid = handlers.seen[0][1]
    assert cid is not None
    assert re.fullmatch(r"[0-9a-f]{32}", cid)


@pytest.mark.parametrize(
    ("error", "attempts", "status", "last_error"),
    [
        (AppError("not allowed"), 0, JobStatus.FAILED, "not allowed"),
        (ValueError("secret detail"), 0, JobStatus.QUEUED, "ValueError"),
        (AgentProviderError("provider down"), 0, JobStatus.QUEUED, "provider down"),
        (AgentProviderError("provider down"), 2, JobStatus.DEAD, "provider down"),
        (IntegrationError("ado 503", category="transient"), 0, JobStatus.QUEUED, "ado 503"),
        (IntegrationError("ado 400", category="validation"), 0, JobStatus.FAILED, "ado 400"),
    ],
)
async def test_run_job_failure_classification(
    db: Database,
    settings: Settings,
    add_job: JobFactory,
    error: Exception,
    attempts: int,
    status: JobStatus,
    last_error: str,
) -> None:
    handlers = FakeHandlers()
    worker = _worker(db, settings, handlers)
    job = await add_job(attempts=attempts, max_attempts=3)
    handlers.errors[job.id] = error
    await worker.run_job(await _claim(db, worker))
    stored = await reload_job(db, job)
    assert (stored.status, stored.last_error) == (status, last_error)
    assert get_correlation_id() is None


async def test_run_job_cancels_its_heartbeat(
    db: Database, settings: Settings, add_job: JobFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    states: list[str] = []

    async def heartbeat(self: Worker, job_id: uuid.UUID) -> None:
        states.append("started")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            states.append("cancelled")
            raise

    monkeypatch.setattr(Worker, "_heartbeat", heartbeat)
    handlers = FakeHandlers()
    worker = _worker(db, settings, handlers)
    job = await add_job()
    handlers.errors[job.id] = AppError("x")
    await worker.run_job(await _claim(db, worker))
    assert states == ["started", "cancelled"]


class _AsyncioWithFakeSleep:
    """Module proxy: `asyncio.sleep` yields immediately, everything else is the real asyncio."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)


async def test_heartbeat_refreshes_lease_and_survives_errors(
    db: Database, add_job: JobFactory, monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    fake = _AsyncioWithFakeSleep()
    monkeypatch.setattr(runner, "asyncio", fake)
    old = utcnow() - timedelta(minutes=30)
    job = await add_job(status=JobStatus.RUNNING, locked_at=old)
    calls = {"n": 0}
    real = queue.heartbeat

    async def flaky(session: AsyncSession, job_id: uuid.UUID) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("db blip")
        await real(session, job_id)

    monkeypatch.setattr(runner.queue, "heartbeat", flaky)
    worker = _worker(db, make_settings(database_url, job_lease_seconds=60), FakeHandlers())
    task = asyncio.create_task(worker._heartbeat(job.id))
    await _wait_until(lambda: calls["n"] >= 3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert fake.sleeps[0] == 20.0  # lease / 3
    assert (await reload_job(db, job)).locked_at > old  # type: ignore[operator]


async def test_heartbeat_interval_has_a_floor(db: Database, monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    fake = _AsyncioWithFakeSleep()
    monkeypatch.setattr(runner, "asyncio", fake)

    async def noop(session: AsyncSession, job_id: uuid.UUID) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(runner.queue, "heartbeat", noop)
    worker = _worker(db, make_settings(database_url, job_lease_seconds=30), FakeHandlers())
    with pytest.raises(asyncio.CancelledError):
        await worker._heartbeat(uuid.uuid4())
    assert fake.sleeps == [10.0]
    worker2 = _worker(db, make_settings(database_url, job_lease_seconds=30), FakeHandlers())
    worker2.settings = worker2.settings.model_copy(update={"job_lease_seconds": 6})
    with pytest.raises(asyncio.CancelledError):
        await worker2._heartbeat(uuid.uuid4())
    assert fake.sleeps[-1] == 5.0


async def test_run_once_processes_until_empty_and_respects_max(
    db: Database, settings: Settings, add_job: JobFactory
) -> None:
    handlers = FakeHandlers()
    worker = _worker(db, settings, handlers)
    for _ in range(3):
        await add_job()
    await add_job(run_after_offset=3600)  # not runnable yet
    assert await worker.run_once(max_jobs=2) == 2
    assert await worker.run_once() == 1
    assert await worker.run_once() == 0
    assert len(handlers.seen) == 3


# --------------------------------------------------------------------------- run_forever


async def test_run_forever_processes_jobs_until_stopped(
    db: Database, fast_settings: Settings, add_job: JobFactory
) -> None:
    handlers = FakeHandlers()
    worker = _worker(db, fast_settings, handlers)
    jobs = [await add_job() for _ in range(3)]
    task = asyncio.create_task(worker.run_forever())
    await _wait_until(lambda: len(handlers.seen) == 3)
    await _wait_until(lambda: handlers.active == 0)
    worker.stop()
    async with asyncio.timeout(5):
        await task
    for job in jobs:
        assert (await reload_job(db, job)).status is JobStatus.SUCCEEDED


async def test_run_forever_runs_jobs_concurrently_and_drains_on_stop(
    db: Database, fast_settings: Settings, add_job: JobFactory
) -> None:
    """Regression: stop() while every slot was busy still claimed one more job once a slot freed up."""
    handlers = FakeHandlers()
    handlers.gate = asyncio.Event()
    worker = _worker(db, fast_settings, handlers)
    jobs = [await add_job() for _ in range(3)]
    task = asyncio.create_task(worker.run_forever())
    await _wait_until(lambda: handlers.active == 2)  # concurrency limit is 2
    worker.stop()
    await asyncio.sleep(0.05)
    assert not task.done()  # waits for running jobs before exiting
    handlers.gate.set()
    async with asyncio.timeout(5):
        await task
    assert handlers.max_active == 2
    statuses = [(await reload_job(db, j)).status for j in jobs]
    assert statuses.count(JobStatus.SUCCEEDED) == 2
    assert statuses.count(JobStatus.QUEUED) == 1  # never claimed after stop


async def test_run_forever_survives_claim_errors(
    db: Database, fast_settings: Settings, add_job: JobFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = FakeHandlers()
    worker = _worker(db, fast_settings, handlers)
    await add_job()
    real = queue.claim_next
    calls = {"n": 0}

    async def flaky(session: AsyncSession, worker_id: str) -> Job | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("db down")
        return await real(session, worker_id)

    monkeypatch.setattr(runner.queue, "claim_next", flaky)
    task = asyncio.create_task(worker.run_forever())
    await _wait_until(lambda: len(handlers.seen) == 1)
    worker.stop()
    async with asyncio.timeout(5):
        await task
    assert calls["n"] >= 2


async def test_sleep_returns_early_when_stopped(db: Database, settings: Settings) -> None:
    worker = _worker(db, settings, FakeHandlers())
    loop = asyncio.get_running_loop()
    started = loop.time()
    await worker._sleep(0.01)  # plain timeout
    worker.stop()
    await worker._sleep(30)  # wakes immediately
    assert loop.time() - started < 1


# --------------------------------------------------------------------------- scheduler


async def _graph_account(session: AsyncSession, user: User, status: ConnectionStatus) -> ConnectedAccount:
    account = ConnectedAccount(
        tenant_id=user.tenant_id,
        user_id=user.id,
        provider=ConnectionProvider.MICROSOFT_GRAPH,
        external_account_id=f"graph-{uuid.uuid4().hex[:6]}",
        status=status,
    )
    session.add(account)
    await session.commit()
    return account


async def _jobs(db: Database, job_type: JobType) -> list[Job]:
    async with db.session() as s:
        return list((await s.execute(select(Job).where(Job.job_type == job_type))).scalars().all())


def _one_iteration(worker: Worker, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []

    async def stop_after(seconds: float) -> None:
        sleeps.append(seconds)
        worker.stop()

    monkeypatch.setattr(worker, "_sleep", stop_after)
    return sleeps


async def test_scheduler_recovers_leases_and_enqueues_polls(
    db: Database,
    database_url: str,
    session: AsyncSession,
    tenant: Tenant,
    make_user: UserFactory,
    add_job: JobFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await make_user(tenant, UserRole.MEMBER)
    active = await _graph_account(session, user, ConnectionStatus.ACTIVE)
    await _graph_account(session, user, ConnectionStatus.NEEDS_REAUTH)
    await add_account(session, user)  # demo mailbox: never polled
    stale = await add_job(
        job_type=JobType.ANALYZE_EMAIL, status=JobStatus.RUNNING, attempts=1, locked_at=utcnow() - timedelta(hours=1)
    )
    worker = _worker(db, make_settings(database_url, mail_poll_interval_seconds=60), FakeHandlers())
    sleeps = _one_iteration(worker, monkeypatch)

    await worker._scheduler()

    assert sleeps == [60]
    assert (await reload_job(db, stale)).status is JobStatus.QUEUED
    (sync,) = await _jobs(db, JobType.SYNC_MAILBOX)
    assert sync.payload == {"connected_account_id": str(active.id)}
    assert sync.tenant_id == tenant.id
    assert sync.dedupe_key == sync.concurrency_key == f"sync:{active.id}"
    assert await _jobs(db, JobType.RETENTION_PURGE) == []  # first retention run is ~1h after start


async def test_scheduler_without_polling(
    db: Database,
    database_url: str,
    session: AsyncSession,
    tenant: Tenant,
    make_user: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _graph_account(session, await make_user(tenant), ConnectionStatus.ACTIVE)
    worker = _worker(db, make_settings(database_url, mail_poll_interval_seconds=0), FakeHandlers())
    _one_iteration(worker, monkeypatch)
    await worker._scheduler()
    assert await _jobs(db, JobType.SYNC_MAILBOX) == []


async def test_scheduler_enqueues_retention_daily(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = utcnow()
    times = iter([t0, t0, t0 + timedelta(hours=1, seconds=1), t0 + timedelta(hours=2)])
    monkeypatch.setattr(runner, "utcnow", lambda: next(times))
    worker = _worker(db, settings, FakeHandlers())
    iterations = {"n": 0}

    async def sleep(seconds: float) -> None:
        iterations["n"] += 1
        if iterations["n"] == 2:
            worker.stop()

    monkeypatch.setattr(worker, "_sleep", sleep)
    await worker._scheduler()
    (purge,) = await _jobs(db, JobType.RETENTION_PURGE)  # once: the second iteration is within 24h
    assert purge.tenant_id is None
    assert purge.dedupe_key == purge.concurrency_key == "retention_purge"


async def test_scheduler_survives_iteration_errors(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(session: AsyncSession, lease: int) -> int:
        raise ConnectionError("db down")

    monkeypatch.setattr(runner.queue, "recover_expired_leases", broken)
    worker = _worker(db, settings, FakeHandlers())
    sleeps = _one_iteration(worker, monkeypatch)
    await worker._scheduler()
    assert sleeps == [60]


async def test_scheduler_is_cancelled_with_the_worker(db: Database, fast_settings: Settings) -> None:
    worker = _worker(db, fast_settings, FakeHandlers())
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.05)
    worker.stop()
    async with asyncio.timeout(5):
        await task  # the scheduler task (sleeping 60 s) is cancelled, not awaited to completion


# --------------------------------------------------------------------------- worker_context


@contextlib.asynccontextmanager
async def _memory_checkpointer(_settings: Settings) -> AsyncIterator[InMemorySaver]:
    yield InMemorySaver()


async def test_worker_context_wires_engine_and_closes_agent(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = FakeAgentWithClose()
    monkeypatch.setattr(runner, "open_checkpointer", _memory_checkpointer)
    monkeypatch.setattr(runner, "build_analysis_agent", lambda cfg: agent)
    async with worker_context(db, settings) as worker:
        assert isinstance(worker, Worker)
        assert isinstance(worker.handlers, JobHandlers)
        engine = worker.handlers.engine
        assert isinstance(engine, WorkflowEngine)
        assert engine.deps.agent is agent
        assert engine.deps.session_factory is db.session_factory
        assert engine.deps.execution.settings is settings
        assert agent.closed == 0
    assert agent.closed == 1


async def test_worker_context_closes_agent_on_error(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = FakeAgentWithClose()
    monkeypatch.setattr(runner, "open_checkpointer", _memory_checkpointer)
    monkeypatch.setattr(runner, "build_analysis_agent", lambda cfg: agent)
    with pytest.raises(RuntimeError):
        async with worker_context(db, settings):
            raise RuntimeError("boom")
    assert agent.closed == 1


async def test_worker_context_with_agent_without_aclose(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "open_checkpointer", _memory_checkpointer)
    async with worker_context(db, settings) as worker:
        assert isinstance(worker.handlers.engine.deps.agent, HeuristicAnalysisAgent)


async def test_worker_context_with_real_sqlite_checkpointer(tmp_path: Any) -> None:
    settings = make_settings(f"sqlite+aiosqlite:///{tmp_path / 'w.db'}")
    database = Database(settings)
    try:
        async with worker_context(database, settings) as worker:
            assert worker.settings is settings
        assert (tmp_path / "w-checkpoints.db").exists()
    finally:
        await database.dispose()
