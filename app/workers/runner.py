"""Worker process: claims jobs, runs handlers, schedules periodic work."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.agents.base import AgentProviderError
from app.agents.factory import build_analysis_agent
from app.core.config import Settings
from app.core.context import new_correlation_id, set_correlation_id
from app.core.errors import AppError, IntegrationError
from app.core.timeutil import utcnow
from app.db.models import ConnectedAccount, Job, User, WorkflowRun
from app.db.session import Database
from app.domain.enums import ConnectionProvider, ConnectionStatus, JobType
from app.observability.logging import get_logger
from app.services.audit import configure_audit_signing
from app.services.email_sync_service import EmailSyncService
from app.services.execution_service import ExecutionService
from app.services.integration_clients import make_ado_client_factory
from app.services.retention_service import purge_tenant, tenant_ids
from app.workers import queue
from app.workflows.checkpointer import open_checkpointer
from app.workflows.engine import WorkflowEngine
from app.workflows.graph import WorkflowDeps
from app.workflows.state import ReviewDecision

log = get_logger(__name__)


class JobHandlers:
    def __init__(self, db: Database, settings: Settings, engine: WorkflowEngine) -> None:
        self.db = db
        self.settings = settings
        self.engine = engine
        self.sync = EmailSyncService(settings)

    async def handle(self, job: Job) -> dict[str, Any]:
        match job.job_type:
            case JobType.SYNC_MAILBOX:
                async with self.db.session() as s:
                    result = await self.sync.sync_account(s, uuid.UUID(job.payload["connected_account_id"]))
                return {"inserted": result.inserted, "updated": result.updated, "removed": result.removed}
            case JobType.ANALYZE_EMAIL:
                run = await self._run(job)
                await self.engine.start(run.id, run.tenant_id)
                return {"workflow_run_id": str(run.id)}
            case JobType.RESUME_WORKFLOW:
                run = await self._run(job)
                decision: ReviewDecision = job.payload.get("command") or {"type": "refresh"}  # type: ignore[assignment]
                await self.engine.resume(run.id, run.tenant_id, decision)
                return {"workflow_run_id": str(run.id), "command": decision.get("type")}
            case JobType.RETENTION_PURGE:
                totals: dict[str, int] = {}
                async with self.db.session() as s:
                    for tid in await tenant_ids(s):
                        for k, v in (await purge_tenant(s, tid, self.settings)).items():
                            totals[k] = totals.get(k, 0) + v
                return totals

    async def _run(self, job: Job) -> WorkflowRun:
        async with self.db.session() as s:
            run = await s.get(WorkflowRun, uuid.UUID(job.payload["workflow_run_id"]))
        if run is None or (job.tenant_id is not None and run.tenant_id != job.tenant_id):
            raise AppError("Workflow run for job not found.")
        return run


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, AgentProviderError):
        return True
    if isinstance(exc, IntegrationError):
        return exc.retryable or exc.category in ("transient", "rate_limited", "timeout")
    return not isinstance(exc, AppError)  # unexpected errors: retry within the attempt budget


class Worker:
    def __init__(self, db: Database, settings: Settings, handlers: JobHandlers) -> None:
        self.db = db
        self.settings = settings
        self.handlers = handlers
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_job(self, job: Job) -> None:
        set_correlation_id(job.correlation_id or new_correlation_id())
        log.info("job_started", job_id=str(job.id), job_type=job.job_type.value, attempt=job.attempts)
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            result = await self.handlers.handle(job)
        except Exception as exc:
            retryable = _is_retryable(exc)
            message = exc.message if isinstance(exc, AppError) else f"{type(exc).__name__}"
            async with self.db.session() as s:
                status = await queue.fail(s, job, message, retryable=retryable)
            # status None: the lease was recovered and the job belongs to another attempt now; leave it alone.
            log.warning(
                "job_failed",
                job_id=str(job.id),
                job_type=job.job_type.value,
                status=status.value if status is not None else "lease_lost",
                retryable=retryable,
                error=message,
            )
            if not isinstance(exc, AppError):
                log.exception("job_unexpected_error", job_id=str(job.id))
        else:
            async with self.db.session() as s:
                completed = await queue.complete(s, job, result)
            if completed:
                log.info("job_succeeded", job_id=str(job.id), job_type=job.job_type.value)
            else:  # finished after its lease was recovered: the current holder's outcome stands
                log.warning("job_result_discarded", job_id=str(job.id), job_type=job.job_type.value)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            set_correlation_id(None)

    async def _heartbeat(self, job: Job) -> None:
        """Keep the lease fresh while the job runs; only jobs of dead workers are recovered."""
        interval = max(5.0, self.settings.job_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.db.session() as s:
                    held = await queue.heartbeat(s, job)
            except Exception:
                log.warning("job_heartbeat_failed", job_id=str(job.id))
                continue
            if not held:  # the lease was recovered (this worker stalled); never extend another holder's lease
                return

    async def run_once(self, max_jobs: int = 100) -> int:
        """Process queued jobs sequentially until none are runnable (used by tests and the demo)."""
        processed = 0
        while processed < max_jobs:
            async with self.db.session() as s:
                job = await queue.claim_next(s, self.worker_id)
            if job is None:
                break
            await self.run_job(job)
            processed += 1
        return processed

    async def run_forever(self) -> None:
        log.info("worker_started", worker_id=self.worker_id, concurrency=self.settings.worker_concurrency)
        semaphore = asyncio.Semaphore(self.settings.worker_concurrency)
        tasks: set[asyncio.Task[None]] = set()
        scheduler = asyncio.create_task(self._scheduler())

        async def _guarded(job: Job) -> None:
            try:
                await self.run_job(job)
            finally:
                semaphore.release()

        try:
            while not self._stop.is_set():
                await semaphore.acquire()
                if self._stop.is_set():  # stop requested while waiting for a free slot: claim nothing new
                    semaphore.release()
                    break
                try:
                    async with self.db.session() as s:
                        job = await queue.claim_next(s, self.worker_id)
                except Exception:
                    semaphore.release()
                    log.exception("job_claim_failed")
                    await self._sleep(self.settings.worker_poll_interval_seconds * 5)
                    continue
                if job is None:
                    semaphore.release()
                    await self._sleep(self.settings.worker_poll_interval_seconds)
                    continue
                task = asyncio.create_task(_guarded(job))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler
            if tasks:
                await asyncio.wait(tasks, timeout=30)
            log.info("worker_stopped", worker_id=self.worker_id)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _scheduler(self) -> None:
        last_poll = utcnow() - timedelta(days=1)
        last_retention = utcnow() - timedelta(hours=23)
        while not self._stop.is_set():
            try:
                async with self.db.session() as s:
                    recovered = await queue.recover_expired_leases(s, self.settings.job_lease_seconds)
                    if recovered:
                        log.warning("jobs_recovered_after_lease_expiry", count=recovered)
                now = utcnow()
                interval = self.settings.mail_poll_interval_seconds
                if interval > 0 and (now - last_poll).total_seconds() >= interval:
                    await self._enqueue_polls()
                    last_poll = now
                if (now - last_retention) >= timedelta(hours=24):
                    async with self.db.session() as s:
                        await queue.enqueue(
                            s,
                            job_type=JobType.RETENTION_PURGE,
                            payload={},
                            tenant_id=None,
                            dedupe_key="retention_purge",
                            concurrency_key="retention_purge",
                        )
                        await s.commit()
                    last_retention = now
            except Exception:
                log.exception("scheduler_iteration_failed")
            await self._sleep(60)

    async def _enqueue_polls(self) -> None:
        async with self.db.session() as s:
            accounts = (
                (
                    await s.execute(
                        select(ConnectedAccount)
                        # A deactivated user's mailbox is not polled: their stored tokens must stay unused.
                        .join(User, User.id == ConnectedAccount.user_id)
                        .where(
                            ConnectedAccount.status == ConnectionStatus.ACTIVE,
                            ConnectedAccount.provider == ConnectionProvider.MICROSOFT_GRAPH,
                            User.is_active.is_(True),
                            User.tenant_id == ConnectedAccount.tenant_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for account in accounts:
                await queue.enqueue(
                    s,
                    job_type=JobType.SYNC_MAILBOX,
                    payload={"connected_account_id": str(account.id)},
                    tenant_id=account.tenant_id,
                    dedupe_key=f"sync:{account.id}",
                    concurrency_key=f"sync:{account.id}",
                )
            await s.commit()


@asynccontextmanager
async def worker_context(db: Database, settings: Settings) -> AsyncIterator[Worker]:
    """Builds the agent, execution service, checkpointer and workflow engine for a worker."""
    configure_audit_signing(settings.audit_hmac_key)
    async with open_checkpointer(settings) as checkpointer:
        agent = build_analysis_agent(settings)
        deps = WorkflowDeps(
            settings=settings,
            session_factory=db.session_factory,
            agent=agent,
            execution=ExecutionService(settings, make_ado_client_factory(settings)),
        )
        engine = WorkflowEngine(deps, checkpointer)
        try:
            yield Worker(db, settings, JobHandlers(db, settings, engine))
        finally:
            aclose = getattr(agent, "aclose", None)
            if aclose is not None:
                await aclose()  # closes the HTTP client / Entra credential of LLM providers
