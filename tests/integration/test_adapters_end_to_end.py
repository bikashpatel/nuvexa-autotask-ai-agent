"""Real REST adapters wired through the API and worker, with HTTP mocked by respx (no mocks of our classes).

(a) MAIL_MODE=graph: Entra connect (fake MSAL app) → POST /emails/sync → worker → EmailSyncService →
    GraphMailboxProvider → respx-mocked Microsoft Graph delta pages.
(b) AZURE_DEVOPS_MODE=pat: approve + execute → worker → ExecutionService → AzureDevOpsRestClient →
    respx-mocked Azure DevOps REST 7.1 (WIQL duplicate lookup, JSON-patch create, validateOnly, metadata).
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.db.models import CredentialRecord, EmailSyncCheckpoint, Job
from app.domain.enums import JobStatus, JobType
from app.integrations.azure_devops.mapping import idempotency_tag
from app.integrations.http import RetryPolicy
from app.integrations.microsoft_graph.demo_mailbox import load_demo_fixture
from app.schemas.api import AdoValidateOut, IntegrationsOut
from tests.integration.conftest import (
    API,
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
from tests.integration.fakes import FakeEntra
from tests.integration.test_auth_flows import ENTRA, entra_login

GRAPH = "https://graph.microsoft.com/v1.0"
DELTA_PATH = "/v1.0/me/mailFolders/inbox/messages/delta"
ADO = "https://dev.azure.com/contoso"
PAT = "fake-pat-value-0123456789"
INCIDENT_ID = load_demo_fixture()["messages"][0]["id"]


@pytest.fixture(autouse=True)
def _instant_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff delays → 0 s so retry paths run instantly (the retry *count* logic is unchanged)."""
    monkeypatch.setattr(RetryPolicy, "compute_delay", lambda self, retry_index, retry_after=None: 0.0)


# ---------------------------------------------------------------------------------------------------
# (a) Microsoft Graph delta sync
# ---------------------------------------------------------------------------------------------------


def graph_message(msg_id: str, subject: str, *, change: str = "v1", minutes_ago: int = 5) -> dict[str, Any]:
    from datetime import timedelta

    from app.core.timeutil import utcnow

    received = (utcnow() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": msg_id,
        "internetMessageId": f"<{msg_id}@contoso.example>",
        "conversationId": f"conv-{msg_id}",
        "changeKey": f"{msg_id}-{change}",
        "subject": subject,
        "from": {"emailAddress": {"name": "Pat Reporter", "address": "Pat.Reporter@contoso.example"}},
        "toRecipients": [{"emailAddress": {"name": "Casey", "address": "casey.entra@contoso.example"}}],
        "ccRecipients": [],
        "receivedDateTime": received,
        "sentDateTime": received,
        "body": {
            "contentType": "html",
            "content": f"<p>Hello,</p><p>{subject}. Production login fails with error 500 since this morning.</p>",
        },
        "bodyPreview": subject,
        "hasAttachments": False,
        "importance": "high",
        "isRead": False,
        "webLink": f"https://outlook.office.com/mail/{msg_id}",
    }


