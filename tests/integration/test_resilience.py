"""Resilience: worker restarts, crashes, retries/backoff, lease recovery, uncertain outcomes, recovery paths.

A "crash" is simulated with a `BaseException` subclass raised inside a node or adapter: it bypasses every
`except Exception` handler exactly like a killed process would, leaving the job leased and the LangGraph
checkpoint at the last completed step. A "restart" is a new WorkflowEngine + Worker built on a new
checkpointer instance over the same durable store (AsyncSqliteSaver file in tmp_path).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentOutputError, AgentProviderError
from app.core.timeutil import utcnow
from app.db.models import ActionExecution, Job, WorkflowRun
from app.domain.enums import JobStatus, JobType
from app.integrations.azure_devops.base import AzureDevOpsClient, CreateOutcome, WorkItemCreateRequest
from app.integrations.azure_devops.mock import MockAzureDevOpsClient
from app.integrations.microsoft_graph.demo_mailbox import load_demo_fixture
from app.services.authz import Actor
from app.workers import queue
from app.workers.runner import JobHandlers, Worker
from app.workflows.engine import WorkflowEngine
from app.workflows.graph import WorkflowDeps
from tests.integration.conftest import (
    Harness,
    HarnessFactory,
    Persona,
    approve_and_execute,
    email_id_by_provider_id,
    find_action,
    review_body,
)
from tests.integration.fakes import ScriptedAgent

FIXTURE = load_demo_fixture()["messages"]
INCIDENT_ID = FIXTURE[0]["id"]
FEATURE_ID = next(m["id"] for m in FIXTURE if m["subject"].startswith("Feature request"))


class SimulatedCrash(BaseException):
    """Stands in for SIGKILL/OOM: escapes every `except Exception`."""


@asynccontextmanager
async def sqlite_saver(path: Path) -> AsyncIterator[AsyncSqliteSaver]:
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()
        yield saver


def restart_worker(h: Harness, checkpointer: BaseCheckpointSaver[str], agent: Any = None) -> WorkflowEngine:
    """Replace the harness worker with a fresh engine/worker (same database and execution service)."""
    deps = WorkflowDeps(
        settings=h.settings,
        session_factory=h.db.session_factory,
        agent=agent or ScriptedAgent(),
        execution=h.execution,
    )
    engine = WorkflowEngine(deps, checkpointer)
    h.engine = engine
    h.worker = Worker(h.db, h.settings, JobHandlers(h.db, h.settings, engine))
    return engine


def _config(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}}


async def _queue_analysis(h: Harness, provider_id: str = INCIDENT_ID) -> tuple[Persona, str]:
    member = await h.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await h.drain()
    email_id = await email_id_by_provider_id(h, member.user_id, provider_id)
    resp = await member.post(f"/emails/{email_id}/analyze")
    assert resp.status_code == 202
    return member, resp.json()["workflow_run"]["id"]


async def _jobs(h: Harness, job_type: JobType) -> list[Job]:
    async with h.db.session() as s:
        stmt = select(Job).where(Job.job_type == job_type).order_by(Job.created_at)
        return list((await s.execute(stmt)).scalars().all())


async def _expire_leases(h: Harness) -> int:
    """Time passes beyond the lease of a dead worker; the scheduler's recovery step runs."""
    async with h.db.session() as s:
        await s.execute(
            update(Job)
            .where(Job.status == JobStatus.RUNNING)
            .values(locked_at=utcnow() - timedelta(seconds=h.settings.job_lease_seconds + 5))
        )
        await s.commit()
        return await queue.recover_expired_leases(s, h.settings.job_lease_seconds)


async def _fast_forward_backoff(h: Harness) -> None:
    async with h.db.session() as s:
        await s.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(run_after=utcnow()))
        await s.commit()


def _llm_steps(detail: dict[str, Any]) -> list[str]:
    return [e["step"] for e in detail["events"] if e["event_type"] == "llm_call"]


# ---------------------------------------------------------------------------------------------------
# Worker restarts and crashes
# ---------------------------------------------------------------------------------------------------


