"""Acceptance flow: sync → analyze → propose → edit/approve → execute → audit (demo mode, no network)."""

from __future__ import annotations

from tests.integration.conftest import Harness, find_action, sync_and_analyze

SPEC_SUBJECT = "Production login failures"


async def test_health_and_auth_config(harness: Harness) -> None:
    assert (await harness.get("/health/live")).json()["status"] == "ok"
    assert (await harness.get("/health/ready")).json()["checks"]["database"] == "ok"
    cfg = (await harness.get("/auth/config")).json()
    assert cfg["demo_login_enabled"] is True


async def test_spec_example_flow_creates_exactly_one_work_item(harness: Harness) -> None:
    headers = await harness.login("member")
    detail = await sync_and_analyze(harness, headers, SPEC_SUBJECT)

    assert detail["run"]["status"] == "pending_approval"
    analysis = detail["analysis"]
    assert analysis["category"] == "incident"
    assert analysis["priority"] in ("high", "critical")
    assert 0 <= analysis["confidence_score"] <= 1
    assert analysis["confidence_method"] == "heuristic-v1"
    work_item = find_action(detail, "create_azure_devops_work_item")
    assert work_item["status"] == "pending_approval"
    assert work_item["payload"]["work_item_type"] == "Bug"
    assert work_item["payload"]["project"] == "NUVEXA-Demo"
    assert work_item["payload"]["assigned_to"] is None  # never invented
    assert work_item["permissions"]["can_approve"] is True
    assert work_item["permissions"]["can_execute"] is False

    # Timeline records LLM calls and proposal tool calls (sanitized).
    event_types = {e["event_type"] for e in detail["events"]}
    assert {"llm_call", "tool_call", "step_completed"} <= event_types

    # Executing before approval is refused.
    action_id = work_item["id"]
    body = {"version": work_item["version"], "payload_hash": work_item["payload_hash"]}
    resp = await harness.post(f"/actions/{action_id}/execute", headers, json=body)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "permission_denied"

    # Approve the exact version/hash shown to the reviewer.
    resp = await harness.post(f"/actions/{action_id}/approve", headers, json={**body, "comment": "Looks right"})
    assert resp.status_code == 200, resp.text
    approved = resp.json()
    assert approved["status"] == "approved"
    assert approved["current_approval"]["payload_hash"] == work_item["payload_hash"]

    resp = await harness.post(f"/actions/{action_id}/execute", headers, json=body)
    assert resp.status_code == 202, resp.text
    await harness.drain()

    action = (await harness.get(f"/actions/{action_id}")).json()
    assert action["status"] == "completed", action
    assert action["external_id"]
    assert action["external_url"].startswith("mock://")
    assert len(action["executions"]) == 1
    assert action["executions"][0]["status"] == "succeeded"

    # Executing again must not create a second work item.
    resp = await harness.post(f"/actions/{action_id}/execute", headers, json=body)
    assert resp.status_code == 409
    client = harness.ado.clients[-1]
    assert len(client.items()) == 1

    history = (await harness.get(f"/actions/{action_id}/history")).json()
    kinds = [e["event_type"] for e in history]
    for expected in ("action_approved", "execute_requested", "execution_started", "external_write"):
        assert expected in kinds, kinds

    summary = (await harness.get("/dashboard/summary")).json()
    assert summary["completed_actions"] >= 1
    assert summary["avg_approval_wait_seconds"] is not None
    assert summary["emails_total"] >= 10


async def test_edit_invalidates_approval_and_requires_reapproval(harness: Harness) -> None:
    headers = await harness.login("member")
    detail = await sync_and_analyze(harness, headers, SPEC_SUBJECT)
    wi = find_action(detail, "create_azure_devops_work_item")
    body = {"version": wi["version"], "payload_hash": wi["payload_hash"]}
    assert (await harness.post(f"/actions/{wi['id']}/approve", headers, json=body)).status_code == 200

    edited_payload = {**wi["payload"], "title": "Investigate login failures on the customer portal (edited)"}
    resp = await harness.client.patch(
        f"/api/v1/actions/{wi['id']}",
        headers=headers,
        json={"expected_version": wi["version"], "payload": edited_payload},
    )
    assert resp.status_code == 200, resp.text
    edited = resp.json()
    assert edited["version"] == wi["version"] + 1
    assert edited["payload_hash"] != wi["payload_hash"]
    assert edited["status"] == "pending_approval"
    assert edited["current_approval"] is None
    assert edited["approvals"][0]["invalidated_at"] is not None

    # The old version/hash can no longer be approved or executed.
    assert (await harness.post(f"/actions/{wi['id']}/approve", headers, json=body)).status_code == 409
    assert (await harness.post(f"/actions/{wi['id']}/execute", headers, json=body)).status_code == 409
    new_body = {"version": edited["version"], "payload_hash": edited["payload_hash"]}
    assert (await harness.post(f"/actions/{wi['id']}/approve", headers, json=new_body)).status_code == 200