@dataclass
class FakeGraph:
    """Microsoft Graph delta endpoint: queued responses per cursor ("initial", "skip:<t>", "delta:<t>")."""

    script: dict[str, list[httpx.Response | Exception]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    evil_calls: list[httpx.Request] = field(default_factory=list)

    def evil(self, request: httpx.Request) -> httpx.Response:
        self.evil_calls.append(request)
        return httpx.Response(200, json={"value": []})

    def queue(self, key: str, *responses: httpx.Response | Exception) -> None:
        self.script.setdefault(key, []).extend(responses)

    def page(
        self, key: str, value: list[dict[str, Any]], *, next_skip: str | None = None, delta: str | None = None
    ) -> None:
        body: dict[str, Any] = {"value": value}
        if next_skip:
            body["@odata.nextLink"] = f"{GRAPH}/me/mailFolders/inbox/messages/delta?$skiptoken={next_skip}"
        if delta:
            body["@odata.deltaLink"] = f"{GRAPH}/me/mailFolders/inbox/messages/delta?$deltatoken={delta}"
        self.queue(key, httpx.Response(200, json=body))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = request.url.params
        key = (
            f"skip:{params['$skiptoken']}"
            if "$skiptoken" in params
            else f"delta:{params['$deltatoken']}"
            if "$deltatoken" in params
            else "initial"
        )
        queued = self.script.get(key)
        assert queued, f"unexpected Graph request {key}: {request.url}"
        item = queued.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def keys(self) -> list[str]:
        out = []
        for r in self.requests:
            p = r.url.params
            out.append(
                f"skip:{p['$skiptoken']}"
                if "$skiptoken" in p
                else f"delta:{p['$deltatoken']}"
                if "$deltatoken" in p
                else "initial"
            )
        return out


@pytest.fixture
def graph() -> Iterator[FakeGraph]:
    fake = FakeGraph()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get(url__regex=rf"^{GRAPH}/me/mailFolders/inbox/messages/delta").mock(side_effect=fake)
        router.route(host="evil.example").mock(side_effect=fake.evil)
        yield fake


@pytest.fixture
def entra(monkeypatch: pytest.MonkeyPatch) -> FakeEntra:
    return FakeEntra().install(monkeypatch)


async def _graph_user(make_harness: HarnessFactory, entra: FakeEntra) -> tuple[Harness, Persona, str]:
    """An Entra-signed-in user with a connected Microsoft 365 mailbox (MAIL_MODE=graph)."""
    h = await make_harness(**ENTRA, mail_mode="graph")
    client = h.tracked_client()
    await entra_login(client, entra)
    me = (await client.get(f"{API}/auth/me")).json()["user"]
    csrf = next(c.value for c in client.cookies.jar if c.name == h.settings.csrf_cookie_name)
    p = Persona("graph-user", client, {"X-CSRF-Token": csrf}, uuid.UUID(me["id"]), uuid.UUID(me["tenant_id"]))
    state_resp = await client.get(f"{API}/integrations/microsoft/connect")
    assert state_resp.status_code == 302
    resp = await client.get(f"{API}/auth/callback", params={"code": "c", "state": entra.flows_started[-1]["state"]})
    assert resp.status_code == 302
    [conn] = (await p.get("/integrations")).json()["microsoft"]["connections"]
    return h, p, conn["id"]


async def _sync(h: Harness, p: Persona, account_id: str) -> None:
    resp = await p.post("/emails/sync", json={"connected_account_id": account_id})
    assert resp.status_code == 202, resp.text
    await h.drain()


async def _subjects(p: Persona) -> dict[str, str]:
    items = (await p.get("/emails", params={"limit": 100})).json()["items"]
    return {e["id"]: e["subject"] for e in items}


async def _connection(p: Persona) -> dict[str, Any]:
    out = IntegrationsOut.model_validate((await p.get("/integrations")).json())
    return out.microsoft.connections[0].model_dump(mode="json")


async def test_graph_delta_sync_end_to_end(make_harness: HarnessFactory, entra: FakeEntra, graph: FakeGraph) -> None:
    h, p, account_id = await _graph_user(make_harness, entra)
    m1, m2, m3 = (graph_message(f"AAMk-{i}", f"Incident {i}: login outage") for i in (1, 2, 3))
    graph.page("initial", [m1, m2], next_skip="page2")
    graph.page("skip:page2", [m3, {"id": "AAMk-never-seen", "@removed": {"reason": "deleted"}}], delta="round1")

    await _sync(h, p, account_id)
    assert graph.keys() == ["initial", "skip:page2"]
    first = graph.requests[0]
    assert first.headers["authorization"] == f"Bearer {entra.access_token}"
    prefer = first.headers["prefer"]
    assert 'IdType="ImmutableId"' in prefer
    assert 'outlook.body-content-type="text"' in prefer
    assert "odata.maxpagesize=50" in prefer
    assert "$select=" in str(first.url)
    assert "receivedDateTime%20ge%20" in str(first.url)
    assert "attachments" not in first.url.params["$select"]  # attachment content is never requested
    assert {c["scopes"][0] for c in entra.silent_calls} == {"Mail.Read"}
    subjects = await _subjects(p)
    assert sorted(subjects.values()) == [
        "Incident 1: login outage",
        "Incident 2: login outage",
        "Incident 3: login outage",
    ]
    email_ids = {s: i for i, s in subjects.items()}
    detail = (await p.get(f"/emails/{email_ids['Incident 2: login outage']}")).json()
    assert detail["body_text"].startswith("Hello,")  # HTML converted to text
    assert detail["sender_address"] == "pat.reporter@contoso.example"
    assert detail["web_link"] == "https://outlook.office.com/mail/AAMk-2"
    sync = (await _connection(p))["sync"]
    assert (sync["last_status"], sync["messages_ingested_total"]) == ("succeeded", 3)
    async with h.db.session() as s:
        checkpoint = (await s.execute(select(EmailSyncCheckpoint))).scalar_one()
    assert checkpoint.delta_link == f"{GRAPH}/me/mailFolders/inbox/messages/delta?$deltatoken=round1"

    # Analyze one email, then the next round changes it and removes another.
    run = await analyze(h, p, email_ids["Incident 2: login outage"])
    assert run["run"]["status"] == "pending_approval"
    changed = graph_message("AAMk-2", "Incident 2: login outage (update)", change="v2")
    graph.page("delta:round1", [{"id": "AAMk-1", "@removed": {"reason": "deleted"}}, changed], delta="round2")
    await _sync(h, p, account_id)
    assert graph.keys()[-1] == "delta:round1"
    assert str(graph.requests[-1].url) == checkpoint.delta_link  # the stored cursor, verbatim
    subjects = await _subjects(p)
    assert sorted(subjects.values()) == ["Incident 2: login outage (update)", "Incident 3: login outage"]
    updated = (await p.get(f"/emails/{email_ids['Incident 2: login outage']}")).json()
    assert updated["content_changed_since_analysis"] is True

    # An expired delta token (410) triggers a full resync without duplicates.
    graph.queue("delta:round2", httpx.Response(410, json={"error": {"code": "SyncStateNotFound", "message": "x"}}))
    graph.page("initial", [changed, m3, graph_message("AAMk-4", "Incident 4: new")], delta="round3")
    await _sync(h, p, account_id)
    assert graph.keys()[-2:] == ["delta:round2", "initial"]
    subjects = await _subjects(p)
    assert len(subjects) == 3
    assert "Incident 4: new" in subjects.values()
    assert (await _connection(p))["sync"]["messages_ingested_total"] == 4


async def test_graph_401_marks_the_connection_for_reauth(
    make_harness: HarnessFactory, entra: FakeEntra, graph: FakeGraph
) -> None:
    """Regression (fixed in EmailSyncService.sync_account): every Graph IntegrationError crashed the failure path
    with MissingGreenlet (expired ORM object read after rollback), so nothing was recorded and the job retried."""
    h, p, account_id = await _graph_user(make_harness, entra)
    graph.queue("initial", httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}}))
    await _sync(h, p, account_id)
    conn = await _connection(p)
    assert conn["status"] == "needs_reauth"
    assert "Reconnect" in conn["last_error"]
    assert conn["sync"]["last_status"] == "failed"
    assert "401" in conn["sync"]["last_error"]
    async with h.db.session() as s:
        [job] = (await s.execute(select(Job).where(Job.job_type == JobType.SYNC_MAILBOX))).scalars().all()
    assert job.status is JobStatus.FAILED  # authorization failures are not retried
    [failure] = await h.audit_events(event_type="mailbox_sync_failed")
    assert failure.details["category"] == "authorization"
    assert_error(await p.post("/emails/sync", json={"connected_account_id": account_id}), 409, "conflict")
    assert len(graph.requests) == 1


