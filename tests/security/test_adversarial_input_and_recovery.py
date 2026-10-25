"""Adversarial QA: hostile/malformed input, persistence failures, interruption and recovery.

Every test is offline and deterministic: SQLite, respx for HTTP, the heuristic/scripted agent and the mock
Azure DevOps client. Database outages are simulated by raising SQLAlchemy `OperationalError`/`DataError`
at the exact point a real driver would; process deaths by a `BaseException` that escapes every
`except Exception` (as in tests/integration/test_resilience.py).

Tests marked `# DEFECT:` assert the correct behaviour and fail until the defect is fixed (test-first).
Related existing coverage is cited in each section so nothing here duplicates it.
"""

from __future__ import annotations

import json
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import literal_column, select, update
from sqlalchemy.exc import DataError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentOutputError
from app.agents.injection import RULES, screen_for_injection
from app.agents.sanitize import SCREEN_LIMIT_CHARS
from app.core.errors import IntegrationError
from app.core.text import html_to_text
from app.core.timeutil import utcnow
from app.db.models import (
    ActionApproval,
    ActionExecution,
    AuditEvent,
    EmailMessage,
    EmailSyncCheckpoint,
    Job,
    ProposedAction,
    WorkflowRun,
)
from app.db.session import Database
from app.domain.enums import JobStatus, JobType, SyncStatus
from app.integrations.http import no_sleep
from app.integrations.microsoft_graph.demo_mailbox import load_demo_fixture
from app.integrations.microsoft_graph.mail_client import GraphMailboxProvider
from app.integrations.microsoft_graph.normalize import normalize_graph_message
from app.services import email_sync_service
from app.services.email_sync_service import EmailSyncService, apply_normalized
from app.services.execution_service import ExecutionService
from app.services.workflow_service import WorkflowService
from app.workers import queue
from app.workflows import checkpointer as cp
from tests.conftest import make_settings
from tests.integration.conftest import (
    API,
    Harness,
    HarnessFactory,
    Persona,
    approve_and_execute,
    email_id_by_provider_id,
    review_body,
)
from tests.integration.fakes import ScriptedAgent
from tests.integration.test_resilience import SimulatedCrash, restart_worker
from tests.unit.integrations.conftest import StaticTokenProvider
from tests.unit.workflows.test_checkpointer import _FakePool, fake_postgres  # noqa: F401 — re-used fixture

GRAPH = "https://graph.microsoft.com/v1.0"
DELTA = f"{GRAPH}/me/mailFolders/inbox/messages/delta"
FIXTURE = load_demo_fixture()["messages"]
INCIDENT_ID = FIXTURE[0]["id"]
FEATURE_ID = next(m["id"] for m in FIXTURE if m["subject"].startswith("Feature request"))


# ---------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------


def graph_message(mid: str, subject: str = "Status update", **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": mid,
        "subject": subject,
        "changeKey": f"{mid}-v1",
        "receivedDateTime": "2026-10-20T08:30:00Z",
        "from": {"emailAddress": {"name": "Ada Example", "address": "ada@example.com"}},
        "body": {"contentType": "text", "content": f"Body of {mid}"},
    }
    data.update(extra)
    return data


def graph_page(value: list[Any], delta_token: str = "round-1") -> httpx.Response:
    # Serialized with ensure_ascii so lone surrogates travel as JSON escapes, exactly like a hostile server's.
    body = json.dumps({"value": value, "@odata.deltaLink": f"{DELTA}?$deltatoken={delta_token}"}).encode()
    return httpx.Response(200, content=body, headers={"content-type": "application/json"})


@contextmanager
def graph_serving(*responses: httpx.Response) -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        yield router.get(url__startswith=DELTA).mock(side_effect=list(responses))


def use_graph_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync the persona's mailbox through the real Graph delta client + normalizer (HTTP served by respx)."""

    def build(account: Any, *, session: AsyncSession, settings: Any) -> GraphMailboxProvider:
        return GraphMailboxProvider(StaticTokenProvider(), settings, sleep=no_sleep)

    monkeypatch.setattr(email_sync_service, "build_mailbox_provider", build)


