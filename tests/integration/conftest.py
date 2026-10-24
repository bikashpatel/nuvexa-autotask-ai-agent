"""In-process API + worker harness (no network): httpx ASGI client, heuristic agent, mock Azure DevOps."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.factory import build_analysis_agent
from app.core.config import Settings
from app.db.session import Database
from app.integrations.azure_devops.base import AzureDevOpsClient
from app.integrations.azure_devops.mock import MockAzureDevOpsClient
from app.main import create_app
from app.services.authz import Actor
from app.services.execution_service import ExecutionService
from app.workers.runner import JobHandlers, Worker
from app.workflows.engine import WorkflowEngine
from app.workflows.graph import WorkflowDeps

API = "/api/v1"


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
class Harness:
    settings: Settings
    db: Database
    client: httpx.AsyncClient
    worker: Worker
    engine: WorkflowEngine
    ado: MockAdo
    execution: ExecutionService

    async def drain(self, max_jobs: int = 200) -> int:
        return await self.worker.run_once(max_jobs=max_jobs)

    async def login(self, persona: str = "member") -> dict[str, str]:
        """Log in a demo persona on a fresh client; returns headers including the CSRF token."""
        resp = await self.client.post(f"{API}/auth/demo-login", json={"persona": persona})
        assert resp.status_code == 200, resp.text
        return {"X-CSRF-Token": self.client.cookies.get(self.settings.csrf_cookie_name) or ""}

    def new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.client._transport, base_url="http://test")

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.client.get(f"{API}{path}", **kw)

    async def post(self, path: str, headers: dict[str, str], **kw: Any) -> httpx.Response:
        return await self.client.post(f"{API}{path}", headers=headers, **kw)


@pytest.fixture
async def harness(settings: Settings, db: Database) -> AsyncIterator[Harness]:
    MockAzureDevOpsClient.reset()
    ado = MockAdo()
    execution = ExecutionService(settings, ado.factory)
    deps = WorkflowDeps(
        settings=settings,
        session_factory=db.session_factory,
        agent=build_analysis_agent(settings),
        execution=execution,
    )
    engine = WorkflowEngine(deps, InMemorySaver())
    worker = Worker(db, settings, JobHandlers(db, settings, engine))
    app = create_app(settings, db)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Harness(settings, db, client, worker, engine, ado, execution)


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