async def test_graph_token_refresh_is_persisted_encrypted(
    make_harness: HarnessFactory, entra: FakeEntra, graph: FakeGraph
) -> None:
    h, p, account_id = await _graph_user(make_harness, entra)
    async with h.db.session() as s:
        before = (await s.execute(select(CredentialRecord))).scalar_one().encrypted_payload
    entra.refresh_on_silent = True
    graph.page("initial", [graph_message("AAMk-1", "Hello")], delta="r1")
    await _sync(h, p, account_id)
    assert graph.requests[0].headers["authorization"] == f"Bearer {entra.access_token}-refreshed"
    async with h.db.session() as s:
        after = (await s.execute(select(CredentialRecord))).scalar_one().encrypted_payload
    assert after != before
    assert b"refreshed" not in after


async def test_graph_silent_token_failure_never_calls_graph(
    make_harness: HarnessFactory, entra: FakeEntra, graph: FakeGraph
) -> None:
    h, p, account_id = await _graph_user(make_harness, entra)
    entra.silent_error = "invalid_grant"
    await _sync(h, p, account_id)
    assert graph.requests == []  # no token → no Graph call
    assert entra.silent_calls[-1]["account"] == entra.identity.home_account_id
    assert await _subjects(p) == {}


async def test_graph_silent_token_failure_marks_the_connection_for_reauth(
    make_harness: HarnessFactory, entra: FakeEntra, graph: FakeGraph
) -> None:
    h, p, account_id = await _graph_user(make_harness, entra)
    entra.silent_error = "invalid_grant"
    await _sync(h, p, account_id)
    conn = await _connection(p)
    assert conn["status"] == "needs_reauth"
    assert "AADSTS50173" in conn["sync"]["last_error"]


