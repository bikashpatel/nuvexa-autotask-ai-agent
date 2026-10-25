"""Helpers for `test_adversarial_ai_and_data_exposure.py` (offline: synthetic data, scripted fakes only).

* `SyntheticSecrets` — every configured secret, generated at runtime (nothing realistic is committed).
* `FakeLLM` — a wire-level stand-in for the OpenAI chat-completions endpoint. The OpenAI SDK uses
  `httpx2`, which respx cannot intercept, so the real `AsyncOpenAI` client built by
  `OpenAIAnalysisAgent._build_client` is given an `httpx2.MockTransport`. The SDK's own retry loop,
  timeouts, status mapping and structured-output parsing therefore run for real.
* Email ingestion through the production Graph normalizer (`normalize_graph_message` + `apply_normalized`).
* Sweeps over every database table, API responses and checkpoints for secret / canary substrings.
"""

from __future__ import annotations

import base64
import json
import re
import secrets as pysecrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx2
import openai
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, update

from app.agents import openai_agent
from app.core.timeutil import utcnow
from app.db.base import Base
from app.db.models import ConnectedAccount, EmailMessage, Job
from app.domain.enums import EmailAnalysisStatus, JobStatus
from app.integrations.microsoft_graph.normalize import normalize_graph_message
from app.services.email_sync_service import apply_normalized
from tests.integration.conftest import Harness, Persona

LLM_BASE_URL = "https://llm.nuvexa-test.invalid/v1"
LLM_HOST = "llm.nuvexa-test.invalid"
CONFIGURED_PROJECT = "NUVEXA-Demo"  # tests/conftest.make_settings: azure_devops_default_project
ATTACKER_PROJECT = "Victim-Prod"
ATTACKER_HOST = "evil.example"

_NONCE_RE = re.compile(r"markers carry the nonce ([0-9a-f]{16})")
_MARKER_WORD_RE = re.compile(r"(END_)?(EMAIL|ANALYSIS)_DATA", re.IGNORECASE)


# ---------------------------------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------------------------------


def _token(n: int) -> str:
    return pysecrets.token_hex(n)


@dataclass(frozen=True)
class SyntheticSecrets:
    token_encryption_key: str
    audit_hmac_key: str
    azure_devops_pat: str
    openai_api_key: str
    entra_client_secret: str
    oauth_access_token: str

    @classmethod
    def generate(cls) -> SyntheticSecrets:
        return cls(
            token_encryption_key=Fernet.generate_key().decode(),
            audit_hmac_key="hmac" + _token(24),
            azure_devops_pat="pat" + _token(24),
            openai_api_key="sk-" + _token(24),
            entra_client_secret="ecs" + _token(16),
            oauth_access_token="eyJ" + _token(12) + "." + "eyJ" + _token(12) + "." + _token(16),
        )

    @property
    def basic_pat(self) -> str:
        """The `Authorization: Basic` credential Azure DevOps receives for the PAT."""
        return base64.b64encode(f":{self.azure_devops_pat}".encode()).decode()

    def settings(self) -> dict[str, Any]:
        return {
            "token_encryption_keys": self.token_encryption_key,
            "audit_hmac_key": self.audit_hmac_key,
            "azure_devops_pat": self.azure_devops_pat,
            "openai_api_key": self.openai_api_key,
            "entra_client_secret": self.entra_client_secret,
        }

    def needles(self) -> dict[str, str]:
        return {
            "TOKEN_ENCRYPTION_KEYS": self.token_encryption_key,
            "AUDIT_HMAC_KEY": self.audit_hmac_key,
            "AZURE_DEVOPS_PAT": self.azure_devops_pat,
            "AZURE_DEVOPS_PAT (Basic)": self.basic_pat,
            "OPENAI_API_KEY": self.openai_api_key,
            "ENTRA_CLIENT_SECRET": self.entra_client_secret,
            "OAuth access token": self.oauth_access_token,
        }


def leaked(haystack: str, needles: dict[str, str]) -> list[str]:
    """Names of the needles that occur in `haystack`."""
    return sorted(name for name, value in needles.items() if value and value in haystack)