async def test_reject_then_execute_is_refused(harness: Harness) -> None:
    headers = await harness.login("member")
    detail = await sync_and_analyze(harness, headers, SPEC_SUBJECT)
    wi = find_action(detail, "create_azure_devops_work_item")
    resp = await harness.post(f"/actions/{wi['id']}/reject", headers, json={"reason": "Duplicate of INC-1"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    body = {"version": wi["version"], "payload_hash": wi["payload_hash"]}
    assert (await harness.post(f"/actions/{wi['id']}/execute", headers, json=body)).status_code == 409


async def test_resync_does_not_duplicate_messages(harness: Harness) -> None:
    headers = await harness.login("member")
    await harness.post("/emails/sync", headers, json={})
    await harness.drain()
    first = (await harness.get("/emails", params={"limit": 100})).json()["total"]
    for _ in range(3):
        await harness.post("/emails/sync", headers, json={})
        await harness.drain()
    after = (await harness.get("/emails", params={"limit": 100})).json()
    ids = [e["id"] for e in after["items"]]
    assert len(ids) == len(set(ids))
    assert after["total"] >= first


async def test_analyze_twice_returns_existing_run(harness: Harness) -> None:
    headers = await harness.login("member")
    await harness.post("/emails/sync", headers, json={})
    await harness.drain()
    email = (await harness.get("/emails")).json()["items"][0]
    first = await harness.post(f"/emails/{email['id']}/analyze", headers)
    second = await harness.post(f"/emails/{email['id']}/analyze", headers)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["workflow_run"]["id"] == first.json()["workflow_run"]["id"]


async def test_reanalysis_supersedes_and_never_duplicates_completed_work(harness: Harness) -> None:
    headers = await harness.login("member")
    detail = await sync_and_analyze(harness, headers, SPEC_SUBJECT)
    run_id = detail["run"]["id"]
    wi = find_action(detail, "create_azure_devops_work_item")
    body = {"version": wi["version"], "payload_hash": wi["payload_hash"]}
    await harness.post(f"/actions/{wi['id']}/approve", headers, json=body)
    await harness.post(f"/actions/{wi['id']}/execute", headers, json=body)
    await harness.drain()

    detail = (await harness.get(f"/workflows/{run_id}")).json()
    if detail["run"]["status"] in ("pending_approval", "approved", "failed"):
        resp = await harness.post(f"/workflows/{run_id}/reanalyze", headers)
        assert resp.status_code == 202, resp.text
        await harness.drain()
        detail = (await harness.get(f"/workflows/{run_id}")).json()
        live_work_items = [
            a
            for a in detail["actions"]
            if a["action_type"] == "create_azure_devops_work_item" and a["status"] not in ("superseded",)
        ]
        # Only the completed item remains: the planner skips completed work, and the idempotency key
        # blocks any equivalent proposal that slips through.
        assert len(live_work_items) == 1
        assert live_work_items[0]["status"] == "completed"
    assert len(harness.ado.clients[-1].items()) == 1


async def test_follow_up_and_reply_actions_execute_locally(harness: Harness) -> None:
    headers = await harness.login("member")
    detail = await sync_and_analyze(harness, headers, SPEC_SUBJECT)
    reply = next((a for a in detail["actions"] if a["action_type"] == "propose_email_reply"), None)
    if reply is None:
        return
    assert reply["payload"]["to"] == [detail["actions"][0]["payload"]["source_email"]["sender"]]
    body = {"version": reply["version"], "payload_hash": reply["payload_hash"]}
    await harness.post(f"/actions/{reply['id']}/approve", headers, json=body)
    await harness.post(f"/actions/{reply['id']}/execute", headers, json=body)
    await harness.drain()
    drafts = (await harness.get("/reply-drafts")).json()
    assert drafts["total"] == 1
    assert (await harness.get(f"/actions/{reply['id']}")).json()["status"] == "completed"


async def test_follow_up_in_same_thread_warns_about_possible_duplicate(harness: Harness) -> None:
    headers = await harness.login("member")
    await sync_and_analyze(harness, headers, "URGENT: Production login failures")
    await harness.post("/emails/sync", headers, json={})  # the demo mailbox delivers the follow-up next
    await harness.drain()
    emails = (await harness.get("/emails", params={"q": "RE: URGENT"})).json()["items"]
    assert emails, "follow-up email not delivered"
    resp = await harness.post(f"/emails/{emails[0]['id']}/analyze", headers)
    await harness.drain()
    detail = (await harness.get(f"/workflows/{resp.json()['workflow_run']['id']}")).json()
    work_items = [a for a in detail["actions"] if a["action_type"] == "create_azure_devops_work_item"]
    assert work_items
    assert any("Possible duplicate" in w for w in work_items[0]["validation_warnings"])
