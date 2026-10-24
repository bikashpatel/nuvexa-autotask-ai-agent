"""Container-level resilience: durable queue, restarts, persisted sessions and checkpoints.

These tests restart containers of the stack under test, so they only run when E2E_COMPOSE_PROJECT
names the isolated Compose project started by scripts/e2e.sh.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx

from e2e.conftest import API, ApiUser, compose, find_action, psql, wait_for

Login = Callable[[str], ApiUser]


def _wait_api_ready() -> None:
    wait_for(lambda: httpx.get(f"{API}/health/ready", timeout=3).status_code == 200, "API ready", timeout=120)


def test_jobs_queued_while_worker_is_down_are_processed_after_restart(login: Login) -> None:
    member = login("member")
    email = member.email_by_subject("Please prepare the Q4 capacity planning report")
    compose("stop", "worker")
    try:
        resp = member.post(f"/emails/{email['id']}/analyze")
        assert resp.status_code in (200, 202), resp.text
        run_id = str(uuid.UUID(resp.json()["workflow_run"]["id"]))  # validated before use in SQL
        assert member.get(f"/workflows/{run_id}").json()["run"]["status"] == "received"
        queued = psql(f"select status from jobs where payload->>'workflow_run_id' = '{run_id}' limit 1")
        assert queued == "queued"
    finally:
        compose("start", "worker")
    detail = wait_for(
        lambda: (d := member.get(f"/workflows/{run_id}").json())["run"]["status"] == "pending_approval" and d,
        "analysis after worker restart",
        timeout=90,
    )
    assert detail["analysis"]["category"] == "task_request"


def test_interrupted_workflow_resumes_from_postgres_checkpoint_after_worker_restart(login: Login) -> None:
    member = login("member")
    detail = member.analyze(member.email_by_subject("Bug: mobile app crashes when uploading"))
    run_id = str(uuid.UUID(detail["run"]["id"]))
    assert int(psql(f"select count(*) from checkpoints where thread_id = '{run_id}'")) > 0
    compose("restart", "worker")
    wi = find_action(detail, "create_azure_devops_work_item")
    assert member.approve(wi).status_code == 200
    assert member.execute(wi).status_code == 202
    done = member.wait_action(wi["id"], {"completed", "failed", "uncertain"}, timeout=90)
    assert done["status"] == "completed"
    resumed = [e for e in member.get(f"/workflows/{run_id}/events").json() if e["event_type"] == "resume"]
    assert resumed, "the new worker did not resume the interrupted LangGraph thread"


def test_forwarded_for_header_cannot_spoof_the_client_ip(fresh_login: Login) -> None:
    spoofed = "203.0.113.77"
    resp = httpx.post(f"{API}/auth/demo-login", json={"persona": "approver"}, headers={"X-Forwarded-For": spoofed})
    assert resp.status_code == 200
    latest_ip = psql("select ip_address from audit_events where event_type = 'login' order by created_at desc limit 1")
    assert latest_ip
    assert latest_ip != spoofed


def test_sessions_survive_api_restart(login: Login) -> None:
    member = login("member")
    assert member.get("/auth/me").status_code == 200
    compose("restart", "api")
    _wait_api_ready()
    assert member.get("/auth/me").status_code == 200  # server-side sessions live in PostgreSQL


def test_database_invariants_hold_after_the_suite(login: Login) -> None:
    login("member")
    duplicates = psql(
        "select count(*) from (select action_id from action_executions where status = 'succeeded' "
        "group by action_id having count(*) > 1) d"
    )
    assert duplicates == "0"
    double_runs = psql(
        "select count(*) from (select email_message_id from workflow_runs where status not in "
        "('completed','rejected','cancelled') group by email_message_id having count(*) > 1) d"
    )
    assert double_runs == "0"
    executed_without_approval = psql(
        "select count(*) from action_executions e left join action_approvals a on a.id = e.approval_id "
        "where a.id is null or a.decision <> 'approved'"
    )
    assert executed_without_approval == "0"
    plaintext_tokens = psql("select count(*) from credential_store where encrypted_payload::text like '%refresh%'")
    assert plaintext_tokens == "0"