def strings_in(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from strings_in(k)
            yield from strings_in(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from strings_in(v)


def has_control_chars(value: str) -> bool:
    return any(unicodedata.category(c)[0] == "C" and c not in "\n\t" for c in value)


async def jobs_of(h: Harness, job_type: JobType) -> list[Job]:
    async with h.db.session() as s:
        stmt = select(Job).where(Job.job_type == job_type).order_by(Job.created_at)
        return list((await s.execute(stmt)).scalars().all())


async def emails(h: Harness) -> list[EmailMessage]:
    async with h.db.session() as s:
        return list((await s.execute(select(EmailMessage).order_by(EmailMessage.received_at))).scalars().all())


async def fast_forward_backoff(h: Harness) -> None:
    async with h.db.session() as s:
        await s.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(run_after=utcnow()))
        await s.commit()


async def expire_leases(h: Harness) -> int:
    async with h.db.session() as s:
        await s.execute(
            update(Job)
            .where(Job.status == JobStatus.RUNNING)
            .values(locked_at=utcnow() - timedelta(seconds=h.settings.job_lease_seconds + 5))
        )
        await s.commit()
        return await queue.recover_expired_leases(s, h.settings.job_lease_seconds)


async def synced_member(h: Harness) -> Persona:
    member = await h.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await h.drain()
    return member


async def analyzed(h: Harness, member: Persona, provider_id: str, **headers: str) -> str:
    email_id = await email_id_by_provider_id(h, member.user_id, provider_id)
    resp = await member.client.post(f"{API}/emails/{email_id}/analyze", headers={**member.headers, **headers})
    assert resp.status_code == 202, resp.text
    await h.drain()
    return str(resp.json()["workflow_run"]["id"])


def operational_error(statement: str = "COMMIT") -> OperationalError:
    return OperationalError(statement, {}, sqlite3.OperationalError("database is locked"))


def error_client(h: Harness, persona: Persona) -> httpx.AsyncClient:
    """Like the persona's client, but unhandled server errors come back as HTTP 500 responses."""
    app = h.client._transport.app  # type: ignore[attr-defined]
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test", cookies=persona.client.cookies)


async def raw_post(persona: Persona, path: str, content: Any) -> httpx.Response:
    headers = {**persona.headers, "content-type": "application/json"}
    return await persona.client.post(f"{API}{path}", content=content, headers=headers)


def assert_envelope(resp: httpx.Response, *statuses: int) -> dict[str, Any]:
    assert resp.status_code in statuses, f"{resp.status_code}: {resp.text[:500]}"
    error = resp.json()["error"]
    assert set(error) == {"code", "message", "correlation_id", "details"}
    assert error["correlation_id"]
    assert error["correlation_id"] == resp.headers.get("x-request-id")
    return dict(error)


# ===================================================================================================
# 1. Hostile and malformed input
#
# Existing coverage (not repeated): normalizer field limits and arbitrary-field fuzzing
# (tests/unit/integrations/test_demo_mailbox_normalize_extra.py::test_field_limits,
# ::test_normalizer_never_raises_for_arbitrary_fields), off-origin/unparseable next/delta links and malformed
# pages (tests/unit/integrations/test_graph_mail_client_extra.py::test_stored_links_off_the_graph_origin_force_a_
# resync, ::test_paging_links_off_origin_are_refused, ::test_malformed_pages_are_transient,
# ::test_page_shapes_skips_and_span_attributes, ::test_page_limit_truncates_and_resumes), request size limits
# (tests/unit/api/test_middleware.py, tests/integration/test_security.py::test_request_size_limit).
# ===================================================================================================


async def test_hostile_graph_delta_page_is_ingested_with_bounded_fields(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_graph_mailbox(monkeypatch)
    member = await harness.persona("member")
    hostile_subject = ("Re:\u202e\u200b\x00\x07 INVOICE " + "\u00dc" * 40 + "\r\n") * 200
    big = graph_message(
        "m-big",
        hostile_subject,
        body={"contentType": "text", "content": "Lorem ipsum dolor sit amet.\x00\x1b " * 80_000},  # ~2.5 MB
        toRecipients=[{"emailAddress": {"name": f"R{i}\x00", "address": f"r{i}@example.com"}} for i in range(5_000)],
        ccRecipients="not-a-list",
        attachments=[
            {
                "name": "A" * 10_000 + "\u202egpj.exe",
                "contentType": 'application/x-msdownload; name="x"\r\nX-Injected: 1' * 20,
                "size": -5,
                "isInline": "yes",
                "contentBytes": "TVqQAAMAAAAEAAAA",
            }
            for _ in range(300)
        ],
        importance="URGENT!!!",
        isRead="maybe",
        receivedDateTime="yesterday-ish",
        webLink="javascript:alert(document.cookie)",
    )
    deep_html = graph_message(
        "m-html",
        body={
            "contentType": "HTML",
            "content": "<div>" * 20_000 + "<script>steal()</script>Deep &amp; nested &#0; &bogus;" + "</div>" * 20_000,
        },
    )
    garbage: list[Any] = [
        42,
        "string",
        None,
        [],
        {"id": 7},
        {"id": ""},
        {"id": "i" * 600},  # longer than any Graph id
        graph_message("m-surrogate-\ud800"),  # lone surrogate in the identity: unrepresentable, skipped
        {"@removed": {"reason": "deleted"}},
    ]
    started = time.perf_counter()
    with graph_serving(graph_page([big, deep_html, *garbage, graph_message("m-ok")])):
        assert (await member.post("/emails/sync", json={})).status_code == 202
        await harness.drain()
    elapsed = time.perf_counter() - started

    [job] = await jobs_of(harness, JobType.SYNC_MAILBOX)
    assert job.status is JobStatus.SUCCEEDED, job.last_error
    stored = {e.provider_message_id: e for e in await emails(harness)}
    assert sorted(stored) == ["m-big", "m-html", "m-ok"]  # malformed entries skipped, the round still completed
    e = stored["m-big"]
    assert len(e.to_recipients) == 100
    assert e.cc_recipients == []
    assert len(e.attachments) == 50
    assert all(len(a["name"]) <= 255 and len(a["content_type"] or "") <= 255 for a in e.attachments)
    assert all(a["size"] is None and a["is_inline"] is False for a in e.attachments)
    assert not any("contentBytes" in a or "content_bytes" in a for a in e.attachments)  # content never kept
    assert (e.importance, e.is_read, e.web_link) == (None, False, None)
    assert e.received_at is not None
    assert not has_control_chars(e.subject)
    assert "\u202e" not in e.subject
    assert not has_control_chars(e.body_text or "")
    assert not any(has_control_chars(s) for s in strings_in(e.to_recipients))
    html_body = stored["m-html"].body_text or ""
    assert "steal()" not in html_body
    assert "Deep & nested" in html_body
    assert elapsed < 20, f"hostile page took {elapsed:.1f}s"
    # The multi-megabyte message is still served by the API (clean JSON, no 500).
    detail = await member.get(f"/emails/{e.id}")
    assert detail.status_code == 200
    assert detail.json()["attachments"][0]["name"].startswith("AAAA")


# DEFECT: identifiers/addresses are stored without removing NUL or re-clipping after lowercasing; PostgreSQL
# rejects NUL in text/varchar/jsonb and over-long varchar values, so such a message aborts the sync batch.
def test_normalized_graph_message_fits_postgres_column_constraints() -> None:
    hostile = graph_message(
        "m-pg",
        **{
            # "İ".lower() is two code points: clipped to 320 *before* lowercasing, the stored value is longer.
            "from": {"emailAddress": {"name": "Eve", "address": "\u0130" * 300 + "@example.com"}},
            "toRecipients": [{"emailAddress": {"name": "To", "address": "to\x00@example.com"}}],
            "internetMessageId": "<abc\x00@example.com>",
            "conversationId": "conv\x00",
            "changeKey": "ck\x00",
            "attachments": [{"name": "a.txt", "contentType": "text/plain\x00"}],
            "webLink": "https://outlook.office.com/mail/\x00id",
        },
    )
    email = EmailMessage(provider_message_id="m-pg", content_hash="")
    apply_normalized(email, normalize_graph_message(hostile))
    problems: list[str] = []
    for column in EmailMessage.__table__.columns:
        value = getattr(email, column.key, None)
        limit = getattr(column.type, "length", None)
        if isinstance(value, str) and limit is not None and len(value) > limit:
            problems.append(f"{column.key}: {len(value)} characters > varchar({limit})")
        if any("\x00" in s for s in strings_in(value)):
            problems.append(f"{column.key}: contains NUL")
    assert problems == []


def _poison_upsert(monkeypatch: pytest.MonkeyPatch, poison_id: str) -> list[str]:
    """The database rejects one message (as PostgreSQL does for NUL bytes or an over-long varchar)."""
    seen: list[str] = []
    real = EmailSyncService._upsert

    async def upsert(self: EmailSyncService, session: AsyncSession, account: Any, msg: Any, result: Any) -> None:
        seen.append(msg.provider_message_id)
        if msg.provider_message_id == poison_id:
            raise DataError(
                "INSERT INTO email_messages ...", {}, ValueError("PostgreSQL text fields cannot contain NUL bytes")
            )
        await real(self, session, account, msg, result)

    monkeypatch.setattr(EmailSyncService, "_upsert", upsert)
    return seen


# DEFECT: a database error while storing a synced batch (after the fetch) bypasses the F-18 failure handling:
# the checkpoint stays RUNNING, last_error is empty and no mailbox_sync_failed audit event is written.
async def test_database_error_while_storing_synced_mail_is_recorded_not_left_running(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _poison_upsert(monkeypatch, FIXTURE[2]["id"])
    member = await harness.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    await harness.drain()
    async with harness.db.session() as s:
        checkpoint = (await s.execute(select(EmailSyncCheckpoint))).scalar_one()
        failures = (
            (await s.execute(select(AuditEvent).where(AuditEvent.event_type == "mailbox_sync_failed"))).scalars().all()
        )
    assert checkpoint.last_status is not SyncStatus.RUNNING, "nothing is running, but the UI says 'syncing'"
    assert checkpoint.last_error
    assert len(failures) == 1


# DEFECT: one message the database cannot store (poison message) blocks every other message of the round, on
# every retry, forever: the delta cursor never advances and the job ends DEAD.
async def test_one_message_rejected_by_the_database_does_not_block_the_rest_of_the_mailbox(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    poison = FIXTURE[2]["id"]
    _poison_upsert(monkeypatch, poison)
    member = await harness.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    for _ in range(4):  # first attempt + retries after backoff
        await harness.drain()
        await fast_forward_backoff(harness)
    stored = {e.provider_message_id for e in await emails(harness)}
    healthy = {m["id"] for m in FIXTURE[:10]} - {poison}  # the demo mailbox's first round is 10 messages
    assert healthy <= stored, f"{len(healthy - stored)} of {len(healthy)} healthy messages were never ingested"


# DEFECT: catastrophic backtracking in the `fake_role_tag` rule (`<{2,}…` with no anchor): screening is
# quadratic in the length of a run of "<" — ~7 s for 20k characters, ~10+ minutes at SCREEN_LIMIT_CHARS — and
# it runs synchronously (twice per node, in every analysis node) inside the worker's event loop.
def test_injection_screening_is_linear_on_runs_of_angle_brackets() -> None:
    started = time.perf_counter()
    screen_for_injection("<" * 10_000)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "make",
    [
        lambda n: " " * n,
        lambda n: ">" * n,
        lambda n: "#" * n,
        lambda n: "[" * n,
        lambda n: "\u200b" * n,
        lambda n: "a" * n,
        lambda n: ("A1b" * 40 + "-") * (n // 121),  # many base64-like runs without whitespace
        lambda n: "already approved " * (n // 17),  # context-window rule hit repeatedly
        lambda n: "call the tool " * (n // 14),
        lambda n: "ignore " * (n // 7),
        lambda n: "\n> " * (n // 3),
    ],
    ids=[
        "spaces",
        "gt",
        "hash",
        "brackets",
        "zero-width",
        "word",
        "base64-runs",
        "approved",
        "call",
        "ignore",
        "quotes",
    ],
)
def test_injection_screening_is_fast_on_other_adversarial_runs(make: Any) -> None:
    text = make(SCREEN_LIMIT_CHARS // 4)
    slow: list[str] = []
    for rule in RULES:
        started = time.perf_counter()
        rule.search(text)
        if time.perf_counter() - started > 0.5:
            slow.append(rule.name)
    started = time.perf_counter()
    screen_for_injection(text)
    assert slow == []
    assert time.perf_counter() - started < 2


@pytest.mark.parametrize(
    "markup",
    [
        "<div>" * 100_000 + "x" + "</div>" * 100_000,
        "<b" * 200_000,
        "<!--" + "a" * 1_000_000,
        "<script>" + "a" * 1_000_000,
        "&amp;" * 200_000 + "&#xD800;&#0;&#1114112;&#99999999999;",
        "<p>" + "<![CDATA[" * 50_000,
        "</" * 200_000,
    ],
    ids=["nested", "unclosed-tags", "unterminated-comment", "unterminated-script", "entities", "cdata", "end-tags"],
)
def test_html_to_text_is_bounded_on_hostile_markup(markup: str) -> None:
    started = time.perf_counter()
    text = html_to_text(markup)
    assert time.perf_counter() - started < 3
    assert not has_control_chars(text)
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in text)


async def _work_item(h: Harness, member: Persona) -> dict[str, Any]:
    run_id = await analyzed(h, member, FEATURE_ID)
    [action] = (await member.get(f"/workflows/{run_id}")).json()["actions"]
    return dict(action)


async def test_deeply_nested_and_malformed_json_bodies_get_clean_4xx_envelopes(harness: Harness) -> None:
    member = await harness.persona("member")
    nested = '{"connected_account_id": ' + "[" * 100_000 + "]" * 100_000 + "}"  # 200 kB, far past recursion limits
    for content in (
        nested.encode(),
        b'{"connected_account_id": "\xff\xfe not utf-8"}',
        b'{"connected_account_id": "\\ud800"}',
        b'{"connected_account_id": NaN}',
        b'{"connected_account_id": 1e999999}',
        b"[" + b"1," * 300_000 + b"1]",
    ):
        resp = await raw_post(member, "/emails/sync", content)
        error = assert_envelope(resp, 400, 422)
        assert len(resp.content) < 20_000, "the error must not echo the hostile body"
        assert "Traceback" not in error["message"]
    # Observation (not a defect): an unknown key is echoed in `details[].loc`, bounded by MAX_REQUEST_BYTES.
    resp = await raw_post(member, "/emails/sync", b'{"' + b"k" * 500_000 + b'": 1}')
    assert assert_envelope(resp, 422)["code"] == "validation_failed"
    assert len(resp.content) < 2 * harness.settings.max_request_bytes


async def test_hostile_action_edits_and_review_inputs_are_rejected_without_echo(harness: Harness) -> None:
    member = await synced_member(harness)
    action = await _work_item(harness, member)
    huge = {**action["payload"], "title": "T" * 900_000}
    deep: Any = "x"
    for _ in range(500):
        deep = {"d": deep}
    cases = [
        ("patch", f"/actions/{action['id']}", {"expected_version": action["version"], "payload": huge}),
        ("patch", f"/actions/{action['id']}", {"expected_version": action["version"], "payload": {"title": deep}}),
        ("patch", f"/actions/{action['id']}", {"expected_version": 0, "payload": {}}),
        ("patch", f"/actions/{action['id']}", {"expected_version": "1; DROP TABLE", "payload": []}),
        ("post", f"/actions/{action['id']}/approve", {"version": action["version"], "payload_hash": "z" * 64}),
        ("post", f"/actions/{action['id']}/approve", {**review_body(action), "comment": "c" * 100_000}),
        ("post", f"/actions/{action['id']}/reject", {"reason": ""}),
        ("post", f"/actions/{action['id']}/execute", {"version": -1, "payload_hash": action["payload_hash"]}),
    ]
    for method, path, body in cases:
        resp = await getattr(member, method)(path, json=body)
        assert_envelope(resp, 409, 422)
        assert "T" * 1_000 not in resp.text
        assert "c" * 1_000 not in resp.text
    lone_surrogate = await raw_post(member, f"/actions/{action['id']}/reject", b'{"reason": "\\ud800"}')
    assert_envelope(lone_surrogate, 422)
    for path in ("/workflows?status=bogus", "/workflows?limit=100000", "/emails?offset=-1", "/actions/not-a-uuid"):
        assert_envelope(await member.get(path), 404, 422)
    still = (await member.get(f"/actions/{action['id']}")).json()
    assert (still["status"], still["version"], still["payload_hash"]) == (
        "pending_approval",
        action["version"],
        action["payload_hash"],
    )


# DEFECT: a streamed (chunked) body over MAX_REQUEST_BYTES reaches FastAPI's body parser, which turns the
# middleware's _BodyTooLarge into "400 There was an error parsing the body" instead of the documented 413.
async def test_streamed_oversized_body_is_rejected_with_413(harness: Harness) -> None:
    member = await harness.persona("member")
    chunk = b" " * 65_536

    async def body() -> AsyncIterator[bytes]:
        yield b'{"connected_account_id": null'
        for _ in range(harness.settings.max_request_bytes // len(chunk) + 2):
            yield chunk
        yield b"}"

    resp = await raw_post(member, "/emails/sync", body())
    error = assert_envelope(resp, 413)
    assert error["code"] == "request_too_large"


# DEFECT: NUL characters in free-text request fields are stored verbatim. SQLite accepts them; PostgreSQL rejects
# them (text: "cannot contain NUL (0x00) bytes", jsonb: "unsupported Unicode escape sequence"), so on the
# production database these requests end in an unhandled DataError → HTTP 500 instead of a 4xx.
async def test_nul_characters_in_review_text_are_rejected_or_removed(harness: Harness) -> None:
    member = await synced_member(harness)
    action = await _work_item(harness, member)
    resp = await member.post(f"/actions/{action['id']}/reject", json={"reason": "duplicate\x00 of #12"})
    assert resp.status_code in (200, 422), resp.text
    async with harness.db.session() as s:
        reasons = (await s.execute(select(ActionApproval.reason))).scalars().all()
        details = (await s.execute(select(AuditEvent.details))).scalars().all()
    stored = [*reasons, *(s for d in details for s in strings_in(d))]
    assert not any("\x00" in (value or "") for value in stored)


# ===================================================================================================
# 2. Database failures, races, partially completed workflows
#
# Existing coverage (not repeated): crash mid-analysis + checkpoint resume, crash after the external write
# (lease expiry → reconcile), dead worker's sync job, lease budget, retries/backoff/dead letters
# (tests/integration/test_resilience.py); IntegrityError races in create_run/enqueue/claim/settings
# (tests/unit/services/test_workflow_service.py::test_create_run_integrity_race_returns_concurrent_run,
# tests/unit/workers/test_queue.py::test_dedupe_race_is_resolved_by_the_unique_index,
# ::test_claim_race_on_concurrency_key_returns_none, F-16); claim/heartbeat errors in the worker loop
# (tests/unit/workers/test_runner.py::test_run_forever_survives_claim_errors,
# ::test_heartbeat_refreshes_lease_and_survives_errors); plan re-run reuse
# (tests/unit/workflows/test_graph_review.py::test_plan_rerun_after_crash_reuses_its_actions).
# ===================================================================================================


# DEFECT: when the database fails while recording the outcome of a successful work-item create, the job is
# retried within the execution lease, the retry's Conflict ("already executing") is swallowed by the engine
# fallback and the job SUCCEEDS; afterwards no path recovers the action: reconcile accepts only UNCERTAIN,
# execute/cancel/re-analysis refuse EXECUTING. The action and run stay "executing" forever.
async def test_db_failure_after_a_successful_work_item_create_is_recoverable(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_finish = ExecutionService._finish
    failures = [operational_error("UPDATE action_executions SET status=?")]

    async def finish(self: ExecutionService, *args: Any) -> Any:
        if failures:
            raise failures.pop()  # the write happened; recording it fails (connection lost / lock timeout)
        return await real_finish(self, *args)

    monkeypatch.setattr(ExecutionService, "_finish", finish)
    member = await synced_member(harness)
    action = await _work_item(harness, member)
    executed = await approve_and_execute(harness, member, action)
    assert executed["status"] == "executing"
    assert len(harness.ado.clients[-1].items()) == 1  # the work item exists in Azure DevOps

    for _ in range(3):  # retries after backoff (seconds) — well inside the execution lease
        await fast_forward_backoff(harness)
        await harness.drain()
    # Time passes beyond the execution lease: the worker that wrote is certainly gone.
    async with harness.db.session() as s:
        await s.execute(
            update(ActionExecution).values(
                started_at=utcnow() - timedelta(seconds=harness.settings.job_lease_seconds + 60)
            )
        )
        await s.commit()
    await fast_forward_backoff(harness)
    await harness.drain()
    current = (await member.get(f"/actions/{action['id']}")).json()
    if current["status"] == "executing":
        resp = await member.post(f"/actions/{action['id']}/reconcile")
        assert resp.status_code == 202, f"no way to recover an orphaned 'executing' action: {resp.text}"
        await harness.drain()
        current = (await member.get(f"/actions/{action['id']}")).json()
    assert current["status"] == "completed"
    assert current["executions"][-1]["deduplicated"] is True
    assert sum(c.create_attempts for c in harness.ado.clients) == 1  # never re-sent


# DEFECT: a job row this worker version cannot decode (a JobType added by a newer release during a rolling deploy,
# or a damaged row) makes claim_next raise LookupError while loading it. The row stays QUEUED at the head of the
# queue, so every claim fails: run_once raises and run_forever logs job_claim_failed forever — no job runs at all.
async def test_an_undecodable_job_row_does_not_block_the_queue(harness: Harness) -> None:
    member = await harness.persona("member")
    async with harness.db.session() as s:
        future = await queue.enqueue(s, job_type=JobType.RETENTION_PURGE, payload={}, tenant_id=None)
        await s.commit()
        await s.execute(
            update(Job)
            .where(Job.id == future.id)
            .values(job_type=literal_column("'send_weekly_digest'"), run_after=utcnow() - timedelta(minutes=5))
        )
        await s.commit()
    assert (await member.post("/emails/sync", json={})).status_code == 202
    processed = await harness.drain()
    [sync_job] = await jobs_of(harness, JobType.SYNC_MAILBOX)
    assert sync_job.status is JobStatus.SUCCEEDED
    assert processed >= 1


async def _job_taken_over_after_lease_expiry(db: Database, lease_seconds: int) -> tuple[Job, Job]:
    """Worker A claims a job and stalls (blocked loop / DB outage: no heartbeats); B takes it over."""
    async with db.session() as s:
        await queue.enqueue(
            s, job_type=JobType.RESUME_WORKFLOW, payload={}, tenant_id=None, concurrency_key="run:r-fence"
        )
        await s.commit()
    async with db.session() as s:
        stale = await queue.claim_next(s, "worker-a")
    assert stale is not None
    async with db.session() as s:
        await s.execute(update(Job).values(locked_at=utcnow() - timedelta(seconds=lease_seconds + 5)))
        await s.commit()
        assert await queue.recover_expired_leases(s, lease_seconds) == 1
    async with db.session() as s:
        current = await queue.claim_next(s, "worker-b")
    assert current is not None
    assert current.id == stale.id
    return stale, current


# DEFECT: complete()/fail() are not fenced by the lease holder (`locked_by`/attempt): a stalled worker whose lease
# was recovered can finish or re-queue the job another worker is running, after which a third worker can claim it
# while B still runs it — two concurrent runs of one concurrency key (one workflow run), against queue.py's contract.
@pytest.mark.parametrize("outcome", ["complete", "fail"])
async def test_a_stale_worker_cannot_release_a_job_another_worker_now_holds(
    db: Database, settings: Any, outcome: str
) -> None:
    stale, _current = await _job_taken_over_after_lease_expiry(db, settings.job_lease_seconds)
    async with db.session() as s:
        if outcome == "complete":
            await queue.complete(s, stale.id, {"by": "worker-a"})
        else:
            await queue.fail(s, stale, "late failure from worker-a", retryable=True)
    async with db.session() as s:
        row = await s.get(Job, stale.id)
        assert row is not None
        assert (row.status, row.locked_by) == (JobStatus.RUNNING, "worker-b")
    async with db.session() as s:
        assert await queue.claim_next(s, "worker-c") is None, "the job would now run twice concurrently"


class _DiesAfterPlanCommit(InMemorySaver):
    """The plan node committed its proposals, then the process died before LangGraph recorded the node.

    From that moment nothing reaches the store (the process is gone) until `restart()`.
    """

    crashed = False
    dead = False

    def restart(self) -> None:
        self.dead = False

    async def aput_writes(self, config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
        if not self.crashed and any(channel == "action_ids" for channel, _ in writes):
            self.crashed = self.dead = True
        if self.dead:
            raise SimulatedCrash
        await super().aput_writes(config, writes, task_id, task_path)

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        if self.dead:
            raise SimulatedCrash
        return await super().aput(config, checkpoint, metadata, new_versions)


async def test_process_death_after_proposals_were_created_resumes_without_duplicates(
    make_harness: HarnessFactory,
) -> None:
    saver = _DiesAfterPlanCommit()
    first_agent = ScriptedAgent()
    h = await make_harness(checkpointer=saver, agent=first_agent)
    member = await synced_member(h)
    email_id = await email_id_by_provider_id(h, member.user_id, INCIDENT_ID)
    resp = await member.post(f"/emails/{email_id}/analyze")
    run_id = resp.json()["workflow_run"]["id"]
    with pytest.raises(SimulatedCrash):
        await h.drain()
    async with h.db.session() as s:
        created = (await s.execute(select(ProposedAction.id))).scalars().all()
    assert len(created) == 2  # the proposals were committed before the "crash"

    saver.restart()
    second_agent = ScriptedAgent()
    restart_worker(h, saver, second_agent)  # same durable checkpoint store
    assert await expire_leases(h) == 1
    await h.drain()
    detail = (await member.get(f"/workflows/{run_id}")).json()
    assert detail["run"]["status"] == "pending_approval"
    assert sorted(a["id"] for a in detail["actions"]) == sorted(str(i) for i in created)  # reused, not duplicated
    assert second_agent.calls == {"classify": 0, "extract": 0, "plan": 1}  # only the interrupted step re-ran
    [job] = await jobs_of(h, JobType.ANALYZE_EMAIL)
    assert (job.status, job.attempts) == (JobStatus.SUCCEEDED, 2)


async def test_database_outage_mid_analysis_is_recorded_sanitized_and_recovered(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.workflows.graph as graph_module

    real = graph_module.get_tenant_settings
    outage = [operational_error("SELECT app_settings.settings FROM app_settings WHERE tenant_id = 'secret-ish'")]

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        if outage:
            raise outage.pop()
        return await real(*args, **kwargs)

    monkeypatch.setattr(graph_module, "get_tenant_settings", flaky)
    member = await synced_member(harness)
    cid = "corr-adversarial-0001"
    run_id = await analyzed(harness, member, INCIDENT_ID, **{"X-Request-ID": cid})

    failed = (await member.get(f"/workflows/{run_id}")).json()
    assert (failed["run"]["status"], failed["run"]["error_category"]) == ("failed", "internal")
    assert failed["run"]["error_message"] == "OperationalError"  # no SQL, parameters or driver text
    assert failed["run"]["correlation_id"] == cid
    [step_failed] = [e for e in failed["events"] if e["event_type"] == "step_failed"]
    assert step_failed["message"] == "OperationalError"
    [job] = await jobs_of(harness, JobType.ANALYZE_EMAIL)
    assert (job.status, job.last_error, job.correlation_id) == (JobStatus.QUEUED, "OperationalError", cid)
    assert "app_settings" not in json.dumps(failed)

    await fast_forward_backoff(harness)
    await harness.drain()
    recovered = (await member.get(f"/workflows/{run_id}")).json()["run"]
    assert (recovered["status"], recovered["error_message"]) == ("pending_approval", None)


def _fail_commit_once_when(monkeypatch: pytest.MonkeyPatch, event_type: str) -> list[str]:
    """The commit carrying a pending audit event of `event_type` fails once (lock timeout / lost connection)."""
    real_commit = AsyncSession.commit
    failed: list[str] = []

    async def commit(self: AsyncSession) -> None:
        if not failed and any(isinstance(o, AuditEvent) and o.event_type == event_type for o in self.new):
            failed.append(event_type)
            raise operational_error()
        await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    return failed


# DEFECT (SQLite / demo mode only): create_run's `begin_nested()` is the first write of the request, so pysqlite
# commits it on RELEASE. When the request then fails (here: the final COMMIT), the email update and the analysis
# job roll back but the RECEIVED run survives without a job. Every later "Analyze" returns that run
# (200, created=false, job_id=null) and nothing ever runs it. PostgreSQL rolls the run back (this test passes there).
async def test_failed_analysis_request_leaves_no_orphan_run(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    member = await synced_member(harness)
    email_id = await email_id_by_provider_id(harness, member.user_id, INCIDENT_ID)
    failed = _fail_commit_once_when(monkeypatch, "analysis_requested")
    async with error_client(harness, member) as client:
        resp = await client.post(
            f"{API}/emails/{email_id}/analyze", headers={**member.headers, "X-Request-ID": "c" * 16}
        )
    assert failed == ["analysis_requested"]
    error = assert_envelope(resp, 500)
    assert (error["code"], error["message"], error["correlation_id"]) == (
        "internal_error",
        "An unexpected error occurred.",  # sanitized: no SQL, no driver message
        "c" * 16,
    )

    retry = await member.post(f"/emails/{email_id}/analyze")  # the user simply tries again
    assert retry.status_code in (200, 202)
    await harness.drain()
    run = (await member.get(f"/workflows/{retry.json()['workflow_run']['id']}")).json()["run"]
    assert run["status"] == "pending_approval", "the analysis was never queued"


# DEFECT (SQLite / demo mode only): same root cause on the write path. queue_execution's enqueue is the request's
# first write, so the execute job is committed even though the request fails and the user gets a 500: the work
# item is created anyway and the trail lacks its `execute_requested` audit event. PostgreSQL rolls it back.
async def test_failed_execute_request_queues_nothing(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    member = await synced_member(harness)
    action = await _work_item(harness, member)
    assert (await member.post(f"/actions/{action['id']}/approve", json=review_body(action))).status_code == 200
    await harness.drain()
    failed = _fail_commit_once_when(monkeypatch, "execute_requested")
    async with error_client(harness, member) as client:
        resp = await client.post(
            f"{API}/actions/{action['id']}/execute", json=review_body(action), headers=member.headers
        )
    assert failed == ["execute_requested"]
    assert_envelope(resp, 500)
    await harness.drain()
    after = (await member.get(f"/actions/{action['id']}")).json()
    assert after["status"] == "approved", "the request failed, yet the external write was performed"
    assert not any(c.create_attempts for c in harness.ado.clients)


# DEFECT (both databases): auto-analysis runs after the sync's own commit, so a transient database error there
# fails the job *after* the new emails and the delta cursor were stored. The retry sees no "new" emails and never
# queues their analysis (on SQLite the first email additionally keeps an orphan RECEIVED run without a job).
async def test_transient_error_during_auto_analysis_does_not_skip_new_emails(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await harness.persona("admin")
    current = (await admin.get("/settings")).json()
    body = {
        "expected_version": current["version"],
        "settings": {**current["settings"], "auto_analyze_new_emails": True},
    }
    assert (await admin.put("/settings", json=body)).status_code == 200
    real = WorkflowService.create_run
    outage = [operational_error("INSERT INTO workflow_runs ...")]
    calls: list[uuid.UUID] = []

    async def create_run(self: WorkflowService, session: AsyncSession, email: EmailMessage, **kw: Any) -> Any:
        calls.append(email.id)
        if len(calls) == 2 and outage:
            raise outage.pop()
        return await real(self, session, email, **kw)

    monkeypatch.setattr(WorkflowService, "create_run", create_run)
    member = await harness.persona("member")
    assert (await member.post("/emails/sync", json={})).status_code == 202
    for _ in range(3):
        await harness.drain()
        await fast_forward_backoff(harness)
    first_round = {m["id"] for m in FIXTURE[:10]}
    statuses = {e.provider_message_id: e.analysis_status.value for e in await emails(harness)}
    skipped = sorted(pid for pid in first_round if statuses.get(pid) in ("not_analyzed", "queued"))
    assert skipped == [], f"{len(skipped)} new emails were never auto-analyzed"
    async with harness.db.session() as s:
        stuck = (await s.execute(select(WorkflowRun.id).where(WorkflowRun.status == "received"))).scalars().all()
    assert stuck == []


# DEFECT (low): cancelling a run whose analysis FAILED (job FAILED/DEAD, graph parked before the failed node)
# never runs `finalize`: the cancel resume falls back to sync_run_status, which returns early for CANCELLED runs.
# The run is CANCELLED but completed_at stays NULL and there is no finalize event / workflow_finished audit event,
# unlike every other cancellation path.
async def test_cancelling_a_failed_analysis_finalizes_the_run(make_harness: HarnessFactory) -> None:
    h = await make_harness(agent=ScriptedAgent({"extract": [AgentOutputError("schema-invalid output")]}))
    member = await synced_member(h)
    run_id = await analyzed(h, member, INCIDENT_ID)
    [job] = await jobs_of(h, JobType.ANALYZE_EMAIL)
    assert job.status is JobStatus.FAILED  # not retried: nothing else will ever advance the graph
    assert (await member.get(f"/workflows/{run_id}")).json()["run"]["status"] == "failed"

    assert (await member.post(f"/workflows/{run_id}/cancel")).status_code == 200
    await h.drain()
    detail = (await member.get(f"/workflows/{run_id}")).json()
    run = detail["run"]
    assert run["status"] == "cancelled"
    email = (await member.get(f"/emails/{run['email_message_id']}")).json()
    assert email["analysis_status"] == "done"
    assert run["completed_at"] is not None
    assert detail["events"][-1]["step"] == "finalize"
    finished = await h.audit_events(event_type="workflow_finished", workflow_run_id=uuid.UUID(run_id))
    assert [e.details for e in finished] == [{"status": "cancelled"}]


# ===================================================================================================
# 3. Timeouts on outbound calls, actionable + sanitized errors with correlation ids
#
# Existing coverage (not repeated): LLM client timeout/retries (tests/unit/agents/test_openai_agent.py, request
# "timeout" and client.timeout assertions), MSAL timeout (tests/unit/integrations/test_entra_oauth_extra.py),
# Azure DevOps client timeout (tests/unit/integrations/test_azure_devops_client_extra.py), JWKS fetch timeout
# (tests/security/test_id_token_validation.py::test_jwks_fetches_use_the_configured_timeout_and_never_follow_
# redirects), SQLAlchemy PostgreSQL statement/lock timeouts (tests/security/test_runtime_hardening.py::
# test_postgres_connections_get_statement_and_lock_timeouts), 500 envelope with correlation id
# (tests/unit/api/test_errors.py). Sanitized run/job errors with correlation ids:
# test_database_outage_mid_analysis_is_recorded_sanitized_and_recovered and
# test_failed_analysis_request_leaves_no_orphan_run above.
# ===================================================================================================


# DEFECT (PostgreSQL only): the LangGraph checkpointer's psycopg pool is opened with its own connection kwargs
# and gets neither statement_timeout nor lock_timeout (F-11 covers only the SQLAlchemy engine), so a checkpoint
# read/write stuck on a lock or a runaway query pins a worker slot indefinitely.
async def test_checkpointer_connections_get_statement_and_lock_timeouts(fake_postgres: type[_FakePool]) -> None:  # noqa: F811
    settings = make_settings("postgresql+psycopg://u:p@db:5432/nuvexa")
    async with cp.open_checkpointer(settings):
        pass
    (pool,) = fake_postgres.instances
    options = str(pool.kwargs["kwargs"].get("options", ""))
    assert f"statement_timeout={settings.database_statement_timeout_ms}" in options
    assert f"lock_timeout={settings.database_lock_timeout_ms}" in options


async def test_graph_mail_client_uses_bounded_timeouts_and_never_follows_redirects(database_url: str) -> None:
    settings = make_settings(database_url, graph_timeout_seconds=7.5)
    graph = GraphMailboxProvider(StaticTokenProvider(), settings, sleep=no_sleep)  # owns its HTTP client
    with respx.mock(assert_all_called=False) as router:
        delta = router.get(url__startswith=DELTA).mock(
            return_value=httpx.Response(302, headers={"location": "https://evil.example/steal"})
        )
        evil = router.get(host="evil.example").mock(return_value=httpx.Response(200, json={"value": []}))
        with pytest.raises(IntegrationError) as info:
            await graph.fetch_changes(None, max_messages=5, initial_days=1)
    assert delta.call_count == 1
    assert not evil.called
    assert "evil" not in str(info.value)
    timeout = delta.calls.last.request.extensions["timeout"]
    assert timeout == {"connect": 7.5, "read": 7.5, "write": 7.5, "pool": 7.5}


# DEFECT (low, observability): the engine fallback logs the reason a queued execute/reconcile was dropped as
# `code=exc.code`, but the redaction processor masks every key named `code` (OAuth authorization codes), so the
# only trace says `code=[REDACTED]`. Same pattern: app/api/errors.py (`app_error`), app/api/routes/auth.py.
async def test_dropped_execution_requests_are_logged_with_an_actionable_reason(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    member = await synced_member(harness)
    run_id = await analyzed(harness, member, FEATURE_ID)
    restart_worker(harness, InMemorySaver())  # graph not waiting → engine falls back to the services
    decision = {"type": "execute", "action_id": str(uuid.uuid4()), "actor_user_id": str(member.user_id)}
    with caplog.at_level("WARNING"):
        await harness.engine.resume(uuid.UUID(run_id), member.tenant_id, decision)  # type: ignore[arg-type]
    [record] = [r for r in caplog.records if "workflow_fallback_operation_failed" in r.getMessage()]
    assert "not_found" in record.getMessage()
