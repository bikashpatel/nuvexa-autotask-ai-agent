"""Black-box API tests through nginx → API container → worker container → PostgreSQL."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from e2e.conftest import API, BASE_URL, ApiUser, find_action, wait_for

Login = Callable[[str], ApiUser]


# --------------------------------------------------------------------------- platform / edge


def test_health_and_public_config_through_proxy() -> None:
    live = httpx.get(f"{API}/health/live")
    ready = httpx.get(f"{API}/health/ready")
    assert live.status_code == 200
    assert ready.json() == {"status": "ok", "version": ready.json()["version"], "checks": {"database": "ok"}}
    cfg = httpx.get(f"{API}/auth/config").json()
    assert cfg["demo_login_enabled"] is True
    assert cfg["entra_login_enabled"] is False


def test_spa_routes_and_static_assets_are_served_by_nginx() -> None:
    for path in ("/", "/login", "/inbox", "/approvals", "/actions/00000000-0000-0000-0000-000000000000"):
        resp = httpx.get(f"{BASE_URL}{path}")
        assert resp.status_code == 200, path
        assert '<div id="root">' in resp.text
        assert "no-cache" in resp.headers.get("cache-control", "")
    index = httpx.get(f"{BASE_URL}/").text
    asset = index.split('src="')[1].split('"')[0]
    resp = httpx.get(f"{BASE_URL}{asset}")
    assert resp.status_code == 200
    assert "immutable" in resp.headers.get("cache-control", "")


def test_security_headers_on_ui_and_api() -> None:
    ui = httpx.get(f"{BASE_URL}/login")
    csp = ui.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert ui.headers.get("x-content-type-options") == "nosniff"
    api = httpx.get(f"{API}/health/live")
    for header in ("x-content-type-options", "x-frame-options", "referrer-policy", "x-request-id"):
        assert header in api.headers, header


def test_correlation_id_is_propagated_through_proxy() -> None:
    resp = httpx.get(f"{API}/emails", headers={"X-Request-ID": "e2e-correlation-0001"})
    assert resp.status_code == 401
    assert resp.headers["x-request-id"] == "e2e-correlation-0001"
    assert resp.json()["error"]["correlation_id"] == "e2e-correlation-0001"


def test_authentication_and_csrf_are_enforced(login: Login) -> None:
    for path in ("/emails", "/actions", "/workflows", "/audit-events", "/settings", "/dashboard/summary"):
        assert httpx.get(f"{API}{path}").status_code == 401, path
    member = login("member")
    resp = member.client.post("/emails/sync", json={})  # no CSRF header
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


def test_oversized_request_is_rejected(login: Login) -> None:
    member = login("member")
    resp = member.client.post(
        "/emails/sync",
        content=b"x" * (2 * 1024 * 1024),
        headers={**member.headers, "content-type": "application/json"},
    )
    assert resp.status_code == 413


def test_logout_revokes_the_session(fresh_login: Login) -> None:
    member = fresh_login("member")
    assert member.get("/auth/me").status_code == 200
    assert member.post("/auth/logout").status_code == 204
    assert member.get("/auth/me").status_code == 401


# --------------------------------------------------------------------------- business flows


def test_incident_is_analyzed_approved_and_executed_exactly_once(login: Login) -> None:
    member = login("member")
    detail = member.analyze(member.email_by_subject("URGENT: Production login failures"))
    assert detail["run"]["status"] == "pending_approval"
    analysis = detail["analysis"]
    assert analysis["category"] == "incident"
    assert analysis["priority"] in ("high", "critical")
    assert analysis["extraction"]["summary"] == "Investigate production login failures after deployment"
    wi = find_action(detail, "create_azure_devops_work_item")
    assert wi["payload"]["work_item_type"] == "Bug"
    assert wi["payload"]["assigned_to"] is None

    denied = member.execute(wi)
    assert denied.status_code == 403
    assert member.approve(wi, "e2e approval").status_code == 200
    assert member.execute(wi).status_code == 202
    done = member.wait_action(wi["id"], {"completed", "failed", "uncertain"})
    assert done["status"] == "completed", done
    assert done["external_url"].startswith("mock://azure-devops/")
    assert [e["status"] for e in done["executions"]] == ["succeeded"]
    assert member.execute(wi).status_code == 409  # never twice

    history = [e["event_type"] for e in member.get(f"/actions/{wi['id']}/history").json()]
    for expected in ("permission_denied", "action_approved", "execution_started", "external_write"):
        assert expected in history, history


def test_rejected_proposal_can_never_execute(login: Login) -> None:
    member = login("member")
    detail = member.analyze(member.email_by_subject("Bug: CSV export drops rows"))
    wi = find_action(detail, "create_azure_devops_work_item")
    resp = member.post(f"/actions/{wi['id']}/reject", json={"reason": "Known issue, tracked elsewhere"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert member.approve(wi).status_code == 409
    assert member.execute(wi).status_code == 409


def test_edit_invalidates_approval_and_requires_reapproval(login: Login) -> None:
    member = login("member")
    detail = member.analyze(member.email_by_subject("Request: VPN access"))
    action = next(a for a in detail["actions"] if a["status"] == "pending_approval")
    assert member.approve(action).status_code == 200
    payload = dict(action["payload"])
    key = "title" if "title" in payload else "subject" if "subject" in payload else "reason"
    payload[key] = f"{payload[key]} (edited in e2e)"
    edited = member.patch(f"/actions/{action['id']}", json={"expected_version": action["version"], "payload": payload})
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["version"] == action["version"] + 1
    assert body["status"] == "pending_approval"
    assert body["current_approval"] is None
    assert member.execute(action).status_code in (403, 409)  # old version/hash is no longer executable
    assert member.approve(body).status_code == 200
    assert member.execute(body).status_code == 202
    assert member.wait_action(action["id"], {"completed"})["version"] == body["version"]


def test_approver_can_approve_a_members_action_but_a_viewer_cannot_see_it(login: Login) -> None:
    member = login("member")
    detail = member.analyze(member.email_by_subject("Feature request: saved filters"))
    action = next(a for a in detail["actions"] if a["status"] == "pending_approval")
    viewer = login("viewer")
    assert viewer.get(f"/actions/{action['id']}").status_code == 404
    assert viewer.post("/emails/sync", json={}).status_code == 403
    approver = login("approver")
    resp = approver.approve(action, "approved by the team lead")
    assert resp.status_code == 200
    assert resp.json()["current_approval"]["decided_by_name"] == approver.me["user"]["display_name"]


def test_follow_up_email_creates_an_internal_task(login: Login) -> None:
    member = login("member")
    detail = member.analyze(member.email_by_subject("Re: Invoice sync between billing and ERP"))
    task = find_action(detail, "create_follow_up_task")
    assert member.approve(task).status_code == 200
    assert member.execute(task).status_code == 202
    member.wait_action(task["id"], {"completed"})
    tasks = member.get("/tasks").json()["items"]
    assert any(t["action_id"] == task["id"] for t in tasks)


def test_newsletter_and_phishing_lead_to_no_action_proposals(login: Login) -> None:
    member = login("member")
    for subject in ("Elarxsys Engineering Newsletter", "Action required: your mailbox storage is full"):
        detail = member.analyze(member.email_by_subject(subject))
        assert detail["analysis"]["category"] in ("information_only", "spam_or_irrelevant"), subject
        assert [a["action_type"] for a in detail["actions"]] == ["mark_no_action"], subject


def test_prompt_injection_is_flagged_and_cannot_force_actions(login: Login) -> None:
    member = login("member")
    detail = member.analyze(member.email_by_subject("Bug: search returns an error"))
    analysis = detail["analysis"]
    assert analysis["injection_flags"], "injection was not flagged"
    assert analysis["priority"] != "critical"
    for action in detail["actions"]:
        assert action["status"] == "pending_approval"  # nothing auto-approved
        assert action["payload"].get("assigned_to") is None
        assert any("injection" in w.lower() for w in action["validation_warnings"])


def test_html_email_is_stored_as_plain_text(login: Login) -> None:
    member = login("member")
    email = member.email_by_subject("Access request: add three analysts")
    body = member.get(f"/emails/{email['id']}").json()["body_text"]
    assert "<" not in body
    assert "analysts" in body.lower()


def test_follow_up_in_same_thread_is_flagged_as_possible_duplicate(login: Login) -> None:
    member = login("member")
    member.analyze(member.email_by_subject("URGENT: Production login failures"))
    detail = member.analyze(member.email_by_subject("RE: URGENT: Production login failures"))
    work_item = find_action(detail, "create_azure_devops_work_item")
    assert any("Possible duplicate" in w for w in work_item["validation_warnings"])


def test_cancelled_workflow_cancels_open_proposals(login: Login) -> None:
    member = login("member")
    detail = member.analyze(member.email_by_subject("Thanks for the quick help"))
    run_id = detail["run"]["id"]
    resp = member.post(f"/workflows/{run_id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    actions = member.get(f"/workflows/{run_id}/actions").json()
    assert all(a["status"] == "cancelled" for a in actions)


def test_dashboard_and_audit_reflect_activity(login: Login) -> None:
    member = login("member")
    member.analyze(member.email_by_subject("URGENT: Production login failures"))
    summary = member.get("/dashboard/summary").json()
    assert summary["emails_total"] >= 10
    assert summary["emails_analyzed"] >= 1
    assert summary["recent_activity"]
    admin = login("admin")
    events = admin.get("/audit-events", params={"limit": 100}).json()
    assert events["total"] >= 1
    types = {e["event_type"] for e in events["items"]}
    assert "login" in types


def test_settings_are_admin_only_and_versioned(login: Login) -> None:
    member = login("member")
    current = member.get("/settings").json()
    body = {"expected_version": current["version"], "settings": current["settings"]}
    assert member.put("/settings", json=body).status_code == 403
    admin = login("admin")
    resp = admin.put("/settings", json=body)
    assert resp.status_code == 200
    assert resp.json()["version"] == current["version"] + 1
    assert admin.put("/settings", json=body).status_code == 409


def test_azure_devops_mock_integration_endpoints(login: Login) -> None:
    member = login("member")
    test = member.get("/integrations/azure-devops/test").json()
    assert test["ok"] is True
    projects = [p["name"] for p in member.get("/integrations/azure-devops/projects").json()["items"]]
    assert "NUVEXA-Demo" in projects
    types = member.get("/integrations/azure-devops/projects/NUVEXA-Demo/work-item-types").json()["items"]
    assert any(t["name"] == "Bug" for t in types)
    valid = member.post(
        "/integrations/azure-devops/validate",
        json={"payload": {"project": "NUVEXA-Demo", "work_item_type": "Bug", "title": "e2e", "priority": 2}},
    ).json()
    assert valid["valid"] is True
    invalid = member.post(
        "/integrations/azure-devops/validate",
        json={"payload": {"project": "Unknown-Project", "work_item_type": "Bug", "title": "e2e", "priority": 2}},
    ).json()
    assert invalid["valid"] is False


def test_workflow_timeline_records_llm_and_tool_calls(login: Login) -> None:
    member = login("member")
    detail = member.analyze(member.email_by_subject("URGENT: Production login failures"))
    events = member.get(f"/workflows/{detail['run']['id']}/events").json()
    kinds = {(e["step"], e["event_type"]) for e in events}
    for step in ("classify", "extract", "plan"):
        assert (step, "llm_call") in kinds
    tool_calls = [e for e in events if e["event_type"] == "tool_call"]
    assert tool_calls
    for event in tool_calls:
        assert "body" not in event["details"].get("arguments", {})  # sanitized: no free-text email content


def test_wait_helper_reports_timeouts() -> None:
    with pytest.raises(AssertionError, match="never"):
        wait_for(lambda: None, "never", timeout=0.2, interval=0.05)