async def test_graph_throttling_is_retried(make_harness: HarnessFactory, entra: FakeEntra, graph: FakeGraph) -> None:
    h, p, account_id = await _graph_user(make_harness, entra)
    graph.queue("initial", httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"code": "TooMany"}}))
    graph.queue("initial", httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}}))
    graph.page("initial", [graph_message("AAMk-1", "Hello")], delta="r1")
    await _sync(h, p, account_id)
    assert graph.keys() == ["initial", "initial", "initial"]
    assert len(await _subjects(p)) == 1
    assert (await _connection(p))["sync"]["last_status"] == "succeeded"


async def _off_origin_round(h: Harness, p: Persona, account_id: str, graph: FakeGraph) -> None:
    graph.page("initial", [graph_message("AAMk-1", "Hello")], delta="r1")
    await _sync(h, p, account_id)
    evil = {"value": [graph_message("AAMk-9", "Injected")], "@odata.nextLink": "https://evil.example/v1.0/me/x?a=1"}
    graph.queue("delta:r1", httpx.Response(200, json=evil))
    await _sync(h, p, account_id)


async def test_graph_off_origin_paging_links_are_never_followed(
    make_harness: HarnessFactory, entra: FakeEntra, graph: FakeGraph
) -> None:
    h, p, account_id = await _graph_user(make_harness, entra)
    await _off_origin_round(h, p, account_id, graph)
    assert graph.evil_calls == []
    assert list((await _subjects(p)).values()) == ["Hello"]  # nothing from the poisoned round was applied
    async with h.db.session() as s:
        checkpoint = (await s.execute(select(EmailSyncCheckpoint))).scalar_one()
    assert checkpoint.delta_link == f"{GRAPH}/me/mailFolders/inbox/messages/delta?$deltatoken=r1"  # not advanced


async def test_graph_off_origin_link_failure_is_recorded(
    make_harness: HarnessFactory, entra: FakeEntra, graph: FakeGraph
) -> None:
    h, p, account_id = await _graph_user(make_harness, entra)
    await _off_origin_round(h, p, account_id, graph)
    conn = await _connection(p)
    assert conn["status"] == "active"  # not an authorization problem
    assert conn["sync"]["last_status"] == "failed"
    assert "outside the configured Graph endpoint" in conn["sync"]["last_error"]


# ---------------------------------------------------------------------------------------------------
# (b) Azure DevOps REST (PAT)
# ---------------------------------------------------------------------------------------------------


