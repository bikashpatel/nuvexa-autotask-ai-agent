"""Workflow test harness: real database, InMemorySaver checkpoints, fake agent and execution service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import StateSnapshot
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ConnectedAccount, EmailMessage, Tenant, User, WorkflowRun
from app.db.session import Database
from app.domain.enums import UserRole
from app.integrations.azure_devops.base import AzureDevOpsClient
from app.integrations.azure_devops.mock import MockAzureDevOpsClient
from app.services.authz import Actor
from app.services.execution_service import ExecutionService
from app.services.identity import actor_from_user
from app.workflows.engine import WorkflowEngine
from app.workflows.graph import WorkflowDeps
from app.workflows.state import ReviewDecision
from tests.conftest import UserFactory
from tests.unit.workflows.fakes import FakeAgent, FakeExecution, add_account, add_email, add_run


@dataclass
class WF:
    settings: Settings
    db: Database
    tenant: Tenant
    owner: User
    approver: User
    viewer: User
    account: ConnectedAccount
    agent: FakeAgent
    execution: FakeExecution
    saver: InMemorySaver
    engine: WorkflowEngine
    ado_clients: list[MockAzureDevOpsClient]

    def use_real_execution(self, fail_mode: str = "none") -> ExecutionService:
        async def factory(_session: AsyncSession, _actor: Actor) -> AzureDevOpsClient:
            client = MockAzureDevOpsClient(fail_mode=fail_mode)  # type: ignore[arg-type]
            self.ado_clients.append(client)
            return client

        service = ExecutionService(self.settings, factory)
        self.engine.deps.execution = service  # nodes look the dependency up at call time
        return service

    async def email(self, **kwargs: Any) -> EmailMessage:
        return await add_email(self.db, self.account, **kwargs)

    async def new_run(self, **email_kwargs: Any) -> WorkflowRun:
        return await add_run(self.db, await self.email(**email_kwargs))

    async def start(self, run: WorkflowRun) -> None:
        await self.engine.start(run.id, run.tenant_id)

    async def resume(self, run: WorkflowRun, decision: dict[str, Any]) -> None:
        await self.engine.resume(run.id, run.tenant_id, cast(ReviewDecision, decision))

    async def snapshot(self, run: WorkflowRun) -> StateSnapshot:
        return await self.engine.graph.aget_state(WorkflowEngine._config(run.id))

    def actor(self, user: User | None = None) -> Actor:
        return actor_from_user(user or self.owner)

    def decision(self, kind: str, action: Any = None, user: User | None = None, **extra: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"type": kind, "actor_user_id": str((user or self.owner).id)}
        if action is not None:
            out.update(action_id=str(action.id), version=action.version, payload_hash=action.payload_hash)
        out.update(extra)
        return out


@pytest.fixture
async def wf(settings: Settings, db: Database, session: AsyncSession, tenant: Tenant, make_user: UserFactory) -> WF:
    MockAzureDevOpsClient.reset()
    owner = await make_user(tenant, UserRole.MEMBER)
    approver = await make_user(tenant, UserRole.APPROVER)
    viewer = await make_user(tenant, UserRole.VIEWER)
    account = await add_account(session, owner)
    agent = FakeAgent()
    execution = FakeExecution()
    saver = InMemorySaver()
    deps = WorkflowDeps(
        settings=settings,
        session_factory=db.session_factory,
        agent=agent,
        execution=cast(ExecutionService, execution),
    )
    engine = WorkflowEngine(deps, saver)
    return WF(settings, db, tenant, owner, approver, viewer, account, agent, execution, saver, engine, [])


def new_id() -> str:
    return str(uuid.uuid4())
