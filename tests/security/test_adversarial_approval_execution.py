"""Adversarial QA: approval and execution integrity, idempotency and write reliability.

Attacks the single external write (the approval-gated Azure DevOps work-item create) through every entry
point: the HTTP API, queued worker jobs, LangGraph resume payloads, the fallback path when the graph is
not waiting, and reconcile. Every test asserts on observable behaviour: HTTP responses, persisted rows,
audit events and the requests that reached the (mocked) Azure DevOps API.

Already covered elsewhere and therefore not repeated here (see the final report for the full map):
PostgreSQL row-lock races (tests/integration/test_postgres_concurrency.py), the exhaustive
create-at-most-once engine model (tests/unit/integrations/test_idempotency_state_machine.py), REST
client retry/timeout classification (tests/unit/integrations/test_azure_devops_client*.py), the RBAC
persona x operation matrix (tests/integration/test_rbac_matrix.py).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.errors import InvalidStateTransition
from app.core.security import compute_payload_hash
from app.core.timeutil import utcnow
from app.db.models import ActionApproval, ActionExecution, Job, ProposedAction, User
from app.domain.enums import ApprovalDecision, ExecutionStatus, JobStatus, JobType, UserRole
from app.integrations.azure_devops.base import AzureDevOpsClient, WorkItemCreateRequest
from app.integrations.azure_devops.client import AzureDevOpsRestClient, PatAuth
from app.integrations.azure_devops.idempotency import CreateAttempt
from app.integrations.azure_devops.mock import MockAzureDevOpsClient
from app.integrations.microsoft_graph.base import MailChangeSet
from app.integrations.microsoft_graph.demo_mailbox import DemoMailboxProvider, load_demo_fixture
from app.services import email_sync_service as email_sync_module
from app.services import execution_service as execution_module
from app.services.authz import Actor, AuditedPermissionDenied
from app.services.identity import load_actor
from app.workers import queue
from app.workers.runner import JobHandlers, Worker
from tests.conftest import make_settings
from tests.integration.conftest import (
    Harness,
    HarnessFactory,
    Persona,
    analyze,
    approve_and_execute,
    assert_error,
    email_id_by_provider_id,
    find_action,
    review_body,
)
from tests.integration.test_adapters_end_to_end import ADO, PAT, FakeAzureDevOps

FIXTURE = load_demo_fixture()["messages"]
INCIDENT_ID = FIXTURE[0]["id"]
FEATURE_ID = next(m["id"] for m in FIXTURE if m["subject"].startswith("Feature request"))
WORK_ITEM = "create_azure_devops_work_item"


# ---------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------


async def _work_item(h: Harness, provider_id: str = FEATURE_ID) -> tuple[Persona, dict[str, Any], str]:
    """A member-owned workflow; returns (member, its work-item proposal, run id)."""
    member = await h.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await h.drain()
    detail = await analyze(h, member, await email_id_by_provider_id(h, member.user_id, provider_id))
    return member, find_action(detail, WORK_ITEM), detail["run"]["id"]


async def _approve(p: Persona, action: dict[str, Any]) -> dict[str, Any]:
    resp = await p.post(f"/actions/{action['id']}/approve", json=review_body(action))
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def _edit(p: Persona, action: dict[str, Any], title: str) -> httpx.Response:
    payload = {**action["payload"], "title": title}
    return await p.patch(f"/actions/{action['id']}", json={"expected_version": action["version"], "payload": payload})


async def _get(p: Persona, action: dict[str, Any]) -> dict[str, Any]:
    return dict((await p.get(f"/actions/{action['id']}")).json())


def _items() -> list[Any]:
    """Every work item that exists in the mock Azure DevOps organization (the process-wide store)."""
    return MockAzureDevOpsClient().items()


def _attempts(clients: list[MockAzureDevOpsClient]) -> int:
    """Create requests that reached the mock Azure DevOps API."""
    return sum(c.create_attempts for c in clients)


async def _executions(h: Harness, action_id: str | uuid.UUID) -> list[ActionExecution]:
    async with h.db.session() as s:
        stmt = (
            select(ActionExecution)
            .where(ActionExecution.action_id == uuid.UUID(str(action_id)))
            .order_by(ActionExecution.attempt_number)
        )
        return list((await s.execute(stmt)).scalars().all())


async def _approvals(h: Harness, action_id: str | uuid.UUID) -> list[ActionApproval]:
    async with h.db.session() as s:
        stmt = select(ActionApproval).where(ActionApproval.action_id == uuid.UUID(str(action_id)))
        return list((await s.execute(stmt)).scalars().all())


async def _live_approvals(h: Harness, action_id: str | uuid.UUID) -> list[ActionApproval]:
    return [
        a
        for a in await _approvals(h, action_id)
        if a.decision is ApprovalDecision.APPROVED and a.invalidated_at is None
    ]


async def _actor(h: Harness, user_id: uuid.UUID) -> Actor:
    async with h.db.session() as s:
        return await load_actor(s, user_id)


def _execute_decision(action: dict[str, Any], actor_user_id: uuid.UUID | str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "execute",
        "action_id": action["id"],
        "actor_user_id": str(actor_user_id),
        "version": action["version"],
        "payload_hash": action["payload_hash"],
        **extra,
    }


class HookedMockClient(MockAzureDevOpsClient):
    """Mock Azure DevOps whose first create attempt first runs `hook` (an interleaved request), optionally
    yielding to the event loop to widen race windows."""

    def __init__(self, hook: Callable[[], Awaitable[None]] | None, *, delay: float = 0.0) -> None:
        super().__init__()
        self._hook = hook
        self._delay = delay

    async def _attempt(self, request: WorkItemCreateRequest) -> CreateAttempt:
        if self._delay:
            await asyncio.sleep(self._delay)
        hook, self._hook = self._hook, None
        if hook is not None:
            await hook()
        return await super()._attempt(request)


@dataclass
class HookedAdo:
    hook: Callable[[], Awaitable[None]] | None = None
    delay: float = 0.0
    clients: list[MockAzureDevOpsClient] = field(default_factory=list)

    async def factory(self, _session: Any, _actor: Actor) -> AzureDevOpsClient:
        client = HookedMockClient(self.hook, delay=self.delay)
        self.hook = None
        self.clients.append(client)
        return client


# ---------------------------------------------------------------------------------------------------
# 1. Forged or stale approval requests
# ---------------------------------------------------------------------------------------------------


async def test_approve_with_any_stale_or_forged_version_hash_pair_records_no_approval(harness: Harness) -> None:
    member, v1, _ = await _work_item(harness)
    resp = await _edit(member, v1, "Edited before anyone approved (v2)")
    assert resp.status_code == 200, resp.text
    v2 = resp.json()
    assert (v2["version"], v2["status"]) == (2, "pending_approval")
    forged = "0" * 64
    for version, payload_hash in (
        (1, v1["payload_hash"]),  # what the first reviewer saw
        (2, v1["payload_hash"]),  # current version, old hash
        (1, v2["payload_hash"]),  # old version, current hash
        (2, forged),
        (3, v2["payload_hash"]),  # a version from the future
    ):
        body = {"version": version, "payload_hash": payload_hash}
        assert_error(await member.post(f"/actions/{v1['id']}/approve", json=body), 409, "stale_version")
        assert_error(await member.post(f"/actions/{v1['id']}/execute", json=body), 409, "stale_version")
    for malformed in ({"version": 0, "payload_hash": v2["payload_hash"]}, {"version": 2, "payload_hash": "x"}):
        assert_error(await member.post(f"/actions/{v1['id']}/approve", json=malformed), 422, "validation_failed")
    assert await _approvals(harness, v1["id"]) == []
    assert await harness.audit_events(event_type="action_approved") == []

    approved = await _approve(member, v2)
    assert approved["current_approval"]["payload_hash"] == v2["payload_hash"]
    [approval] = await _live_approvals(harness, v1["id"])
    assert (approval.action_version, approval.payload_hash) == (2, v2["payload_hash"])


async def _reach_status(h: Harness, member: Persona, wi: dict[str, Any], run_id: str, status: str) -> None:
    match status:
        case "rejected":
            assert (await member.post(f"/actions/{wi['id']}/reject", json={"reason": "no"})).status_code == 200
        case "cancelled":
            assert (await member.post(f"/actions/{wi['id']}/cancel", json={})).status_code == 200
        case "superseded":
            assert (await member.post(f"/workflows/{run_id}/reanalyze")).status_code == 202
        case "completed":
            assert (await approve_and_execute(h, member, wi))["status"] == "completed"
        case "failed":
            h.ado.fail_mode = "permanent_validation"
            assert (await approve_and_execute(h, member, wi))["status"] == "failed"
        case "uncertain":
            h.ado.fail_mode = "lookup_unavailable_after_timeout"
            assert (await approve_and_execute(h, member, wi))["status"] == "uncertain"
    await h.drain()
    h.ado.fail_mode = "none"
    assert (await _get(member, wi))["status"] == status


@pytest.mark.parametrize("status", ["rejected", "cancelled", "superseded", "completed", "failed", "uncertain"])
async def test_actions_past_review_can_never_be_approved_again(harness: Harness, status: str) -> None:
    member, wi, run_id = await _work_item(harness)
    await _reach_status(harness, member, wi, run_id, status)
    approvals_before = len(await _approvals(harness, wi["id"]))
    approved_events = len(await harness.audit_events(event_type="action_approved"))
    approver = await harness.persona("approver")
    for reviewer in (member, approver):
        assert_error(await reviewer.post(f"/actions/{wi['id']}/approve", json=review_body(wi)), 409)
    assert len(await _approvals(harness, wi["id"])) == approvals_before
    assert len(await harness.audit_events(event_type="action_approved")) == approved_events
    assert (await _get(member, wi))["status"] == status


async def test_review_and_execute_requests_need_the_csrf_token_of_their_own_live_session(harness: Harness) -> None:
    member, wi, _ = await _work_item(harness)
    approver = await harness.persona("approver")
    body = review_body(wi)
    attempts: list[tuple[str, str, dict[str, Any]]] = [
        ("post", f"/actions/{wi['id']}/approve", body),
        ("post", f"/actions/{wi['id']}/execute", body),
        ("post", f"/actions/{wi['id']}/reject", {"reason": "csrf"}),
        ("post", f"/actions/{wi['id']}/cancel", {}),
        ("post", f"/actions/{wi['id']}/reconcile", {}),
        ("patch", f"/actions/{wi['id']}", {"expected_version": 1, "payload": {**wi["payload"], "title": "x"}}),
    ]
    for method, path, json_body in attempts:
        send = getattr(member.client, method)
        assert_error(await send(f"/api/v1{path}", json=json_body), 403, "csrf_failed")
        # A valid token of *another* user's session is not accepted either.
        assert_error(await send(f"/api/v1{path}", json=json_body, headers=approver.headers), 403, "csrf_failed")

    # A session that was logged out, or replaced by a new sign-in, can no longer approve or execute.
    old_cookie = member.client.cookies.get(harness.settings.session_cookie_name)
    old_headers = dict(member.headers)
    assert (await member.client.post("/api/v1/auth/demo-login", json={"persona": "member"})).status_code == 200
    replay = harness.tracked_client()
    replay.cookies.set(harness.settings.session_cookie_name, old_cookie or "")
    resp = await replay.post(f"/api/v1/actions/{wi['id']}/approve", json=body, headers=old_headers)
    assert_error(resp, 401, "authentication_required")
    # The rotated session does not accept the previous session's CSRF token.
    resp = await member.client.post(f"/api/v1/actions/{wi['id']}/approve", json=body, headers=old_headers)
    assert_error(resp, 403, "csrf_failed")
    fresh_headers = {"X-CSRF-Token": member.client.cookies.get(harness.settings.csrf_cookie_name) or ""}
    assert (await member.client.post("/api/v1/auth/logout", headers=fresh_headers)).status_code == 204
    resp = await member.client.post(f"/api/v1/actions/{wi['id']}/execute", json=body, headers=fresh_headers)
    assert_error(resp, 401, "authentication_required")

    after = await _get(approver, wi)
    assert (after["status"], after["version"], after["current_approval"]) == ("pending_approval", 1, None)
    assert await _approvals(harness, wi["id"]) == []


async def _forger(h: Harness, kind: str, tenant_id: uuid.UUID) -> uuid.UUID:
    match kind:
        case "other_tenant_admin":
            return (await h.create_user(await h.create_tenant("Rival Corp"), UserRole.ADMIN)).id
        case "other_member":
            return (await h.create_user(tenant_id, UserRole.MEMBER)).id
        case "viewer":
            return (await h.create_user(tenant_id, UserRole.VIEWER)).id
    return uuid.uuid4()  # a user id that does not exist


@pytest.mark.parametrize(
    "forgery",
    ["review_decision_type", "claims_approval", "other_tenant_admin", "other_member", "viewer", "unknown_user"],
)
async def test_forged_resume_payloads_never_cause_a_write(harness: Harness, forgery: str) -> None:
    member, wi, run_id = await _work_item(harness)
    # A second, *approved* action in another run gives the forger a real approval id to point at.
    _, other, _ = await _work_item(harness, INCIDENT_ID)
    await _approve(member, other)
    [other_approval] = await _live_approvals(harness, other["id"])
    match forgery:
        case "review_decision_type":  # the graph has no "approve" command; it must be a no-op
            decision = {"type": "approve", "action_id": wi["id"], "actor_user_id": str(member.user_id)}
        case "claims_approval":  # "approved"/"approval_id" in the payload grant nothing
            decision = _execute_decision(wi, member.user_id, approved=True, approval_id=str(other_approval.id))
        case _:  # the target is approved by its owner; the forger is someone who must not execute it
            await _approve(member, wi)
            await harness.drain()
            decision = _execute_decision(wi, await _forger(harness, forgery, member.tenant_id))
    before = (await _get(member, wi))["status"]

    await harness.engine.resume(uuid.UUID(run_id), member.tenant_id, decision)  # type: ignore[arg-type]
    await harness.drain()

    assert _items() == []
    assert await _executions(harness, wi["id"]) == []
    assert (await _get(member, wi))["status"] == before


# ---------------------------------------------------------------------------------------------------
# 2. Approval of one payload followed by execution of a modified payload
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tamper", ["payload_only", "payload_and_hash", "version_only"])
async def test_payload_tampered_in_the_database_after_approval_is_never_sent(harness: Harness, tamper: str) -> None:
    member, wi, run_id = await _work_item(harness)
    await _approve(member, wi)
    await harness.drain()
    assert (await member.post(f"/actions/{wi['id']}/execute", json=review_body(wi))).status_code == 202
    # Someone with database write access edits the queued action (no version bump, no new approval).
    async with harness.db.session() as s:
        action = await s.get(ProposedAction, uuid.UUID(wi["id"]))
        assert action is not None
        payload = {**action.payload, "title": "Tampered after approval"}
        if tamper in ("payload_only", "payload_and_hash"):
            action.payload = payload
        if tamper == "payload_and_hash":  # a consistent forgery: the hash column matches the new payload
            action.payload_hash = compute_payload_hash(
                action.action_type.value, action.target_integration.value, payload
            )
        if tamper == "version_only":
            action.version += 1
        await s.commit()
        tampered = {"id": wi["id"], "version": action.version, "payload_hash": action.payload_hash}

    await harness.drain()  # the queued job carries what was approved; the row no longer matches it
    # The forger also queues a request that matches the tampered row exactly.
    await harness.engine.resume(uuid.UUID(run_id), member.tenant_id, _execute_decision(tampered, member.user_id))  # type: ignore[arg-type]
    await harness.drain()

    assert _items() == []
    assert _attempts(harness.ado.clients) == 0
    assert await _executions(harness, wi["id"]) == []
    if tamper == "payload_only":  # detected by recomputing the hash
        events = await harness.audit_events(event_type="payload_integrity_failure")
    else:  # the approval is bound to the old (version, hash) pair
        events = await harness.audit_events(event_type="permission_denied", actor_user_id=member.user_id)
    assert events
    assert {str(e.resource_id) for e in events} == {wi["id"]}


async def test_execute_job_queued_for_v1_never_sends_the_edited_and_reapproved_v2(harness: Harness) -> None:
    member, v1, _ = await _work_item(harness)
    await _approve(member, v1)
    await harness.drain()
    assert (await member.post(f"/actions/{v1['id']}/execute", json=review_body(v1))).status_code == 202
    resp = await _edit(member, v1, "Edited and re-approved while v1 was queued")
    assert resp.status_code == 200, resp.text
    v2 = resp.json()
    await _approve(member, v2)

    await harness.drain()  # runs the v1 execute job
    assert _items() == []
    assert await _executions(harness, v1["id"]) == []
    assert (await _get(member, v1))["status"] == "approved"

    # Only an explicit request for v2 executes v2, and exactly the payload that was approved is sent.
    assert (await member.post(f"/actions/{v1['id']}/execute", json=review_body(v2))).status_code == 202
    await harness.drain()
    [item] = _items()
    assert item.fields["System.Title"] == "Edited and re-approved while v1 was queued"
    [execution] = await _executions(harness, v1["id"])
    assert (execution.status, execution.action_version, execution.payload_hash) == (
        ExecutionStatus.SUCCEEDED,
        2,
        v2["payload_hash"],
    )


INTERVENTIONS = ["edit", "reject", "cancel_action", "cancel_workflow", "demote_owner", "deactivate_owner"]


@pytest.mark.parametrize("intervention", INTERVENTIONS)
async def test_a_queued_execute_job_is_reauthorized_when_it_runs(harness: Harness, intervention: str) -> None:
    member, wi, run_id = await _work_item(harness)
    await _approve(member, wi)
    await harness.drain()
    assert (await member.post(f"/actions/{wi['id']}/execute", json=review_body(wi))).status_code == 202
    match intervention:
        case "edit":
            assert (await _edit(member, wi, "Changed after the execute request")).status_code == 200
        case "reject":
            assert (
                await member.post(f"/actions/{wi['id']}/reject", json={"reason": "changed mind"})
            ).status_code == 200
        case "cancel_action":
            assert (await member.post(f"/actions/{wi['id']}/cancel", json={})).status_code == 200
        case "cancel_workflow":
            assert (await member.post(f"/workflows/{run_id}/cancel")).status_code == 200
        case "demote_owner" | "deactivate_owner":
            values = {"role": UserRole.VIEWER} if intervention == "demote_owner" else {"is_active": False}
            async with harness.db.session() as s:
                await s.execute(update(User).where(User.id == member.user_id).values(**values))
                await s.commit()

    await harness.drain()

    assert _items() == []
    assert _attempts(harness.ado.clients) == 0
    assert await _executions(harness, wi["id"]) == []
    if intervention == "demote_owner":
        denials = await harness.audit_events(event_type="permission_denied", actor_user_id=member.user_id)
        assert denials, "the refused execution must be audited"


async def test_review_and_workflow_writes_are_refused_while_the_external_write_is_in_flight(
    make_harness: HarnessFactory,
) -> None:
    hooked = HookedAdo()
    h = await make_harness(ado_factory=hooked.factory)
    member, wi, run_id = await _work_item(h)
    approver = await h.persona("approver")
    await _approve(member, wi)
    await h.drain()
    refused: dict[str, int] = {}

    async def interleave() -> None:  # runs inside the adapter call, after the action was claimed
        assert (await _get(member, wi))["status"] == "executing"
        attempts = {
            "edit": _edit(member, wi, "Edited while the create request was in flight"),
            "approve": approver.post(f"/actions/{wi['id']}/approve", json=review_body(wi)),
            "reject": approver.post(f"/actions/{wi['id']}/reject", json={"reason": "too late"}),
            "cancel_action": member.post(f"/actions/{wi['id']}/cancel", json={}),
            "cancel_workflow": member.post(f"/workflows/{run_id}/cancel"),
            "reanalyze": member.post(f"/workflows/{run_id}/reanalyze"),
            "execute_again": approver.post(f"/actions/{wi['id']}/execute", json=review_body(wi)),
            "reconcile": approver.post(f"/actions/{wi['id']}/reconcile"),
        }
        for name, request in attempts.items():
            refused[name] = (await request).status_code

    hooked.hook = interleave
    assert (await member.post(f"/actions/{wi['id']}/execute", json=review_body(wi))).status_code == 202
    await h.drain()

    assert refused == dict.fromkeys(refused, 409), refused
    assert len(refused) == 8
    done = await _get(member, wi)
    assert (done["status"], done["version"], done["payload_hash"]) == ("completed", 1, wi["payload_hash"])
    [item] = _items()
    assert item.fields["System.Title"] == wi["payload"]["title"]
    assert _attempts(hooked.clients) == 1


# ---------------------------------------------------------------------------------------------------
# 3. Replays, duplicated deliveries and concurrency
# ---------------------------------------------------------------------------------------------------


async def test_replayed_approve_execute_and_job_redelivery_create_one_work_item(harness: Harness) -> None:
    member, wi, _ = await _work_item(harness)
    first, second = await _approve(member, wi), await _approve(member, wi)
    assert first["current_approval"]["id"] == second["current_approval"]["id"]
    assert len(await _approvals(harness, wi["id"])) == 1
    assert len(await harness.audit_events(event_type="action_approved")) == 1
    await harness.drain()

    a = await member.post(f"/actions/{wi['id']}/execute", json=review_body(wi))
    b = await member.post(f"/actions/{wi['id']}/execute", json=review_body(wi))
    assert (a.status_code, b.status_code) == (202, 202)
    assert a.json()["job_id"] == b.json()["job_id"]  # deduplicated while queued
    await harness.drain()
    assert len(_items()) == 1

    # At-least-once delivery: the finished job is delivered again.
    async with harness.db.session() as s:
        await s.execute(update(Job).where(Job.id == uuid.UUID(a.json()["job_id"])).values(status=JobStatus.QUEUED))
        await s.commit()
    await harness.drain()
    # Replayed HTTP requests after completion.
    assert_error(await member.post(f"/actions/{wi['id']}/execute", json=review_body(wi)), 409)
    assert_error(await member.post(f"/actions/{wi['id']}/reconcile"), 409)
    assert_error(await member.post(f"/actions/{wi['id']}/approve", json=review_body(wi)), 409)
    await harness.drain()

    assert len(_items()) == 1
    assert _attempts(harness.ado.clients) == 1
    assert [e.status for e in await _executions(harness, wi["id"])] == [ExecutionStatus.SUCCEEDED]


async def test_execute_job_redelivered_while_its_write_is_in_flight_does_not_send_a_second_create(
    make_harness: HarnessFactory,
) -> None:
    hooked = HookedAdo()
    h = await make_harness(ado_factory=hooked.factory)
    member, wi, _ = await _work_item(h)
    await _approve(member, wi)
    await h.drain()

    async def redeliver() -> None:
        # The lease looked expired (e.g. missed heartbeats): a second worker runs the very same job row.
        async with h.db.session() as s:
            job = (
                await s.execute(
                    select(Job).where(Job.job_type == JobType.RESUME_WORKFLOW, Job.status == JobStatus.RUNNING)
                )
            ).scalar_one()
        second = Worker(h.db, h.settings, JobHandlers(h.db, h.settings, h.engine))
        await second.run_job(job)

    hooked.hook = redeliver
    member_run = (await member.post(f"/actions/{wi['id']}/execute", json=review_body(wi))).json()
    assert member_run["job_id"]
    await h.drain()

    assert len(_items()) == 1
    assert _attempts(hooked.clients) == 1
    assert [e.status for e in await _executions(h, wi["id"])] == [ExecutionStatus.SUCCEEDED]
    assert (await _get(member, wi))["status"] == "completed"
    # The redelivered copy really ran (and was turned away by the live execution, not by luck).
    events = (await member.get(f"/workflows/{member_run['action']['workflow_run_id']}/events")).json()
    assert any("handled 'execute' directly" in (e["message"] or "") for e in events), events


async def test_two_workers_racing_one_execute_job_on_sqlite_create_one_work_item(
    make_harness: HarnessFactory,
) -> None:
    hooked = HookedAdo(delay=0.05)
    h = await make_harness(ado_factory=hooked.factory)
    member, wi, _ = await _work_item(h)
    await _approve(member, wi)
    await h.drain()
    assert (await member.post(f"/actions/{wi['id']}/execute", json=review_body(wi))).status_code == 202
    second = Worker(h.db, h.settings, JobHandlers(h.db, h.settings, h.engine))

    await asyncio.gather(h.worker.run_once(), second.run_once(), h.worker.run_once())

    assert len(_items()) == 1
    assert _attempts(hooked.clients) == 1
    assert [e.status for e in await _executions(h, wi["id"])] == [ExecutionStatus.SUCCEEDED]


@pytest.mark.parametrize("first", ["reanalyze", "execute"])
async def test_reanalysis_racing_a_queued_execute_never_sends_a_superseded_or_duplicate_item(
    harness: Harness, first: str
) -> None:
    member, wi, run_id = await _work_item(harness)
    await _approve(member, wi)
    await harness.drain()
    requests = {
        "reanalyze": lambda: member.post(f"/workflows/{run_id}/reanalyze"),
        "execute": lambda: member.post(f"/actions/{wi['id']}/execute", json=review_body(wi)),
    }
    for name in (first, *(n for n in requests if n != first)):
        assert (await requests[name]()).status_code == 202, name

    await harness.drain()

    detail = (await member.get(f"/workflows/{run_id}")).json()
    live = [a for a in detail["actions"] if a["action_type"] == WORK_ITEM and a["status"] != "superseded"]
    if first == "reanalyze":
        assert _items() == []  # the approved proposal was superseded before the execute job ran
        assert (await _get(member, wi))["status"] == "superseded"
        assert [a["status"] for a in live] == ["pending_approval"]
        blocked = await harness.audit_events(event_type="execution_blocked", action_id=uuid.UUID(wi["id"]))
        assert blocked
    else:
        assert len(_items()) == 1
        assert [(a["id"], a["status"]) for a in live] == [(wi["id"], "completed")]
    assert len({a["idempotency_key"] for a in live}) == len(live)


class _ReplayingMailbox(DemoMailboxProvider):
    """Delivers every message of the first round twice per page, and replays the same page on each sync
    (duplicate change notifications / a delta link that went backwards)."""

    async def fetch_changes(self, delta_link: str | None, *, max_messages: int, initial_days: int) -> MailChangeSet:
        page = await super().fetch_changes(None, max_messages=max_messages, initial_days=initial_days)
        return MailChangeSet(upserts=[*page.upserts, *page.upserts], delta_link=page.delta_link, truncated=False)


async def test_duplicated_and_replayed_mail_deliveries_never_start_a_second_run_or_write(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = _ReplayingMailbox()
    monkeypatch.setattr(email_sync_module, "build_mailbox_provider", lambda *_a, **_k: mailbox)
    admin = await harness.persona("admin")
    current = (await admin.get("/settings")).json()
    body = {
        "expected_version": current["version"],
        "settings": {**current["settings"], "auto_analyze_new_emails": True},
    }
    assert (await admin.put("/settings", json=body)).status_code == 200
    member = await harness.persona("member")

    async def sync() -> None:
        assert (await member.post("/emails/sync", json={})).status_code == 202
        await harness.drain()

    async def counts() -> tuple[int, int, int, int]:
        async with harness.db.session() as s:
            jobs = (await s.execute(select(Job).where(Job.job_type == JobType.ANALYZE_EMAIL))).scalars().all()
            actions = (await s.execute(select(ProposedAction.id))).scalars().all()
        emails = (await member.get("/emails", params={"limit": 100})).json()["total"]
        runs = (await member.get("/workflows", params={"limit": 100})).json()["total"]
        return emails, runs, len(jobs), len(actions)

    await sync()
    first_round = await counts()
    assert first_round[:3] == (10, 10, 10)
    run = (await member.get("/workflows", params={"limit": 100})).json()["items"]
    feature_email = await email_id_by_provider_id(harness, member.user_id, FEATURE_ID)
    feature_run = next(r for r in run if r["email_message_id"] == str(feature_email))
    [wi] = (await member.get(f"/workflows/{feature_run['id']}/actions")).json()
    assert (await approve_and_execute(harness, member, wi))["status"] == "completed"

    for _ in range(2):
        await sync()

    assert await counts() == first_round
    assert len(_items()) == 1


# ---------------------------------------------------------------------------------------------------
# 4. Azure DevOps throttling, ambiguous outcomes and bounded retries (REST client + respx)
# ---------------------------------------------------------------------------------------------------


class ThrottlingAzureDevOps(FakeAzureDevOps):
    """Adds throttling modes to the shared fake: create "429" (Retry-After: 1 hour), "503_bare" (no
    Retry-After), and WIQL "429" (Retry-After: 1 hour)."""

    def create(self, request: httpx.Request, wit: str) -> httpx.Response:
        if request.url.params.get("validateOnly") != "true" and self.create_script:
            mode = self.create_script[0]
            if mode in ("429", "503_bare"):
                self.create_script.pop(0)
                self._log("create", request)
                if mode == "429":
                    return httpx.Response(429, headers={"Retry-After": "3600"}, json={"message": "throttled"})
                return httpx.Response(503, json={"message": "unavailable"})
        return super().create(request, wit)

    def wiql(self, request: httpx.Request) -> httpx.Response:
        if self.wiql_script and self.wiql_script[0] == "429":
            self.wiql_script.pop(0)
            self._log("wiql", request)
            return httpx.Response(429, headers={"Retry-After": "3600"}, json={"message": "throttled"})
        return super().wiql(request)


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@dataclass
class RestWorld:
    h: Harness
    ado: ThrottlingAzureDevOps
    sleeps: SleepRecorder
    member: Persona
    wi: dict[str, Any]
    run_id: str


@pytest.fixture
def throttling_ado() -> Iterator[ThrottlingAzureDevOps]:
    fake = ThrottlingAzureDevOps()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        fake.mount(router)
        yield fake


async def _rest_world(make_harness: HarnessFactory, database_url: str, ado: ThrottlingAzureDevOps) -> RestWorld:
    overrides: dict[str, Any] = {
        "azure_devops_mode": "pat",
        "azure_devops_organization": "contoso",
        "azure_devops_pat": PAT,
    }
    rest_settings: Settings = make_settings(database_url, **overrides)
    sleeps = SleepRecorder()

    async def factory(_session: Any, _actor: Actor) -> AzureDevOpsClient:
        return AzureDevOpsRestClient(rest_settings, auth=PatAuth(PAT), sleep=sleeps)

    h = await make_harness(ado_factory=factory, **overrides)
    member, wi, run_id = await _work_item(h, INCIDENT_ID)
    return RestWorld(h, ado, sleeps, member, wi, run_id)


@pytest.mark.parametrize("mode", ["429", "503_bare"])
async def test_refused_creates_are_retried_a_bounded_number_of_times_with_capped_backoff(
    make_harness: HarnessFactory, database_url: str, throttling_ado: ThrottlingAzureDevOps, mode: str
) -> None:
    throttling_ado.create_script = [mode] * 20
    w = await _rest_world(make_harness, database_url, throttling_ado)
    max_retries = w.h.settings.azure_devops_max_retries

    failed = await approve_and_execute(w.h, w.member, w.wi)

    assert throttling_ado.count("create") == max_retries + 1
    assert len(w.sleeps.calls) == max_retries
    if mode == "429":  # Retry-After is honoured but capped (RetryPolicy.max_retry_after = 30 s)
        assert w.sleeps.calls == [30.0] * max_retries
    else:  # full jitter within the exponential cap 0.5 s, 1 s, 2 s …
        assert all(0.0 <= d <= min(8.0, 0.5 * 2**i) for i, d in enumerate(w.sleeps.calls)), w.sleeps.calls
    [execution] = failed["executions"]
    assert (failed["status"], execution["retry_count"]) == ("failed", max_retries)
    assert execution["error_category"] == ("rate_limited" if mode == "429" else "transient")
    # The job queue never re-runs the write on its own: draining again sends nothing.
    async with w.h.db.session() as s:
        jobs = (await s.execute(select(Job).where(Job.job_type == JobType.RESUME_WORKFLOW))).scalars().all()
    assert {(j.status, j.attempts) for j in jobs} == {(JobStatus.SUCCEEDED, 1)}
    await w.h.drain()
    assert throttling_ado.count("create") == max_retries + 1
    assert throttling_ado.items == {}

    # Nothing was created, so the still-valid approval may be retried once the throttling stops.
    throttling_ado.create_script = []
    assert (await w.member.post(f"/actions/{w.wi['id']}/execute", json=review_body(w.wi))).status_code == 202
    await w.h.drain()
    assert (await _get(w.member, w.wi))["status"] == "completed"
    assert len(throttling_ado.items) == 1


async def test_lost_create_response_with_a_throttled_duplicate_check_is_sealed_as_uncertain_then_reconciled(
    make_harness: HarnessFactory, database_url: str, throttling_ado: ThrottlingAzureDevOps
) -> None:
    throttling_ado.create_script = ["timeout_after_create"]  # created server-side, response lost
    throttling_ado.wiql_script = ["ok"] + ["429"] * 4  # the follow-up duplicate check stays throttled
    w = await _rest_world(make_harness, database_url, throttling_ado)
    max_retries = w.h.settings.azure_devops_max_retries
    approver = await w.h.persona("approver")

    uncertain = await approve_and_execute(w.h, w.member, w.wi)

    assert uncertain["status"] == "uncertain", uncertain
    assert throttling_ado.count("create") == 1
    assert w.sleeps.calls == [30.0] * max_retries  # WIQL retries honour the capped Retry-After
    # Nothing but reconcile may move an uncertain action; nothing may re-send the create.
    for p in (w.member, approver):
        assert_error(await p.post(f"/actions/{w.wi['id']}/execute", json=review_body(w.wi)), 409)
        assert_error(await p.post(f"/actions/{w.wi['id']}/approve", json=review_body(w.wi)), 409)
        assert_error(await _edit(p, w.wi, "Edited while uncertain"), 409)
        assert_error(await p.post(f"/actions/{w.wi['id']}/reject", json={"reason": "x"}), 409)
        assert_error(await p.post(f"/actions/{w.wi['id']}/cancel", json={}), 409)
        assert_error(await p.post(f"/workflows/{w.run_id}/reanalyze"), 409)
    await w.h.drain()
    assert throttling_ado.count("create") == 1
    assert (await _get(w.member, w.wi))["status"] == "uncertain"

    assert (await w.member.post(f"/actions/{w.wi['id']}/reconcile")).status_code == 202
    await w.h.drain()
    done = await _get(w.member, w.wi)
    assert (done["status"], done["external_id"]) == ("completed", "4242")
    assert done["executions"][-1]["deduplicated"] is True
    assert throttling_ado.count("create") == 1
    assert len(throttling_ado.items) == 1
    assert ADO in done["external_url"]


async def _fast_forward_job_backoff(h: Harness) -> None:
    """The job queue's retry backoff (5 s, 10 s, ...) elapses; nothing else changes."""
    async with h.db.session() as s:
        await s.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(run_after=utcnow()))
        await s.commit()