@dataclass
class FakeAzureDevOps:
    """Stateful Azure DevOps Services REST stand-in for one organization/project."""

    create_script: list[str] = field(default_factory=list)  # "ok" | "timeout_after_create" | "400" | "503" | "500"
    wiql_script: list[str] = field(default_factory=list)  # "ok" | "500"
    items: dict[int, dict[str, Any]] = field(default_factory=dict)
    next_id: int = 4242
    calls: dict[str, list[httpx.Request]] = field(default_factory=dict)

    def _log(self, name: str, request: httpx.Request) -> None:
        self.calls.setdefault(name, []).append(request)

    def count(self, name: str) -> int:
        return len(self.calls.get(name, []))

    def wiql(self, request: httpx.Request) -> httpx.Response:
        self._log("wiql", request)
        mode = self.wiql_script.pop(0) if self.wiql_script else "ok"
        if mode == "500":
            return httpx.Response(500, json={"message": "WIQL unavailable"})
        query = json.loads(request.content)["query"]
        refs = [{"id": i} for i, item in self.items.items() if f"'{item['tag']}'" in query]
        return httpx.Response(200, json={"queryType": "flat", "workItems": refs})

    def read(self, request: httpx.Request) -> httpx.Response:
        self._log("read", request)
        ids = [int(i) for i in request.url.params["ids"].split(",")]
        return httpx.Response(
            200, json={"count": len(ids), "value": [self._resource(i) for i in ids if i in self.items]}
        )

    def create(self, request: httpx.Request, wit: str) -> httpx.Response:
        self._log("validate" if request.url.params.get("validateOnly") == "true" else "create", request)
        ops = json.loads(request.content)
        fields = {op["path"].removeprefix("/fields/"): op["value"] for op in ops}
        if request.url.params.get("validateOnly") == "true":
            if not str(fields.get("System.Title", "")).strip() or "Reject" in str(fields.get("System.Title")):
                return httpx.Response(
                    400,
                    json={
                        "message": "TF401320: Rule Error for field Title.",
                        "customProperties": {"FieldReferenceName": "System.Title"},
                    },
                )
            return httpx.Response(200, json={"id": 0, "fields": fields})
        mode = self.create_script.pop(0) if self.create_script else "ok"
        if mode == "400":
            return httpx.Response(400, json={"message": "TF401320: Rule Error for field Severity."})
        if mode == "503":
            return httpx.Response(503, headers={"Retry-After": "1"}, json={"message": "unavailable"})
        if mode == "500":
            return httpx.Response(500, json={"message": "boom"})
        tag = next(t.strip() for t in fields["System.Tags"].split(";") if t.strip().startswith("nuvexa-"))
        item_id = self.next_id
        self.next_id += 1
        self.items[item_id] = {"fields": {**fields, "System.State": "New", "System.WorkItemType": wit}, "tag": tag}
        if mode == "timeout_after_create":
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(200, json=self._resource(item_id))

    def _resource(self, item_id: int) -> dict[str, Any]:
        fields = {**self.items[item_id]["fields"], "System.TeamProject": "NUVEXA-Demo", "System.Id": item_id}
        return {"id": item_id, "rev": 1, "fields": fields, "url": f"{ADO}/_apis/wit/workItems/{item_id}"}

    def mount(self, router: respx.MockRouter) -> None:
        router.post(url__regex=rf"^{ADO}/NUVEXA-Demo/_apis/wit/wiql\?").mock(side_effect=self.wiql)
        router.get(url__regex=rf"^{ADO}/NUVEXA-Demo/_apis/wit/workitems\?").mock(side_effect=self.read)
        router.post(url__regex=rf"^{ADO}/NUVEXA-Demo/_apis/wit/workitems/\$(?P<wit>[^?]+)\?").mock(
            side_effect=self.create
        )


def _basic(pat: str) -> str:
    return "Basic " + base64.b64encode(f":{pat}".encode()).decode()


@pytest.fixture
def ado_server() -> Iterator[tuple[FakeAzureDevOps, respx.MockRouter]]:
    fake = FakeAzureDevOps()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        fake.mount(router)
        yield fake, router


async def _pat_world(make_harness: HarnessFactory) -> tuple[Harness, Persona, dict[str, Any]]:
    h = await make_harness(
        azure_devops_mode="pat", azure_devops_organization="contoso", azure_devops_pat=PAT, ado_factory="real"
    )
    member = await h.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await h.drain()
    email_id = await email_id_by_provider_id(h, member.user_id, INCIDENT_ID)
    detail = await analyze(h, member, email_id)
    return h, member, find_action(detail, "create_azure_devops_work_item")