# ---------------------------------------------------------------------------------------------------
# Email ingestion (production normalizer, no network)
# ---------------------------------------------------------------------------------------------------


def graph_message(
    *,
    subject: str = "Customer portal login fails",
    body: str = "Hello, users cannot log in to the customer portal since this morning. Thanks, Dana",
    content_type: str = "text",
    sender_name: str = "Dana Reyes",
    sender_address: str = "dana.reyes@example.com",
    attachments: list[dict[str, Any]] | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    msg_id = f"AAMk-{uuid.uuid4().hex}"
    received = (utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    item: dict[str, Any] = {
        "id": msg_id,
        "internetMessageId": f"<{msg_id}@example.com>",
        "conversationId": conversation_id or f"conv-{msg_id}",
        "changeKey": f"{msg_id}-v1",
        "subject": subject,
        "from": {"emailAddress": {"name": sender_name, "address": sender_address}},
        "toRecipients": [{"emailAddress": {"name": "Support", "address": "support-desk@example.com"}}],
        "ccRecipients": [],
        "receivedDateTime": received,
        "sentDateTime": received,
        "body": {"contentType": content_type, "content": body},
        "hasAttachments": bool(attachments),
        "isRead": False,
    }
    if attachments:
        item["attachments"] = attachments
    return item


async def ingest_email(h: Harness, persona: Persona, message: dict[str, Any]) -> uuid.UUID:
    """Store `message` in the persona's mailbox exactly like a Graph sync would (normalize → apply)."""
    normalized = normalize_graph_message(message)
    async with h.db.session() as s:
        account = (
            await s.execute(select(ConnectedAccount).where(ConnectedAccount.user_id == persona.user_id))
        ).scalar_one()
        email = EmailMessage(
            tenant_id=account.tenant_id,
            owner_user_id=account.user_id,
            connected_account_id=account.id,
            provider_message_id=normalized.provider_message_id,
            analysis_status=EmailAnalysisStatus.NOT_ANALYZED,
            content_hash="",
        )
        apply_normalized(email, normalized)
        s.add(email)
        await s.commit()
        return email.id


async def start_analysis(h: Harness, persona: Persona, email_id: uuid.UUID) -> str:
    resp = await persona.post(f"/emails/{email_id}/analyze")
    assert resp.status_code in (200, 202), resp.text
    await h.drain()
    return str(resp.json()["workflow_run"]["id"])


async def workflow(persona: Persona, run_id: str) -> dict[str, Any]:
    resp = await persona.get(f"/workflows/{run_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def jobs(h: Harness) -> list[Job]:
    async with h.db.session() as s:
        return list((await s.execute(select(Job).order_by(Job.created_at))).scalars().all())


async def fast_forward_backoff(h: Harness) -> None:
    async with h.db.session() as s:
        await s.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(run_after=utcnow()))
        await s.commit()


# ---------------------------------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------------------------------


async def dump_database(h: Harness) -> str:
    """Every row of every table (including encrypted credentials and audit HMACs) as text."""
    parts: list[str] = []
    async with h.db.session() as s:
        for table in Base.metadata.sorted_tables:
            for row in (await s.execute(table.select())).all():
                parts.append(f"{table.name}: {tuple(row)!r}")
    return "\n".join(parts)


async def dump_api(persona: Persona, paths: list[str]) -> str:
    parts: list[str] = []
    for path in paths:
        resp = await persona.get(path)
        assert resp.status_code == 200, f"GET {path} -> {resp.status_code}"  # a sweep of error pages proves nothing
        parts.append(f"GET {path}: {resp.text}")
    return "\n".join(parts)


async def dump_checkpoints(h: Harness) -> str:
    """LangGraph checkpoints and pending writes (InMemorySaver, or the production AsyncSqliteSaver tables)."""
    saver = h.engine.graph.checkpointer
    conn = getattr(saver, "conn", None)
    if conn is not None:  # AsyncSqliteSaver from app.workflows.checkpointer.open_checkpointer
        parts = []
        for table in ("checkpoints", "writes"):
            async with conn.execute(f"SELECT * FROM {table}") as cursor:
                parts.extend(f"{table}: {tuple(row)!r}" for row in await cursor.fetchall())
        return "\n".join(parts)
    return repr(getattr(saver, "storage", None)) + repr(getattr(saver, "writes", None))


# ---------------------------------------------------------------------------------------------------
# Delimited data blocks in prompts
# ---------------------------------------------------------------------------------------------------


@dataclass
class DataBlocks:
    nonce: str
    email_block: str
    analysis_block: str | None
    outside: str  # the user message with both blocks removed


def split_blocks(user_message: str) -> DataBlocks:
    """Check delimiter integrity and return the parts of a user prompt.

    Exactly one opening and one closing marker per block, carrying the nonce announced in the intro;
    no other delimiter material (`<<<`, `>>>`, marker words) inside a block.
    """
    match = _NONCE_RE.search(user_message)
    assert match, "the user message does not announce the data-block nonce"
    nonce = match.group(1)
    outside = user_message
    blocks: dict[str, str | None] = {}
    for kind in ("EMAIL", "ANALYSIS"):
        open_marker, close_marker = f"<<<{kind}_DATA_{nonce}>>>", f"<<<END_{kind}_DATA_{nonce}>>>"
        opens, closes = user_message.count(open_marker), user_message.count(close_marker)
        if kind == "ANALYSIS" and opens == closes == 0:
            blocks[kind] = None
            continue
        assert (opens, closes) == (1, 1), f"{kind} block markers: {opens} open / {closes} close"
        start = user_message.index(open_marker) + len(open_marker)
        end = user_message.index(close_marker)
        assert start <= end, f"{kind} block closes before it opens"
        inner = user_message[start:end]
        assert "<<<" not in inner, f"forged '<<<' run inside the {kind} block"
        assert ">>>" not in inner, f"forged '>>>' run inside the {kind} block"
        assert not _MARKER_WORD_RE.search(inner), f"delimiter marker word inside the {kind} block"
        blocks[kind] = inner
        outside = outside.replace(f"{open_marker}{inner}{close_marker}", " ")
    email_block = blocks["EMAIL"]
    assert email_block is not None
    return DataBlocks(nonce=nonce, email_block=email_block, analysis_block=blocks["ANALYSIS"], outside=outside)


# ---------------------------------------------------------------------------------------------------
# Wire-level fake of the OpenAI chat-completions endpoint
# ---------------------------------------------------------------------------------------------------

Reply = Callable[[dict[str, Any], httpx2.Request], httpx2.Response]


def chat_completion(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    refusal: str | None = None,
    finish_reason: str = "stop",
    choices: bool = True,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content, "refusal": refusal}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-test-2026",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}] if choices else [],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280},
    }


def tool(name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function", "function": {"name": name, "arguments": raw}}


def respond(payload: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> Reply:
    return lambda _body, _request: httpx2.Response(status, json=payload, headers=headers)


def raise_(make: Callable[[httpx2.Request], Exception]) -> Reply:
    def reply(_body: dict[str, Any], request: httpx2.Request) -> httpx2.Response:
        raise make(request)

    return reply


BENIGN_CLASSIFICATION: dict[str, Any] = {
    "category": "incident",
    "rationale": "Users report that they cannot log in.",
    "urgency": "high",
    "priority": "high",
    "action_recommended": True,
    "needs_more_info": False,
    "missing_information": [],
    "extraction_warnings": [],
    "confidence": 0.8,
}
BENIGN_EXTRACTION: dict[str, Any] = {
    "summary": "Investigate customer portal login failures",
    "description": "Users cannot log in to the customer portal.",
    "requester_name": None,
    "requester_email": None,
    "organization": None,
    "product_or_system": "customer portal",
    "environment": None,
    "impact": None,
    "deadline": None,
    "suggested_owner": None,
    "suggested_team": None,
    "references": [],
    "suggested_work_item_type": "Bug",
    "proposed_fields": [],
    "evidence": [],
}
BENIGN_WORK_ITEM: dict[str, Any] = {
    "title": "Investigate customer portal login failures",
    "description": "Users cannot log in to the customer portal.",
    "work_item_type": "Bug",
    "priority": "high",
    "tags": ["login"],
    "rationale": "A login outage was reported.",
    "expected_impact": "The outage is tracked.",
}


def _benign(step: str) -> Reply:
    if step == "classify":
        return respond(chat_completion(content=json.dumps(BENIGN_CLASSIFICATION)))
    if step == "extract":
        return respond(chat_completion(content=json.dumps(BENIGN_EXTRACTION)))
    return respond(
        chat_completion(
            tool_calls=[tool("propose_azure_devops_work_item", BENIGN_WORK_ITEM)], finish_reason="tool_calls"
        )
    )


def step_of(body: dict[str, Any]) -> str:
    if body.get("tools"):
        return "plan"
    name = ((body.get("response_format") or {}).get("json_schema") or {}).get("name")
    return {"ClassificationOutput": "classify", "ExtractionOutput": "extract"}[name]


@dataclass
class LLMCall:
    step: str
    request: httpx2.Request
    body: dict[str, Any]

    def message(self, role: str) -> str:
        return "\n".join(str(m.get("content") or "") for m in self.body["messages"] if m.get("role") == role)

    @property
    def system(self) -> str:
        return self.message("system")

    @property
    def user(self) -> str:
        return self.message("user")


@dataclass
class FakeLLM:
    """Scripted OpenAI endpoint: per-step queues of replies; benign replies once a queue is empty."""

    script: dict[str, list[Reply]] = field(default_factory=lambda: {"classify": [], "extract": [], "plan": []})
    defaults: dict[str, Reply] = field(default_factory=lambda: {s: _benign(s) for s in ("classify", "extract", "plan")})
    calls: list[LLMCall] = field(default_factory=list)
    retry_sleeps: list[int] = field(default_factory=list)

    def queue(self, step: str, *replies: Reply) -> None:
        self.script[step].extend(replies)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        step = step_of(body)
        self.calls.append(LLMCall(step, request, body))
        queued = self.script[step]
        reply = queued.pop(0) if queued else self.defaults[step]
        return reply(body, request)

    def steps(self) -> list[str]:
        return [c.step for c in self.calls]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
        transport = httpx2.MockTransport(self.handler)

        def wired(real: Callable[..., Any]) -> Callable[..., Any]:
            def client_factory(**kwargs: Any) -> Any:
                # Exactly the client `_build_client` configures (key, endpoint, max_retries, timeout) + the fake wire.
                return real(**kwargs, http_client=httpx2.AsyncClient(transport=transport))

            return client_factory

        async def no_sleep(_client: Any, *, retries_taken: int, **_kwargs: Any) -> None:
            self.retry_sleeps.append(retries_taken)

        monkeypatch.setattr(openai_agent, "AsyncOpenAI", wired(openai_agent.AsyncOpenAI))
        monkeypatch.setattr(openai_agent, "AsyncAzureOpenAI", wired(openai_agent.AsyncAzureOpenAI))
        monkeypatch.setattr(openai._base_client.AsyncAPIClient, "_sleep_for_retry", no_sleep)
        return self


def llm_settings(secrets: SyntheticSecrets | None = None, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "llm_provider": "openai",
        "llm_model": "gpt-test",
        "openai_api_key": secrets.openai_api_key if secrets else "sk-" + _token(16),
        "openai_base_url": LLM_BASE_URL,
        "llm_timeout_seconds": 7.5,
    }
    values.update(overrides)
    return values


AZURE_ENDPOINT = "https://aoai.nuvexa-test.invalid"
AZURE_HOST = "aoai.nuvexa-test.invalid"
AZURE_DEPLOYMENT = "gpt-test-deployment"


def azure_llm_settings(api_key: str) -> dict[str, Any]:
    return {
        "llm_provider": "azure_openai",
        "azure_openai_endpoint": AZURE_ENDPOINT,
        "azure_openai_api_key": api_key,
        "azure_openai_api_version": "2024-10-21",
        "azure_openai_deployment": AZURE_DEPLOYMENT,
        "llm_timeout_seconds": 7.5,
    }
