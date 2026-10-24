"""In-process API + worker harness (no network): httpx ASGI client, heuristic agent, mock Azure DevOps."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import pytest
from asgi_lifespan import LifespanManager
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import EmailAnalysisAgent
from app.agents.factory import build_analysis_agent
from app.core.config import Settings
from app.core.timeutil import utcnow
from app.db.models import AuditEvent, ConnectedAccount, EmailMessage, Tenant, User
from app.db.session import Database
from app.demo import DEMO_TENANT
from app.domain.enums import ConnectionProvider, ConnectionStatus, UserRole
from app.integrations.azure_devops.base import AzureDevOpsClient
from app.integrations.azure_devops.mock import MockAzureDevOpsClient
from app.integrations.microsoft_graph.demo_mailbox import load_demo_fixture
from app.main import create_app
from app.schemas.api import ErrorResponse
from app.services.auth_service import SessionService
from app.services.authz import Actor
from app.services.execution_service import ExecutionService
from app.services.integration_clients import AdoClientFactory, make_ado_client_factory
from app.workers.runner import JobHandlers, Worker
from app.workflows.engine import WorkflowEngine
from app.workflows.graph import WorkflowDeps
from tests.conftest import make_settings

API = "/api/v1"
DEMO_MESSAGE_COUNT = len(load_demo_fixture()["messages"])


@dataclass
class MockAdo:
    """Controls the mock Azure DevOps client handed to the execution service."""

    fail_mode: str = "none"
    clients: list[MockAzureDevOpsClient] = field(default_factory=list)

    async def factory(self, _session: AsyncSession, _actor: Actor) -> AzureDevOpsClient:
        client = MockAzureDevOpsClient(fail_mode=self.fail_mode)
        self.clients.append(client)
        return client


@dataclass
class Persona:
    """A logged-in browser: its own cookie jar plus the CSRF header for unsafe requests."""

    name: str
    client: httpx.AsyncClient
    headers: dict[str, str]
    user_id: uuid.UUID
    tenant_id: uuid.UUID

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.client.get(f"{API}{path}", **kw)

    async def post(self, path: str, **kw: Any) -> httpx.Response:
        return await self.client.post(f"{API}{path}", headers=self.headers, **kw)

    async def patch(self, path: str, **kw: Any) -> httpx.Response:
        return await self.client.patch(f"{API}{path}", headers=self.headers, **kw)

    async def put(self, path: str, **kw: Any) -> httpx.Response:
        return await self.client.put(f"{API}{path}", headers=self.headers, **kw)

    async def delete(self, path: str, **kw: Any) -> httpx.Response:
        return await self.client.delete(f"{API}{path}", headers=self.headers, **kw)


@dataclass
class Harness:
    settings: Settings
    db: Database
    client: httpx.AsyncClient
    worker: Worker
    engine: WorkflowEngine
    ado: MockAdo
    execution: ExecutionService
    _clients: list[httpx.AsyncClient] = field(default_factory=list)

    async def drain(self, max_jobs: int = 200) -> int:
        return await self.worker.run_once(max_jobs=max_jobs)

    async def login(self, persona: str = "member") -> dict[str, str]:
        """Log in a demo persona on a fresh client; returns headers including the CSRF token."""
        resp = await self.client.post(f"{API}/auth/demo-login", json={"persona": persona})
        assert resp.status_code == 200, resp.text
        return {"X-CSRF-Token": self.client.cookies.get(self.settings.csrf_cookie_name) or ""}

    def new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.client._transport, base_url="http://test")

    def tracked_client(self) -> httpx.AsyncClient:
        client = self.new_client()
        self._clients.append(client)
        return client

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.client.get(f"{API}{path}", **kw)

    async def post(self, path: str, headers: dict[str, str], **kw: Any) -> httpx.Response:
        return await self.client.post(f"{API}{path}", headers=headers, **kw)

    # -- personas ----------------------------------------------------------------------------------
    async def persona(self, name: str) -> Persona:
        """A demo persona (admin/approver/member/viewer) logged in on its own client."""
        client = self.tracked_client()
        resp = await client.post(f"{API}/auth/demo-login", json={"persona": name})
        assert resp.status_code == 200, resp.text
        me = resp.json()["user"]
        headers = {"X-CSRF-Token": client.cookies.get(self.settings.csrf_cookie_name) or ""}
        return Persona(name, client, headers, uuid.UUID(me["id"]), uuid.UUID(me["tenant_id"]))

    async def user_persona(self, user: User, name: str | None = None) -> Persona:
        """Any user row, logged in by issuing a real session (as a successful sign-in would)."""
        async with self.db.session() as s:
            row = await s.get(User, user.id)
            assert row is not None
            issued = await SessionService(self.settings).issue(s, row, ip=None, user_agent="pytest")
            await s.commit()
        client = self.tracked_client()
        client.cookies.set(self.settings.session_cookie_name, issued.token)
        client.cookies.set(self.settings.csrf_cookie_name, issued.csrf_token)
        return Persona(name or user.email, client, {"X-CSRF-Token": issued.csrf_token}, user.id, user.tenant_id)

    async def demo_tenant_id(self) -> uuid.UUID:
        async with self.db.session() as s:
            tid = (await s.execute(select(Tenant.id).where(Tenant.slug == DEMO_TENANT.slug))).scalar_one_or_none()
        assert tid is not None, "log in a demo persona first"
        return tid

    async def create_tenant(self, name: str = "Other Co") -> uuid.UUID:
        async with self.db.session() as s:
            tenant = Tenant(name=name, slug=f"t-{uuid.uuid4().hex[:10]}")
            s.add(tenant)
            await s.commit()
            return tenant.id

    async def create_user(
        self, tenant_id: uuid.UUID, role: UserRole, *, email: str | None = None, demo_mailbox: bool = True
    ) -> User:
        """A user (optionally with a synthetic demo mailbox, so it can sync its own mail)."""
        async with self.db.session() as s:
            user = User(
                tenant_id=tenant_id,
                email=email or f"{role.value}-{uuid.uuid4().hex[:6]}@example.com",
                display_name=f"Test {role.value.title()}",
                role=role,
                is_active=True,
            )
            s.add(user)
            await s.flush()
            if demo_mailbox:
                s.add(
                    ConnectedAccount(
                        tenant_id=tenant_id,
                        user_id=user.id,
                        provider=ConnectionProvider.DEMO_MAILBOX,
                        external_account_id=f"demo:{user.id}",
                        email=user.email,
                        display_name=f"{user.display_name} (demo)",
                        granted_scopes=["demo.mailbox.read"],
                        status=ConnectionStatus.ACTIVE,
                        connected_at=utcnow(),
                    )
                )
            await s.commit()
            return user

    async def audit_events(self, **filters: Any) -> list[AuditEvent]:
        async with self.db.session() as s:
            stmt = select(AuditEvent).order_by(AuditEvent.created_at)
            for key, value in filters.items():
                stmt = stmt.where(getattr(AuditEvent, key) == value)
            return list((await s.execute(stmt)).scalars().all())


@asynccontextmanager
async def build_harness(
    settings: Settings,
    db: Database,
    *,
    ado_factory: AdoClientFactory | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    agent: EmailAnalysisAgent | None = None,
) -> AsyncIterator[Harness]:
    """App + worker sharing one database. `ado_factory` replaces the mock Azure DevOps client factory."""
    MockAzureDevOpsClient.reset()
    ado = MockAdo()
    execution = ExecutionService(settings, ado_factory or ado.factory)
    deps = WorkflowDeps(
        settings=settings,
        session_factory=db.session_factory,
        agent=agent or build_analysis_agent(settings),
        execution=execution,
    )
    engine = WorkflowEngine(deps, checkpointer or InMemorySaver())
    worker = Worker(db, settings, JobHandlers(db, settings, engine))
    app = create_app(settings, db)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            h = Harness(settings, db, client, worker, engine, ado, execution)
            try:
                yield h
            finally:
                for extra in h._clients:
                    await extra.aclose()


@pytest.fixture
async def harness(settings: Settings, db: Database) -> AsyncIterator[Harness]:
    async with build_harness(settings, db) as h:
        yield h


HarnessFactory = Callable[..., Awaitable[Harness]]


@pytest.fixture
async def make_harness(database_url: str, db: Database) -> AsyncIterator[HarnessFactory]:
    """Build harnesses with settings overrides, e.g. `await make_harness(azure_devops_mode="pat", ...)`.

    Keyword arguments `ado_factory`, `checkpointer` and `agent` are passed to `build_harness`
    (`ado_factory="real"` wires the production factory, i.e. the REST client in PAT mode); everything else
    overrides `Settings`. All harnesses share the test database.
    """
    async with AsyncExitStack() as stack:

        async def _make(
            *,
            ado_factory: AdoClientFactory | Literal["real"] | None = None,
            checkpointer: BaseCheckpointSaver[str] | None = None,
            agent: EmailAnalysisAgent | None = None,
            **overrides: Any,
        ) -> Harness:
            settings = make_settings(database_url, **overrides)
            factory = make_ado_client_factory(settings) if ado_factory == "real" else ado_factory
            return await stack.enter_async_context(
                build_harness(settings, db, ado_factory=factory, checkpointer=checkpointer, agent=agent)
            )

        yield _make


# -- workflow helpers ----------------------------------------------------------------------------------


async def sync_and_analyze(h: Harness, headers: dict[str, str], subject_contains: str) -> dict[str, Any]:
    """Sync the demo mailbox, analyze the email whose subject matches, return the workflow detail."""
    resp = await h.post("/emails/sync", headers, json={})
    assert resp.status_code == 202, resp.text
    await h.drain()
    emails = (await h.get("/emails", params={"limit": 100})).json()["items"]
    email = next(e for e in emails if subject_contains.lower() in e["subject"].lower())
    resp = await h.post(f"/emails/{email['id']}/analyze", headers)
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["workflow_run"]["id"]
    await h.drain()
    detail = (await h.get(f"/workflows/{run_id}")).json()
    return detail


def find_action(detail: dict[str, Any], action_type: str) -> dict[str, Any]:
    return next(a for a in detail["actions"] if a["action_type"] == action_type)


async def sync_all(h: Harness, p: Persona) -> list[dict[str, Any]]:
    """Sync the persona's demo mailbox until every fixture message is delivered (10 + 2 + 2)."""
    for _ in range(8):
        resp = await p.post("/emails/sync", json={})
        assert resp.status_code == 202, resp.text
        await h.drain()
        page = (await p.get("/emails", params={"limit": 100})).json()
        if page["total"] >= DEMO_MESSAGE_COUNT:
            return list(page["items"])
    raise AssertionError("demo mailbox did not deliver every message")


