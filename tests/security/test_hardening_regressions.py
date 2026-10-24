"""Regression tests for defects found while hardening (token hygiene, failure recording, authorization)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.core.errors import IntegrationNotConfigured
from app.core.security import SecretBox
from app.db.models import ConnectedAccount, EmailSyncCheckpoint, User
from app.domain.enums import ConnectionProvider, SyncStatus
from app.integrations.microsoft_graph.token_store import CredentialStore
from tests.integration.conftest import HarnessFactory, sync_and_analyze
from tests.integration.fakes import FakeEntra, FakeIdentity
from tests.integration.test_auth_flows import ENTRA, _connect, entra_login
from tests.security.conftest import Harness

API = "/api/v1"


async def test_connecting_a_different_microsoft_account_does_not_inherit_the_old_tokens(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    # Arrange: the user connects mailbox A, then replaces it with mailbox B (same Entra tenant)
    h = await make_harness(**ENTRA, mail_mode="graph")
    await entra_login(h.client, entra)
    entra.access_token = "mailbox-a-access-token"
    await _connect(h, h.client, entra, "/integrations/microsoft/connect")
    entra.identity = FakeIdentity(oid=str(uuid.uuid4()), email="second.mailbox@contoso.example", name="Second")
    entra.access_token = "mailbox-b-access-token"

    # Act
    await _connect(h, h.client, entra, "/integrations/microsoft/connect")

    # Assert: the stored (encrypted) cache for the new connection holds only mailbox B's tokens
    async with h.db.session() as s:
        account = (
            await s.execute(
                select(ConnectedAccount).where(ConnectedAccount.provider == ConnectionProvider.MICROSOFT_GRAPH)
            )
        ).scalar_one()
        serialized = await CredentialStore(s, SecretBox.from_settings(h.settings)).load(
            account.id, tenant_id=account.tenant_id
        )
    assert account.external_account_id == entra.identity.home_account_id
    assert serialized is not None
    assert "mailbox-b-access-token" in serialized
    assert "mailbox-a-access-token" not in serialized


async def test_unexpected_sync_errors_are_recorded_and_do_not_leave_the_checkpoint_running(
    harness: Harness, monkeypatch: Any
) -> None:
    # Arrange: the mailbox provider crashes with a non-integration error
    class ExplodingProvider:
        async def fetch_changes(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("parser exploded")

    monkeypatch.setattr("app.services.email_sync_service.build_mailbox_provider", lambda *a, **kw: ExplodingProvider())
    headers = await harness.login("member")

    # Act
    assert (await harness.post("/emails/sync", headers, json={})).status_code == 202
    await harness.drain()

    # Assert
    async with harness.db.session() as s:
        checkpoint = (await s.execute(select(EmailSyncCheckpoint))).scalar_one()
    assert checkpoint.last_status is SyncStatus.FAILED
    assert checkpoint.last_error
    assert "parser exploded" not in checkpoint.last_error  # internal details are not surfaced
    assert await harness.audit_events(event_type="mailbox_sync_failed")


async def test_missing_integration_configuration_is_recorded_as_a_configuration_failure(harness: Harness) -> None:
    # Arrange: an approved work item, but the Azure DevOps client cannot be built
    async def not_configured(*_: Any) -> Any:
        raise IntegrationNotConfigured("Connect your Azure DevOps account first.")

    harness.execution.ado_client_factory = not_configured
    headers = await harness.login("member")
    detail = await sync_and_analyze(harness, headers, "URGENT: Production login failures")
    action = next(a for a in detail["actions"] if a["action_type"] == "create_azure_devops_work_item")
    body = {"version": action["version"], "payload_hash": action["payload_hash"]}
    await harness.post(f"/actions/{action['id']}/approve", headers, json=body)

    # Act
    await harness.post(f"/actions/{action['id']}/execute", headers, json=body)
    await harness.drain()

    # Assert
    result = (await harness.get(f"/actions/{action['id']}")).json()
    assert result["status"] == "failed"
    assert result["executions"][-1]["error_category"] == "configuration"


async def test_viewers_cannot_use_the_azure_devops_service_credentials(harness: Harness) -> None:
    await harness.login("viewer")
    for method, path, body in (
        ("GET", "/integrations/azure-devops/test", None),
        ("GET", "/integrations/azure-devops/projects", None),
        ("GET", "/integrations/azure-devops/projects/NUVEXA-Demo/work-item-types", None),
        ("POST", "/integrations/azure-devops/validate", {"payload": {"project": "NUVEXA-Demo", "title": "x"}}),
    ):
        headers = {"X-CSRF-Token": harness.client.cookies.get(harness.settings.csrf_cookie_name) or ""}
        resp = await harness.client.request(method, f"{API}{path}", json=body, headers=headers)
        assert resp.status_code == 403, (path, resp.text)
    await harness.login("member")
    assert (await harness.get("/integrations/azure-devops/test")).status_code == 200


async def test_a_malformed_resume_request_does_not_wedge_the_workflow(harness: Harness) -> None:
    # Arrange: a workflow waiting for review
    headers = await harness.login("member")
    detail = await sync_and_analyze(harness, headers, "URGENT: Production login failures")
    run_id = uuid.UUID(detail["run"]["id"])
    async with harness.db.session() as s:
        owner = await s.get(User, uuid.UUID(detail["run"]["owner_user_id"]))
        assert owner is not None
        tenant_id = owner.tenant_id

    # Act: a corrupted command (not producible by the API, but possible from a damaged job row)
    await harness.engine.resume(run_id, tenant_id, {"type": "execute", "action_id": "not-a-uuid"})

    # Assert: the graph is still waiting for review and accepts normal commands
    snapshot = await harness.engine.graph.aget_state({"configurable": {"thread_id": str(run_id)}})
    assert snapshot.interrupts
    await harness.engine.resume(run_id, tenant_id, {"type": "refresh"})
    assert (await harness.get(f"/workflows/{run_id}")).json()["run"]["status"] == "pending_approval"