async def test_pat_execution_creates_exactly_one_work_item(
    make_harness: HarnessFactory, ado_server: tuple[FakeAzureDevOps, respx.MockRouter]
) -> None:
    ado, _ = ado_server
    h, member, wi = await _pat_world(make_harness)
    integrations = (await member.get("/integrations")).json()["azure_devops"]
    assert (integrations["mode"], integrations["organization"], integrations["configured"]) == ("pat", "contoso", True)
    tag = idempotency_tag(wi["idempotency_key"])

    done = await approve_and_execute(h, member, wi)
    assert done["status"] == "completed", done
    assert (done["external_id"], done["external_url"]) == ("4242", f"{ADO}/NUVEXA-Demo/_workitems/edit/4242")
    assert (ado.count("wiql"), ado.count("create"), ado.count("read")) == (1, 1, 0)

    [lookup] = ado.calls["wiql"]
    assert lookup.url.params["api-version"] == "7.1"
    assert lookup.url.params["$top"] == "20"
    query = json.loads(lookup.content)["query"]
    assert f"[System.Tags] CONTAINS '{tag}'" in query
    assert "[System.TeamProject] = 'NUVEXA-Demo'" in query

    [create] = ado.calls["create"]
    assert create.url.path == "/contoso/NUVEXA-Demo/_apis/wit/workitems/$Bug"
    assert create.url.params["api-version"] == "7.1"
    assert "validateOnly" not in create.url.params
    assert create.headers["authorization"] == _basic(PAT)
    assert create.headers["content-type"] == "application/json-patch+json"
    assert create.headers["x-tfs-fedauthredirect"] == "Suppress"
    ops = json.loads(create.content)
    assert {op["op"] for op in ops} == {"add"}
    fields = {op["path"]: op["value"] for op in ops}
    assert fields["/fields/System.Title"] == wi["payload"]["title"]
    assert fields["/fields/Microsoft.VSTS.Common.Priority"] == wi["payload"]["priority"]
    assert "Approved in NUVEXA before creation" in fields["/fields/System.Description"]
    assert "<script" not in fields["/fields/System.Description"].lower()
    assert tag in [t.strip() for t in fields["/fields/System.Tags"].split(";")]
    assert "/fields/System.AssignedTo" not in fields

    [execution] = done["executions"]
    assert (execution["status"], execution["provider_status_code"], execution["retry_count"]) == ("succeeded", 200, 0)
    # A second execute request is refused without touching Azure DevOps.
    assert_error(await member.post(f"/actions/{wi['id']}/execute", json=review_body(wi)), 409)
    assert ado.count("create") == 1


async def test_pat_ambiguous_timeout_recovers_via_tag_lookup_without_resending(
    make_harness: HarnessFactory, ado_server: tuple[FakeAzureDevOps, respx.MockRouter]
) -> None:
    ado, _ = ado_server
    ado.create_script = ["timeout_after_create"]
    h, member, wi = await _pat_world(make_harness)
    done = await approve_and_execute(h, member, wi)
    assert done["status"] == "completed", done
    assert done["external_id"] == "4242"
    assert ado.count("create") == 1  # never re-sent
    assert ado.count("wiql") == 2  # before the create and after the ambiguous failure
    [read] = ado.calls["read"]
    assert read.url.params["ids"] == "4242"
    assert read.url.params["errorPolicy"] == "omit"
    assert len(ado.items) == 1


