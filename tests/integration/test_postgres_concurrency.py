"""Concurrency guarantees that only a real PostgreSQL can demonstrate (row locks, SKIP LOCKED, partial indexes).

Run with: TEST_DATABASE_URL=postgresql+psycopg://… uv run pytest -m postgres tests/integration
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections import Counter
from typing import Any

import pytest
from sqlalchemy import func, select

from app.core.errors import Conflict
from app.db.models import ActionApproval, ActionExecution, Job, ProposedAction, WorkflowRun
from app.domain.enums import ApprovalDecision, ExecutionStatus, JobStatus, JobType
from app.integrations.azure_devops.base import AzureDevOpsClient, WorkItemCreateRequest
from app.integrations.azure_devops.idempotency import CreateAttempt
from app.integrations.azure_devops.mock import MockAzureDevOpsClient
from app.integrations.microsoft_graph.demo_mailbox import load_demo_fixture
from app.services.authz import Actor
from app.services.identity import load_actor
from app.services.settings_service import load_settings_row
from app.workers import queue
from app.workers.runner import JobHandlers, Worker
from tests.integration.conftest import Harness, HarnessFactory, Persona, analyze, email_id_by_provider_id, review_body

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
        reason="requires PostgreSQL (set TEST_DATABASE_URL=postgresql+psycopg://...)",
    ),
]

FIXTURE = load_demo_fixture()["messages"]
FEATURE_ID = next(m["id"] for m in FIXTURE if m["subject"].startswith("Feature request"))


class SlowMockClient(MockAzureDevOpsClient):
    """Widens the race window: the create attempt yields to the event loop before writing."""

    async def _attempt(self, request: WorkItemCreateRequest) -> CreateAttempt:
        await asyncio.sleep(0.05)
        return await super()._attempt(request)


async def _approved_feature_item(h: Harness) -> tuple[Persona, dict[str, Any]]:
    member = await h.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await h.drain()
    detail = await analyze(h, member, await email_id_by_provider_id(h, member.user_id, FEATURE_ID))
    [work_item] = detail["actions"]
    assert (await member.post(f"/actions/{work_item['id']}/approve", json=review_body(work_item))).status_code == 200
    await h.drain()
    return member, work_item


async def test_concurrent_execute_calls_create_exactly_one_work_item(make_harness: HarnessFactory) -> None:
    clients: list[SlowMockClient] = []

    async def factory(_s: Any, _a: Actor) -> AzureDevOpsClient:
        client = SlowMockClient()
        clients.append(client)
        return client

    h = await make_harness(ado_factory=factory)
    member, work_item = await _approved_feature_item(h)
    action_id = uuid.UUID(work_item["id"])
    async with h.db.session() as s:
        actor = await load_actor(s, member.user_id)

    async def execute() -> Any:
        async with h.db.session() as s:
            return await h.execution.execute(s, actor, action_id)

    results = await asyncio.gather(*(execute() for _ in range(5)), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert all(isinstance(e, Conflict) for e in errors), errors
    executions = [r for r in results if not isinstance(r, BaseException)]
    assert executions, "at least one caller performed (or returned) the execution"
    assert {e.status for e in executions} == {ExecutionStatus.SUCCEEDED}
    assert len({e.id for e in executions}) == 1
    assert sum(c.create_attempts for c in clients) == 1
    assert len(MockAzureDevOpsClient().items()) == 1
    async with h.db.session() as s:
        statuses = (
            (await s.execute(select(ActionExecution.status).where(ActionExecution.action_id == action_id)))
            .scalars()
            .all()
        )
        action = await s.get(ProposedAction, action_id)
    assert statuses == [ExecutionStatus.SUCCEEDED]
    assert action is not None
    assert action.status.value == "completed"


async def test_concurrent_workers_never_run_a_job_twice_and_respect_concurrency_keys(harness: Harness) -> None:
    keys = [None] * 8 + ["shared-a"] * 5 + ["shared-b"] * 5 + [f"own-{i}" for i in range(6)]
    async with harness.db.session() as s:
        for i, key in enumerate(keys):
            await queue.enqueue(
                s, job_type=JobType.RETENTION_PURGE, payload={"n": i}, tenant_id=None, concurrency_key=key
            )
        await s.commit()
    claimed: list[uuid.UUID] = []
    active: Counter[str] = Counter()
    overlaps: list[str] = []
    done = 0

    async def worker(name: str) -> None:
        nonlocal done
        while done < len(keys):
            async with harness.db.session() as s:
                job = await queue.claim_next(s, name)
            if job is None:
                await asyncio.sleep(0.005)
                continue
            key = job.concurrency_key
            if key is not None:
                if active[key]:
                    overlaps.append(key)
                active[key] += 1
            claimed.append(job.id)
            await asyncio.sleep(0.01)  # "work"
            if key is not None:
                active[key] -= 1
            async with harness.db.session() as s:
                await queue.complete(s, job.id, {"by": name})
            done += 1

    await asyncio.wait_for(asyncio.gather(*(worker(f"w{i}") for i in range(5))), timeout=60)
    assert len(claimed) == len(set(claimed)) == len(keys)
    assert overlaps == []
    async with harness.db.session() as s:
        jobs = (await s.execute(select(Job))).scalars().all()
    assert {(j.status, j.attempts) for j in jobs} == {(JobStatus.SUCCEEDED, 1)}
    assert len({j.result["by"] for j in jobs}) > 1  # the work really was shared


async def test_two_workers_drain_analysis_jobs_without_duplicates(harness: Harness) -> None:
    member = await harness.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await harness.drain()
    # Persist the tenant settings row first: its lazy creation races (see the xfail test below).
    assert (await member.get("/settings")).status_code == 200
    emails = (await member.get("/emails", params={"limit": 100})).json()["items"][:6]
    for email in emails:
        assert (await member.post(f"/emails/{email['id']}/analyze")).status_code == 202
    second = Worker(harness.db, harness.settings, JobHandlers(harness.db, harness.settings, harness.engine))
    processed = await asyncio.gather(harness.worker.run_once(), second.run_once())
    assert sum(processed) == len(emails)
    async with harness.db.session() as s:
        jobs = (await s.execute(select(Job).where(Job.job_type == JobType.ANALYZE_EMAIL))).scalars().all()
        runs = (await s.execute(select(WorkflowRun.status))).scalars().all()
        keys = (await s.execute(select(ProposedAction.idempotency_key))).scalars().all()
    assert {(j.status, j.attempts) for j in jobs} == {(JobStatus.SUCCEEDED, 1)}
    assert [r.value for r in runs] == ["pending_approval"] * len(emails)
    assert len(keys) == len(set(keys))


async def test_concurrent_analyze_requests_create_one_active_run(harness: Harness) -> None:
    member = await harness.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await harness.drain()
    email_id = (await member.get("/emails")).json()["items"][0]["id"]
    responses = await asyncio.gather(*(member.post(f"/emails/{email_id}/analyze") for _ in range(8)))
    assert sorted(r.status_code for r in responses) == [200] * 7 + [202], [r.text for r in responses]
    assert len({r.json()["workflow_run"]["id"] for r in responses}) == 1
    assert [r.json()["created"] for r in responses].count(True) == 1
    async with harness.db.session() as s:
        runs = (
            await s.execute(
                select(func.count()).select_from(WorkflowRun).where(WorkflowRun.email_message_id == email_id)
            )
        ).scalar_one()
        jobs = (
            await s.execute(select(func.count()).select_from(Job).where(Job.job_type == JobType.ANALYZE_EMAIL))
        ).scalar_one()
    assert (runs, jobs) == (1, 1)
    assert len(await harness.audit_events(event_type="analysis_requested")) == 1


async def test_concurrent_sync_requests_queue_one_job(harness: Harness) -> None:
    member = await harness.persona("member")
    responses = await asyncio.gather(*(member.post("/emails/sync", json={}) for _ in range(6)))
    assert {r.status_code for r in responses} == {202}
    assert len({tuple(r.json()["job_ids"]) for r in responses}) == 1
    async with harness.db.session() as s:
        count = (
            await s.execute(select(func.count()).select_from(Job).where(Job.job_type == JobType.SYNC_MAILBOX))
        ).scalar_one()
    assert count == 1


async def test_concurrent_approvals_of_the_same_version_record_one_approval(harness: Harness) -> None:
    member = await harness.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await harness.drain()
    detail = await analyze(harness, member, await email_id_by_provider_id(harness, member.user_id, FEATURE_ID))
    [work_item] = detail["actions"]
    approver, admin = await harness.persona("approver"), await harness.persona("admin")
    body = review_body(work_item)
    responses = await asyncio.gather(
        *(p.post(f"/actions/{work_item['id']}/approve", json=body) for p in (member, approver, admin, member, approver))
    )
    assert {r.status_code for r in responses} == {200}, [r.text for r in responses]
    async with harness.db.session() as s:
        approvals = (
            (await s.execute(select(ActionApproval).where(ActionApproval.action_id == uuid.UUID(work_item["id"]))))
            .scalars()
            .all()
        )
    assert [(a.decision, a.invalidated_at) for a in approvals] == [(ApprovalDecision.APPROVED, None)]
    assert len(await harness.audit_events(event_type="action_approved")) == 1


async def test_concurrent_approve_and_edit_never_leave_a_stale_approval(harness: Harness) -> None:
    member = await harness.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await harness.drain()
    detail = await analyze(harness, member, await email_id_by_provider_id(harness, member.user_id, FEATURE_ID))
    [work_item] = detail["actions"]
    approver = await harness.persona("approver")
    edit = member.patch(
        f"/actions/{work_item['id']}",
        json={"expected_version": 1, "payload": {**work_item["payload"], "title": "Raced edit"}},
    )
    approve = approver.post(f"/actions/{work_item['id']}/approve", json=review_body(work_item))
    edit_resp, approve_resp = await asyncio.gather(edit, approve)
    assert edit_resp.status_code == 200, edit_resp.text
    assert approve_resp.status_code in (200, 409), approve_resp.text
    action = (await member.get(f"/actions/{work_item['id']}")).json()
    assert (action["version"], action["status"], action["current_approval"]) == (2, "pending_approval", None)
    assert all(a["invalidated_at"] is not None for a in action["approvals"] if a["decision"] == "approved")
    resp = await approver.post(f"/actions/{work_item['id']}/execute", json=review_body(work_item))
    assert resp.status_code == 409


async def test_first_concurrent_use_of_tenant_settings_does_not_conflict(harness: Harness) -> None:
    tenant_id = await harness.create_tenant("Fresh Co")

    async def first_use() -> None:
        async with harness.db.session() as s:
            await load_settings_row(s, tenant_id, harness.settings)
            await asyncio.sleep(0.2)  # flushed, not yet committed: the other transaction cannot see the row
            await s.commit()

    await asyncio.gather(first_use(), first_use())
