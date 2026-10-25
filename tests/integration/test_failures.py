"""Failure handling: transient retries, ambiguous timeouts, uncertain outcomes, recovery, job queue."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import update

from app.core.timeutil import utcnow
from app.db.models import ActionExecution, Job, ProposedAction, User
from app.domain.enums import ActionStatus, ExecutionStatus, JobStatus, JobType
from app.services.approval_service import current_approval
from app.services.identity import actor_from_user
from app.workers import queue
from tests.integration.conftest import Harness, find_action, sync_and_analyze

SPEC_SUBJECT = "Production login failures"


async def _approved_work_item(h: Harness) -> tuple[dict[str, str], dict]:  # type: ignore[type-arg]
    headers = await h.login("member")
    detail = await sync_and_analyze(h, headers, SPEC_SUBJECT)
    wi = find_action(detail, "create_azure_devops_work_item")
    body = {"version": wi["version"], "payload_hash": wi["payload_hash"]}
    assert (await h.post(f"/actions/{wi['id']}/approve", headers, json=body)).status_code == 200
    return headers, wi


async def _execute(h: Harness, headers: dict[str, str], wi: dict) -> dict:  # type: ignore[type-arg]
    body = {"version": wi["version"], "payload_hash": wi["payload_hash"]}
    resp = await h.post(f"/actions/{wi['id']}/execute", headers, json=body)
    assert resp.status_code == 202, resp.text
    await h.drain()
    return (await h.get(f"/actions/{wi['id']}")).json()


async def test_transient_failure_is_retried_within_bounds(harness: Harness) -> None:
    harness.ado.fail_mode = "transient_once"
    headers, wi = await _approved_work_item(harness)
    action = await _execute(harness, headers, wi)
    assert action["status"] == "completed"
    assert action["executions"][-1]["retry_count"] >= 1
    assert len(harness.ado.clients[-1].items()) == 1


async def test_timeout_after_write_does_not_duplicate(harness: Harness) -> None:
    harness.ado.fail_mode = "timeout_after_create_once"
    headers, wi = await _approved_work_item(harness)
    action = await _execute(harness, headers, wi)
    assert action["status"] == "completed", action
    assert len(harness.ado.clients[-1].items()) == 1


async def test_uncertain_outcome_is_recorded_and_reconcilable(harness: Harness) -> None:
    harness.ado.fail_mode = "lookup_unavailable_after_timeout"
    headers, wi = await _approved_work_item(harness)
    action = await _execute(harness, headers, wi)
    assert action["status"] == "uncertain", action
    assert action["executions"][-1]["status"] == "uncertain"
    assert action["permissions"]["can_reconcile"] is True
    # A plain retry is not allowed while the outcome is unknown.
    body = {"version": wi["version"], "payload_hash": wi["payload_hash"]}
    assert (await harness.post(f"/actions/{wi['id']}/execute", headers, json=body)).status_code == 409

    harness.ado.fail_mode = "none"  # lookup works again
    resp = await harness.post(f"/actions/{wi['id']}/reconcile", headers)
    assert resp.status_code == 202, resp.text
    await harness.drain()
    action = (await harness.get(f"/actions/{wi['id']}")).json()
    assert action["status"] in ("completed", "failed")
    if action["status"] == "completed":
        assert action["executions"][-1]["deduplicated"] is True
    assert len(harness.ado.clients[-1].items()) <= 1


async def test_permanent_validation_error_fails_without_retry_and_is_recoverable(harness: Harness) -> None:
    harness.ado.fail_mode = "permanent_validation"
    headers, wi = await _approved_work_item(harness)
    action = await _execute(harness, headers, wi)
    assert action["status"] == "failed"
    last = action["executions"][-1]
    assert last["status"] == "failed"
    assert last["error_category"] == "validation"
    assert last["retry_count"] == 0
    # Recovery: retry under the still-valid approval once the cause is fixed.
    harness.ado.fail_mode = "none"
    action = await _execute(harness, headers, wi)
    assert action["status"] == "completed"
    assert len(action["executions"]) == 2


async def test_crashed_execution_is_reconciled_not_resent(harness: Harness) -> None:
    _, wi = await _approved_work_item(harness)
    action_id = uuid.UUID(wi["id"])
    async with harness.db.session() as s:
        action = await s.get(ProposedAction, action_id)
        assert action is not None
        user = await s.get(User, action.owner_user_id)
        assert user is not None
        # Simulate a worker that claimed the action and died before recording an outcome.
        approval = await current_approval(s, action)
        assert approval is not None
        s.add(
            ActionExecution(
                tenant_id=action.tenant_id,
                action_id=action.id,
                approval_id=approval.id,
                requested_by_user_id=user.id,
                attempt_number=1,
                status=ExecutionStatus.RUNNING,
                action_version=action.version,
                payload_hash=action.payload_hash,
                started_at=utcnow() - timedelta(seconds=harness.settings.job_lease_seconds + 60),
            )
        )
        action.status = ActionStatus.EXECUTING
        await s.commit()
        execution = await harness.execution.execute(s, actor_from_user(user), action_id)
        assert execution.provider_response.get("reconciled") is True
    action_json = (await harness.get(f"/actions/{wi['id']}")).json()
    # Nothing was created before the "crash", so reconciliation confirms absence → failed (safe to retry).
    assert action_json["status"] == "failed"
    assert harness.ado.clients
    assert not harness.ado.clients[-1].items()


async def test_job_queue_dedupe_concurrency_and_lease_recovery(harness: Harness) -> None:
    async with harness.db.session() as s:
        a = await queue.enqueue(
            s, job_type=JobType.RETENTION_PURGE, payload={}, tenant_id=None, dedupe_key="k1", concurrency_key="c1"
        )
        b = await queue.enqueue(
            s, job_type=JobType.RETENTION_PURGE, payload={}, tenant_id=None, dedupe_key="k1", concurrency_key="c1"
        )
        c = await queue.enqueue(
            s, job_type=JobType.RETENTION_PURGE, payload={}, tenant_id=None, dedupe_key="k2", concurrency_key="c1"
        )
        await s.commit()
        assert a.id == b.id
        assert c.id != a.id

        first = await queue.claim_next(s, "w1")
        assert first is not None
        # Same concurrency key is blocked while the first job runs.
        assert await queue.claim_next(s, "w2") is None

        await s.execute(update(Job).where(Job.id == first.id).values(locked_at=utcnow() - timedelta(hours=1)))
        await s.commit()
        assert await queue.recover_expired_leases(s, lease_seconds=60) == 1
        job = await s.get(Job, first.id)
        assert job is not None
        await s.refresh(job)
        assert job.status is JobStatus.QUEUED

        claimed = await queue.claim_next(s, "w3")
        assert claimed is not None
        status = await queue.fail(s, claimed, "boom", retryable=True)
        assert status in (JobStatus.QUEUED, JobStatus.DEAD)
        # The released claim is gone: releasing it again is a fenced no-op (Prompt 5), not a second outcome.
        assert await queue.fail(s, claimed, "boom", retryable=False) is None
        await s.execute(update(Job).where(Job.id == claimed.id).values(run_after=utcnow()))  # backoff elapses
        await s.commit()
        reclaimed = await queue.claim_next(s, "w4")
        assert reclaimed is not None
        status = await queue.fail(s, reclaimed, "boom", retryable=False)
        assert status is JobStatus.FAILED