async def test_pat_validation_error_fails_without_retry(
    make_harness: HarnessFactory, ado_server: tuple[FakeAzureDevOps, respx.MockRouter]
) -> None:
    ado, _ = ado_server
    ado.create_script = ["400"]
    h, member, wi = await _pat_world(make_harness)
    failed = await approve_and_execute(h, member, wi)
    assert failed["status"] == "failed"
    [execution] = failed["executions"]
    assert (execution["status"], execution["error_category"], execution["retry_count"]) == ("failed", "validation", 0)
    assert execution["provider_status_code"] == 400
    assert "TF401320" in execution["error_message"]
    assert ado.count("create") == 1
    async with h.db.session() as s:
        jobs = (await s.execute(select(Job).where(Job.job_type == JobType.RESUME_WORKFLOW))).scalars().all()
    assert all(j.attempts == 1 for j in jobs)
    # Once the cause is fixed the still-valid approval may be retried (nothing was created).
    resp = await member.post(f"/actions/{wi['id']}/execute", json=review_body(wi))
    assert resp.status_code == 202
    await h.drain()
    assert (await member.get(f"/actions/{wi['id']}")).json()["status"] == "completed"
    assert len(ado.items) == 1


async def test_pat_transient_errors_are_retried_safely(
    make_harness: HarnessFactory, ado_server: tuple[FakeAzureDevOps, respx.MockRouter]
) -> None:
    ado, _ = ado_server
    ado.create_script = ["503", "500", "ok"]  # refused → retried; 500 may have applied → lookup → absent → retried
    h, member, wi = await _pat_world(make_harness)
    done = await approve_and_execute(h, member, wi)
    assert done["status"] == "completed"
    assert done["executions"][0]["retry_count"] == 2
    assert ado.count("create") == 3
    assert ado.count("wiql") == 2  # pre-create + after the ambiguous 500
    assert len(ado.items) == 1


async def test_pat_uncertain_outcome_is_reconciled_through_the_api(
    make_harness: HarnessFactory, ado_server: tuple[FakeAzureDevOps, respx.MockRouter]
) -> None:
    ado, _ = ado_server
    ado.create_script = ["timeout_after_create"]
    ado.wiql_script = ["ok", "500", "500", "500", "500"]  # the post-timeout lookup keeps failing
    h, member, wi = await _pat_world(make_harness)
    uncertain = await approve_and_execute(h, member, wi)
    assert uncertain["status"] == "uncertain"
    assert uncertain["executions"][0]["error_category"] == "ambiguous"
    assert uncertain["permissions"]["can_reconcile"] is True
    resp = await member.post(f"/actions/{wi['id']}/reconcile")
    assert resp.status_code == 202
    await h.drain()
    done = (await member.get(f"/actions/{wi['id']}")).json()
    assert done["status"] == "completed"
    assert done["external_id"] == "4242"
    assert done["executions"][-1]["deduplicated"] is True
    assert ado.count("create") == 1
    [event] = await h.audit_events(event_type="execution_reconciled")
    assert event.details["found"] is True