async def _expire_execution_lease(h: Harness, action_id: str) -> None:
    """Time passes beyond JOB_LEASE_SECONDS: the RUNNING execution row is now older than the lease, and the
    scheduler's lease-recovery sweep runs."""
    async with h.db.session() as s:
        await s.execute(
            update(ActionExecution)
            .where(ActionExecution.action_id == uuid.UUID(action_id), ActionExecution.status == ExecutionStatus.RUNNING)
            .values(started_at=utcnow() - timedelta(seconds=h.settings.job_lease_seconds + 5))
        )
        await s.commit()
        await queue.recover_expired_leases(s, h.settings.job_lease_seconds)


# DEFECT: an unrecorded write stays "executing" forever: the job retry is swallowed, /reconcile needs "uncertain".
async def test_a_write_whose_outcome_could_not_be_recorded_still_converges_through_the_tag_lookup(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    member, wi, _ = await _work_item(harness)
    await _approve(member, wi)
    await harness.drain()
    real_record_audit = execution_module.record_audit
    failures: list[str] = []

    def database_fails_while_recording_the_outcome(session: Any, **kwargs: Any) -> Any:
        if kwargs.get("event_type") == "external_write" and not failures:
            failures.append(kwargs["event_type"])
            raise OperationalError("COMMIT", {}, Exception("connection to the database was lost"))
        return real_record_audit(session, **kwargs)

    monkeypatch.setattr(execution_module, "record_audit", database_fails_while_recording_the_outcome)
    assert (await member.post(f"/actions/{wi['id']}/execute", json=review_body(wi))).status_code == 202

    await harness.drain()  # the work item is created in Azure DevOps, then recording the outcome fails
    assert failures == ["external_write"]
    assert len(_items()) == 1
    for _ in range(3):  # the job queue retries within its attempt budget (5 s, 10 s: well inside the lease)
        await _fast_forward_job_backoff(harness)
        await harness.drain()
    await _expire_execution_lease(harness, wi["id"])  # then time passes; no worker is running anything
    await harness.drain()

    status = (await _get(member, wi))["status"]
    if status in ("executing", "uncertain"):  # the owner must be able to resolve the unknown outcome
        resp = await member.post(f"/actions/{wi['id']}/reconcile")
        assert resp.status_code == 202, f"an unrecorded write stuck in '{status}' cannot be reconciled: {resp.text}"
        await harness.drain()
    done = await _get(member, wi)
    assert done["status"] == "completed", done
    assert done["external_id"] == str(_items()[0].id)
    assert len(_items()) == 1
    assert _attempts(harness.ado.clients) == 1  # converged through the tag lookup, never re-sent


# ---------------------------------------------------------------------------------------------------
# 5. Rejected, cancelled, superseded and failed actions never reach execution through any path
# ---------------------------------------------------------------------------------------------------


async def _closed_action(h: Harness, status: str) -> tuple[Persona, dict[str, Any], str]:
    member, wi, run_id = await _work_item(h)
    if status == "failed_then_edited":
        h.ado.fail_mode = "permanent_validation"
        assert (await approve_and_execute(h, member, wi))["status"] == "failed"
        h.ado.fail_mode = "none"
        assert (await _edit(member, wi, "Edited after the failure, not re-approved")).status_code == 200
    else:
        await _approve(member, wi)
        await h.drain()
        await _reach_status(h, member, wi, run_id, status)
    await h.drain()
    return member, wi, run_id


PATHS = ["api_execute", "api_reconcile", "resume_execute", "resume_reconcile", "service_execute", "service_reconcile"]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("status", ["rejected", "cancelled", "superseded", "failed_then_edited"])
async def test_closed_actions_never_reach_the_external_write(harness: Harness, status: str, path: str) -> None:
    member, wi, run_id = await _closed_action(harness, status)
    current = await _get(member, wi)
    attempts_before = _attempts(harness.ado.clients)
    executions_before = len(await _executions(harness, wi["id"]))
    decision = _execute_decision(current, member.user_id)

    match path:
        case "api_execute":
            assert (await member.post(f"/actions/{wi['id']}/execute", json=review_body(current))).status_code in (
                403,
                409,
            )
        case "api_reconcile":
            assert_error(await member.post(f"/actions/{wi['id']}/reconcile"), 409)
        case "resume_execute" | "resume_reconcile":
            if path == "resume_reconcile":
                decision["type"] = "reconcile"
            await harness.engine.resume(uuid.UUID(run_id), member.tenant_id, decision)  # type: ignore[arg-type]
        case "service_execute":
            # An action back in review has no approval (403); terminal ones are an invalid transition (409).
            expected = AuditedPermissionDenied if status == "failed_then_edited" else InvalidStateTransition
            async with harness.db.session() as s:
                with pytest.raises(expected):
                    await harness.execution.execute(s, await _actor(harness, member.user_id), uuid.UUID(wi["id"]))
        case "service_reconcile":
            async with harness.db.session() as s:
                with pytest.raises(InvalidStateTransition):
                    await harness.execution.reconcile(s, await _actor(harness, member.user_id), uuid.UUID(wi["id"]))
    await harness.drain()

    assert _attempts(harness.ado.clients) == attempts_before
    assert _items() == []
    assert len(await _executions(harness, wi["id"])) == executions_before
    assert (await _get(member, wi))["status"] == current["status"]


# DEFECT: cancelling a workflow skips its uncertain action (approval stays live); reconcile→failed then lets it execute.
async def test_cancelled_workflow_never_executes_an_action_that_was_uncertain_when_it_was_cancelled(
    harness: Harness,
) -> None:
    member, wi, run_id = await _work_item(harness)
    harness.ado.fail_mode = "lookup_unavailable_after_timeout"
    assert (await approve_and_execute(harness, member, wi))["status"] == "uncertain"
    cancel = await member.post(f"/workflows/{run_id}/cancel")
    await harness.drain()
    if cancel.status_code != 200:  # refusing the cancel while an outcome is unknown is equally safe
        assert_error(cancel, 409)
        return
    assert (await member.get(f"/workflows/{run_id}")).json()["run"]["status"] == "cancelled"
    MockAzureDevOpsClient.reset()  # Azure DevOps confirms the item does not exist
    harness.ado.fail_mode = "none"
    assert (await member.post(f"/actions/{wi['id']}/reconcile")).status_code == 202
    await harness.drain()
    attempts_before = _attempts(harness.ado.clients)

    # Every remaining route to a write: retry under the old approval, or edit → re-approve → execute.
    resp = await member.post(f"/actions/{wi['id']}/execute", json=review_body(wi))
    await harness.drain()
    edited = await _edit(member, (await _get(member, wi)), "Edited after the workflow was cancelled")
    if edited.status_code == 200:
        v2 = edited.json()
        if (await member.post(f"/actions/{wi['id']}/approve", json=review_body(v2))).status_code == 200:
            await member.post(f"/actions/{wi['id']}/execute", json=review_body(v2))
            await harness.drain()

    assert _items() == [], "a cancelled workflow produced an external write"
    assert _attempts(harness.ado.clients) == attempts_before
    assert resp.status_code in (403, 409), resp.text
    assert (await member.get(f"/workflows/{run_id}")).json()["run"]["status"] == "cancelled"