async def email_id_by_provider_id(h: Harness, owner_user_id: uuid.UUID, provider_message_id: str) -> uuid.UUID:
    async with h.db.session() as s:
        email_id = (
            await s.execute(
                select(EmailMessage.id).where(
                    EmailMessage.owner_user_id == owner_user_id,
                    EmailMessage.provider_message_id == provider_message_id,
                )
            )
        ).scalar_one()
    return email_id


async def analyze(h: Harness, p: Persona, email_id: uuid.UUID | str) -> dict[str, Any]:
    """Start analysis as `p`, run the worker, return the workflow detail."""
    resp = await p.post(f"/emails/{email_id}/analyze")
    assert resp.status_code in (200, 202), resp.text
    run_id = resp.json()["workflow_run"]["id"]
    await h.drain()
    detail = await p.get(f"/workflows/{run_id}")
    assert detail.status_code == 200, detail.text
    return dict(detail.json())


def review_body(action: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"version": action["version"], "payload_hash": action["payload_hash"], **extra}


async def approve_and_execute(h: Harness, p: Persona, action: dict[str, Any]) -> dict[str, Any]:
    resp = await p.post(f"/actions/{action['id']}/approve", json=review_body(action))
    assert resp.status_code == 200, resp.text
    resp = await p.post(f"/actions/{action['id']}/execute", json=review_body(action))
    assert resp.status_code == 202, resp.text
    await h.drain()
    return dict((await p.get(f"/actions/{action['id']}")).json())


def assert_error(resp: httpx.Response, status: int, code: str | None = None) -> dict[str, Any]:
    """Every non-2xx response: the documented envelope, a correlation id, and the X-Request-ID header."""
    assert resp.status_code == status, f"{resp.status_code} != {status}: {resp.text}"
    body = ErrorResponse.model_validate(resp.json())
    if code is not None:
        assert body.error.code == code, body
    request_id = resp.headers.get("x-request-id")
    assert request_id, "X-Request-ID header missing on error response"
    assert body.error.correlation_id == request_id
    return dict(resp.json()["error"])