async def test_pat_integration_endpoints(
    make_harness: HarnessFactory, ado_server: tuple[FakeAzureDevOps, respx.MockRouter]
) -> None:
    ado, router = ado_server
    project_calls: list[httpx.Request] = []

    def projects(request: httpx.Request) -> httpx.Response:
        project_calls.append(request)
        token = request.url.params.get("continuationToken")
        if request.url.params.get("$top") == "1":
            return httpx.Response(200, json={"count": 1, "value": [{"id": "p1", "name": "NUVEXA-Demo"}]})
        if token is None:
            return httpx.Response(
                200,
                headers={"x-ms-continuationtoken": "page-2"},
                json={"value": [{"id": "p1", "name": "NUVEXA-Demo", "state": "wellFormed"}]},
            )
        return httpx.Response(200, json={"value": [{"id": "p2", "name": "Customer Portal"}, {"bogus": True}]})

    router.get(url__regex=rf"^{ADO}/_apis/projects\?").mock(side_effect=projects)
    router.get(url__regex=rf"^{ADO}/NUVEXA-Demo/_apis/wit/workitemtypes\?").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {"name": "Bug", "referenceName": "Microsoft.VSTS.WorkItemTypes.Bug", "description": "A defect"},
                    {"name": "Old Type", "isDisabled": True},
                ]
            },
        )
    )
    h = await make_harness(azure_devops_mode="pat", azure_devops_organization="contoso", azure_devops_pat=PAT)
    member = await h.persona("member")

    check = (await member.get("/integrations/azure-devops/test")).json()
    assert check["ok"] is True
    assert (check["mode"], check["organization"]) == ("pat", "contoso")
    assert check["message"] == "Connected to Azure DevOps organization 'contoso'."
    assert project_calls[-1].headers["authorization"] == _basic(PAT)
    assert project_calls[-1].url.params["api-version"] == "7.1"

    listed = (await member.get("/integrations/azure-devops/projects")).json()["items"]
    assert [(p["id"], p["name"], p["url"]) for p in listed] == [
        ("p1", "NUVEXA-Demo", f"{ADO}/NUVEXA-Demo"),
        ("p2", "Customer Portal", f"{ADO}/Customer%20Portal"),
    ]
    assert project_calls[-1].url.params["continuationToken"] == "page-2"

    types = (await member.get("/integrations/azure-devops/projects/NUVEXA-Demo/work-item-types")).json()
    assert types["items"] == [
        {
            "name": "Bug",
            "reference_name": "Microsoft.VSTS.WorkItemTypes.Bug",
            "description": "A defect",
            "is_disabled": False,
        },
        {"name": "Old Type", "reference_name": None, "description": None, "is_disabled": True},
    ]
    err = assert_error(
        await member.get("/integrations/azure-devops/projects/%20NUVEXA-Demo/work-item-types"), 502, "integration_error"
    )
    assert "Invalid Azure DevOps project name" in err["message"]

    payload = {"project": "NUVEXA-Demo", "work_item_type": "Bug", "title": "Login fails", "priority": 2}
    ok = AdoValidateOut.model_validate(
        (await member.post("/integrations/azure-devops/validate", json={"payload": payload})).json()
    )
    assert (ok.valid, ok.server_validated) == (True, True)
    [validate] = ado.calls["validate"]
    assert validate.url.params["validateOnly"] == "true"
    assert validate.url.params["api-version"] == "7.1"
    assert validate.headers["content-type"] == "application/json-patch+json"
    rejected = AdoValidateOut.model_validate(
        (
            await member.post(
                "/integrations/azure-devops/validate", json={"payload": {**payload, "title": "Reject me"}}
            )
        ).json()
    )
    assert (rejected.valid, rejected.server_validated) == (False, True)
    assert rejected.server_issues[0].field == "System.Title"
    assert "TF401320" in rejected.server_issues[0].message
    assert ado.count("create") == 0  # validation never creates anything


@pytest.mark.parametrize(
    ("response", "message_part"),
    [
        (httpx.Response(401, json={"message": "unauthorized"}), "Authentication failed"),
        (
            httpx.Response(203, text="<html>sign in</html>", headers={"content-type": "text/html"}),
            "Authentication failed",
        ),
        (httpx.Response(403, json={"message": "nope"}), "not permitted"),
        (httpx.Response(404, json={"message": "no org"}), "not found"),
    ],
)
async def test_pat_connection_test_reports_failures(
    make_harness: HarnessFactory,
    ado_server: tuple[FakeAzureDevOps, respx.MockRouter],
    response: httpx.Response,
    message_part: str,
) -> None:
    _, router = ado_server
    router.get(url__regex=rf"^{ADO}/_apis/projects\?").mock(return_value=response)
    h = await make_harness(azure_devops_mode="pat", azure_devops_organization="contoso", azure_devops_pat=PAT)
    member = await h.persona("member")
    check = (await member.get("/integrations/azure-devops/test")).json()
    assert check["ok"] is False
    assert message_part in check["message"]
    assert PAT not in json.dumps(check)
    err = assert_error(await member.get("/integrations/azure-devops/projects"), 502, "integration_error")
    assert PAT not in err["message"]


async def test_pat_mode_never_falls_back_to_the_mock(make_harness: HarnessFactory) -> None:
    with respx.mock(assert_all_mocked=True) as router:
        route = router.route().mock(side_effect=httpx.ConnectError("no network in tests"))
        h = await make_harness(azure_devops_mode="pat", azure_devops_organization="contoso", azure_devops_pat=PAT)
        member = await h.persona("member")
        check = (await member.get("/integrations/azure-devops/test")).json()
    assert check["ok"] is False
    assert "could not be reached" in check["message"]
    assert route.call_count == 4  # 1 + 3 retries of an idempotent GET