async def test_restarted_worker_resumes_an_interrupted_run_from_the_checkpointer(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    store = tmp_path / "checkpoints.db"
    async with sqlite_saver(store) as first:
        h = await make_harness(checkpointer=first)
        member, run_id = await _queue_analysis(h)
        await h.drain()
        detail = (await member.get(f"/workflows/{run_id}")).json()
        assert detail["run"]["status"] == "pending_approval"
        assert (await h.engine.graph.aget_state(_config(run_id))).interrupts
    # The first worker process is gone. A new one opens the same checkpoint store.
    async with sqlite_saver(store) as second:
        engine = restart_worker(h, second)
        snapshot = await engine.graph.aget_state(_config(run_id))
        assert snapshot.interrupts, "the interrupt survived the restart"
        assert snapshot.next == ("wait_for_review",)
        work_item = find_action(detail, "create_azure_devops_work_item")
        done = await approve_and_execute(h, member, work_item)
        assert done["status"] == "completed"
        reply = find_action(detail, "propose_email_reply")
        assert (await member.post(f"/actions/{reply['id']}/reject", json={"reason": "no"})).status_code == 200
        await h.drain()
        final = (await member.get(f"/workflows/{run_id}")).json()
        assert final["run"]["status"] == "completed"  # completed + rejected → completed
        messages = [e["message"] or "" for e in final["events"]]
        assert "Resumed: execute" in messages  # resumed inside the graph …
        assert not any("graph not waiting" in m for m in messages)  # … not via the fallback
        assert final["events"][-1]["step"] == "finalize"
        assert not (await engine.graph.aget_state(_config(run_id))).next  # the graph reached END


async def test_lost_checkpoints_fall_back_to_safe_direct_execution(harness: Harness) -> None:
    member, run_id = await _queue_analysis(harness, FEATURE_ID)
    await harness.drain()
    detail = (await member.get(f"/workflows/{run_id}")).json()
    [work_item] = detail["actions"]
    restart_worker(harness, InMemorySaver())  # e.g. a misconfigured, non-durable checkpointer
    done = await approve_and_execute(harness, member, work_item)
    assert done["status"] == "completed"
    assert len(harness.ado.clients[-1].items()) == 1
    final = (await member.get(f"/workflows/{run_id}")).json()
    assert final["run"]["status"] == "completed"
    assert any("graph not waiting; handled 'execute' directly" in (e["message"] or "") for e in final["events"])
    # Execution safety does not depend on the graph: a forged resume still cannot write.
    harness.ado.clients.clear()
    await harness.engine.resume(
        uuid.UUID(run_id),
        member.tenant_id,
        {"type": "execute", "action_id": work_item["id"], "actor_user_id": str(member.user_id)},
    )
    assert harness.ado.clients == [] or not harness.ado.clients[-1].create_attempts


async def test_crash_mid_analysis_resumes_from_the_last_completed_step(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    store = tmp_path / "checkpoints.db"
    crashing = ScriptedAgent({"extract": [SimulatedCrash()]})
    async with sqlite_saver(store) as first:
        h = await make_harness(checkpointer=first, agent=crashing)
        member, run_id = await _queue_analysis(h)
        with pytest.raises(SimulatedCrash):
            await h.drain()
    [job] = await _jobs(h, JobType.ANALYZE_EMAIL)
    assert (job.status, job.attempts) == (JobStatus.RUNNING, 1)  # still leased by the dead worker
    mid = (await member.get(f"/workflows/{run_id}")).json()
    assert mid["run"]["status"] == "classified"

    async with sqlite_saver(store) as second:
        healthy = ScriptedAgent()
        restart_worker(h, second, healthy)
        assert await h.drain() == 0  # the lease has not expired yet
        assert await _expire_leases(h) == 1
        assert await h.drain() == 1
    detail = (await member.get(f"/workflows/{run_id}")).json()
    assert detail["run"]["status"] == "pending_approval"
    assert healthy.calls == {"classify": 0, "extract": 1, "plan": 1}  # classify was not re-run
    assert crashing.calls == {"classify": 1, "extract": 1, "plan": 0}
    assert _llm_steps(detail) == ["classify", "extract", "plan"]
    [job] = await _jobs(h, JobType.ANALYZE_EMAIL)
    assert (job.status, job.attempts) == (JobStatus.SUCCEEDED, 2)
    assert len(detail["actions"]) == 2


class _CrashAfterCreate(MockAzureDevOpsClient):
    """The work item is created, then the worker dies before the outcome is recorded."""

    async def create_work_item_idempotent(self, request: WorkItemCreateRequest) -> CreateOutcome:
        await super().create_work_item_idempotent(request)
        raise SimulatedCrash


async def test_crash_after_an_external_write_is_reconciled_never_resent(harness: Harness) -> None:
    clients: list[MockAzureDevOpsClient] = []
    crash = [True]

    async def factory(_s: AsyncSession, _a: Actor) -> AzureDevOpsClient:
        client = _CrashAfterCreate() if crash and crash.pop() else MockAzureDevOpsClient()
        clients.append(client)
        return client

    harness.execution.ado_client_factory = factory
    member, run_id = await _queue_analysis(harness, FEATURE_ID)
    await harness.drain()
    [work_item] = (await member.get(f"/workflows/{run_id}")).json()["actions"]
    body = review_body(work_item)
    assert (await member.post(f"/actions/{work_item['id']}/approve", json=body)).status_code == 200
    await harness.drain()
    assert (await member.post(f"/actions/{work_item['id']}/execute", json=body)).status_code == 202
    with pytest.raises(SimulatedCrash):
        await harness.drain()
    stuck = (await member.get(f"/actions/{work_item['id']}")).json()
    assert stuck["status"] == "executing"
    assert [e["status"] for e in stuck["executions"]] == ["running"]
    assert len(clients[0].items()) == 1  # the write happened

    # Re-delivering the job right away is refused while the execution lease is live …
    async with harness.db.session() as s:
        await s.execute(
            update(ActionExecution)
            .where(ActionExecution.action_id == uuid.UUID(work_item["id"]))
            .values(started_at=utcnow() - timedelta(seconds=harness.settings.job_lease_seconds + 60))
        )
        await s.commit()
    # … and after the lease expired the new worker reconciles instead of creating again.
    assert await _expire_leases(harness) == 1
    await harness.drain()
    done = (await member.get(f"/actions/{work_item['id']}")).json()
    assert done["status"] == "completed", done
    assert [(e["status"], e["deduplicated"]) for e in done["executions"]] == [("uncertain", False), ("succeeded", True)]
    assert done["external_id"] == str(clients[0].items()[0].id)
    assert len(clients[-1].items()) == 1
    assert sum(c.create_attempts for c in clients) == 1
    [event] = await harness.audit_events(event_type="execution_reconciled")
    assert event.details["reason"] == "stale execution lease"


# ---------------------------------------------------------------------------------------------------
# Job retries, backoff, dead letters
# ---------------------------------------------------------------------------------------------------


async def test_provider_errors_are_retried_with_backoff(make_harness: HarnessFactory) -> None:
    agent = ScriptedAgent({"classify": [AgentProviderError("LLM provider returned HTTP 429"), None]})
    h = await make_harness(agent=agent)
    member, run_id = await _queue_analysis(h)
    assert await h.drain() == 1
    [job] = await _jobs(h, JobType.ANALYZE_EMAIL)
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 1)
    assert timedelta(seconds=3) < job.run_after - utcnow() <= timedelta(seconds=5)  # 5 s · 2^(attempt-1)
    assert "429" in (job.last_error or "")
    failed = (await member.get(f"/workflows/{run_id}")).json()
    assert failed["run"]["status"] == "failed"
    assert (failed["run"]["error_category"], failed["run"]["current_step"]) == ("llm_provider", "classify")
    [step_failed] = [e for e in failed["events"] if e["event_type"] == "step_failed"]
    assert step_failed["details"] == {"failure_category": "llm_provider", "retryable": True}
    email = (await member.get(f"/emails/{failed['run']['email_message_id']}")).json()
    assert email["analysis_status"] == "failed"
    assert await h.drain() == 0  # backoff not elapsed: nothing runnable

    await _fast_forward_backoff(h)
    assert await h.drain() == 1
    [job] = await _jobs(h, JobType.ANALYZE_EMAIL)
    assert (job.status, job.attempts) == (JobStatus.SUCCEEDED, 2)
    recovered = (await member.get(f"/workflows/{run_id}")).json()
    assert recovered["run"]["status"] == "pending_approval"
    assert agent.calls["classify"] == 2
    assert agent.calls["extract"] == 1  # only the failed step is re-run
    assert (await member.get(f"/emails/{email['id']}")).json()["analysis_status"] == "awaiting_review"


async def test_recovered_run_does_not_keep_reporting_the_old_error(make_harness: HarnessFactory) -> None:
    """Regression (fixed in graph.set_run_status): a recovered run kept reporting the old llm_provider error."""
    agent = ScriptedAgent({"classify": [AgentProviderError("LLM provider returned HTTP 429"), None]})
    h = await make_harness(agent=agent)
    member, run_id = await _queue_analysis(h)
    await h.drain()
    await _fast_forward_backoff(h)
    await h.drain()
    run = (await member.get(f"/workflows/{run_id}")).json()["run"]
    assert run["status"] == "pending_approval"
    assert (run["error_category"], run["error_message"]) == (None, None)


async def test_retries_are_bounded_then_the_job_is_dead_and_reanalysis_recovers(make_harness: HarnessFactory) -> None:
    agent = ScriptedAgent({"classify": [AgentProviderError("provider down") for _ in range(3)]})
    h = await make_harness(agent=agent)
    member, run_id = await _queue_analysis(h)
    delays = []
    for _ in range(3):
        before = utcnow()
        assert await h.drain() == 1
        [job] = await _jobs(h, JobType.ANALYZE_EMAIL)
        if job.status is JobStatus.QUEUED:
            delays.append(round((job.run_after - before).total_seconds()))
            await _fast_forward_backoff(h)
    [job] = await _jobs(h, JobType.ANALYZE_EMAIL)
    assert (job.status, job.attempts, job.finished_at is not None) == (JobStatus.DEAD, 3, True)
    assert delays == [5, 10]  # exponential backoff
    assert await h.drain() == 0
    assert (await member.get(f"/workflows/{run_id}")).json()["run"]["status"] == "failed"

    # A human retries via re-analysis once the provider is back (the script is exhausted → healthy).
    assert (await member.post(f"/workflows/{run_id}/reanalyze")).status_code == 202
    await h.drain()
    detail = (await member.get(f"/workflows/{run_id}")).json()
    assert detail["run"]["status"] == "pending_approval"
    assert detail["actions"]


async def test_invalid_model_output_is_not_retried(make_harness: HarnessFactory) -> None:
    agent = ScriptedAgent({"extract": [AgentOutputError("schema-invalid output")]})
    h = await make_harness(agent=agent)
    member, run_id = await _queue_analysis(h)
    assert await h.drain() == 1
    [job] = await _jobs(h, JobType.ANALYZE_EMAIL)
    assert (job.status, job.attempts) == (JobStatus.FAILED, 1)
    run = (await member.get(f"/workflows/{run_id}")).json()["run"]
    assert (run["status"], run["error_category"], run["error_message"]) == (
        "failed",
        "llm_output",
        "schema-invalid output",
    )


async def test_dead_workers_sync_job_is_recovered_by_lease_expiry(harness: Harness) -> None:
    member = await harness.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    async with harness.db.session() as s:
        claimed = await queue.claim_next(s, "worker-that-died")
    assert claimed is not None
    assert claimed.job_type is JobType.SYNC_MAILBOX
    assert await harness.drain() == 0  # leased (and its concurrency key is busy)
    assert (await member.post("/emails/sync", json={})).json()["job_ids"] == [str(claimed.id)]  # deduplicated
    assert await _expire_leases(harness) == 1
    assert await harness.drain() == 1
    [job] = await _jobs(harness, JobType.SYNC_MAILBOX)
    assert (job.status, job.attempts, job.locked_by) == (JobStatus.SUCCEEDED, 2, None)
    assert (await member.get("/emails")).json()["total"] == 10


async def test_lease_recovery_gives_up_after_the_attempt_budget(harness: Harness) -> None:
    member = await harness.persona("member")
    await member.post("/emails/sync", json={})
    for _ in range(3):
        async with harness.db.session() as s:
            assert await queue.claim_next(s, "flapping-worker") is not None
        assert await _expire_leases(harness) == 1
    [job] = await _jobs(harness, JobType.SYNC_MAILBOX)
    assert (job.status, job.attempts) == (JobStatus.DEAD, 3)
    assert job.last_error == "lease expired; attempts exhausted"
    assert await harness.drain() == 0


# ---------------------------------------------------------------------------------------------------
# Uncertain outcomes and recovery
# ---------------------------------------------------------------------------------------------------


async def test_uncertain_stays_uncertain_until_a_lookup_succeeds(harness: Harness) -> None:
    member, run_id = await _queue_analysis(harness)
    await harness.drain()
    work_item = find_action((await member.get(f"/workflows/{run_id}")).json(), "create_azure_devops_work_item")
    harness.ado.fail_mode = "lookup_unavailable_after_timeout"
    uncertain = await approve_and_execute(harness, member, work_item)
    assert uncertain["status"] == "uncertain"
    assert (await member.get(f"/workflows/{run_id}")).json()["run"]["status"] == "failed"
    assert (await member.post(f"/actions/{work_item['id']}/execute", json=review_body(work_item))).status_code == 409

    harness.ado.fail_mode = "auth_error"  # the lookup still cannot run
    assert (await member.post(f"/actions/{work_item['id']}/reconcile")).status_code == 202
    await harness.drain()
    still = (await member.get(f"/actions/{work_item['id']}")).json()
    assert still["status"] == "uncertain"
    assert still["permissions"]["can_reconcile"] is True
    events = (await member.get(f"/workflows/{run_id}/events")).json()
    assert any((e["message"] or "").startswith("Reconcile not performed") for e in events)

    harness.ado.fail_mode = "none"
    assert (await member.post(f"/actions/{work_item['id']}/reconcile")).status_code == 202
    await harness.drain()
    done = (await member.get(f"/actions/{work_item['id']}")).json()
    assert done["status"] == "completed"
    assert done["executions"][-1]["deduplicated"] is True
    assert len(MockAzureDevOpsClient().items()) == 1


async def test_reconciled_absence_allows_a_safe_retry(harness: Harness) -> None:
    member, run_id = await _queue_analysis(harness, FEATURE_ID)
    await harness.drain()
    [work_item] = (await member.get(f"/workflows/{run_id}")).json()["actions"]
    harness.ado.fail_mode = "lookup_unavailable_after_timeout"
    assert (await approve_and_execute(harness, member, work_item))["status"] == "uncertain"
    MockAzureDevOpsClient.reset()  # the provider confirms the item does not exist (e.g. it was rolled back)
    harness.ado.fail_mode = "none"
    assert (await member.post(f"/actions/{work_item['id']}/reconcile")).status_code == 202
    await harness.drain()
    failed = (await member.get(f"/actions/{work_item['id']}")).json()
    assert failed["status"] == "failed"
    assert "safe to retry" in failed["last_error"]
    assert failed["permissions"]["can_execute"] is True
    resp = await member.post(f"/actions/{work_item['id']}/execute", json=review_body(work_item))
    assert resp.status_code == 202
    await harness.drain()
    done = (await member.get(f"/actions/{work_item['id']}")).json()
    assert done["status"] == "completed"
    assert [e["status"] for e in done["executions"]] == ["uncertain", "failed", "succeeded"]
    assert len(MockAzureDevOpsClient().items()) == 1
    async with harness.db.session() as s:
        run = await s.get(WorkflowRun, uuid.UUID(run_id))
    assert run is not None
    assert run.status.value == "completed"


async def test_transient_adapter_failures_do_not_fail_the_workflow(harness: Harness) -> None:
    member, run_id = await _queue_analysis(harness, FEATURE_ID)
    await harness.drain()
    [work_item] = (await member.get(f"/workflows/{run_id}")).json()["actions"]
    harness.ado.fail_mode = "transient_once"
    done = await approve_and_execute(harness, member, work_item)
    assert done["status"] == "completed"
    assert done["executions"][0]["retry_count"] == 1
    assert (await member.get(f"/workflows/{run_id}")).json()["run"]["status"] == "completed"


async def test_analysis_after_email_body_purge_fails_cleanly(harness: Harness) -> None:
    member, run_id = await _queue_analysis(harness)
    from app.db.models import EmailMessage

    async with harness.db.session() as s:
        run = await s.get(WorkflowRun, uuid.UUID(run_id))
        assert run is not None
        await s.execute(
            update(EmailMessage)
            .where(EmailMessage.id == run.email_message_id)
            .values(body_text=None, body_purged_at=utcnow())
        )
        await s.commit()
    await harness.drain()
    detail = (await member.get(f"/workflows/{run_id}")).json()
    assert detail["run"]["status"] == "failed"
    assert "purged" in detail["run"]["error_message"]
    email = (await member.get(f"/emails/{detail['run']['email_message_id']}")).json()
    assert (email["body_purged"], email["analysis_status"]) == (True, "failed")
    [job] = await _jobs(harness, JobType.ANALYZE_EMAIL)
    assert job.status is JobStatus.FAILED  # an AppError: not retried
