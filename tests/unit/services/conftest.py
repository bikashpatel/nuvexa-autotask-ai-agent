"""Row factories, a committed-state inspector and fakes for service-layer unit tests.

Every test gets its own database (SQLite file by default, PostgreSQL when TEST_DATABASE_URL is set).
`Factory` writes rows through the test `session`; `Fresh` reads them back through a *separate* session so
assertions see only what a service actually committed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import set_correlation_id
from app.core.security import compute_payload_hash
from app.db.models import (
    ActionApproval,
    ActionExecution,
    AuditEvent,
    ConnectedAccount,
    EmailMessage,
    Job,
    ProposedAction,
    Tenant,
    User,
    WorkflowRun,
)
from app.db.session import Database
from app.domain.enums import (
    ACTION_TARGETS,
    ActionStatus,
    ActionType,
    ApprovalDecision,
    ConnectionProvider,
    ConnectionStatus,
    EmailAnalysisStatus,
    ExecutionStatus,
    UserRole,
    WorkflowStatus,
    WorkflowTrigger,
)
from app.integrations.azure_devops.base import AzureDevOpsClient
from app.integrations.azure_devops.mock import MockAzureDevOpsClient, MockFailMode
from app.schemas.actions import REQUIRED_PERMISSIONS, parse_payload
from app.services.action_policy import idempotency_key, source_reference
from app.services.authz import Actor
from app.services.identity import actor_from_user

RECEIVED_AT = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
SENDER = "requester@example.com"
SUBJECT = "Production login failures"
INTERNET_MESSAGE_ID = "<incident-42@example.com>"
PROJECT = "NUVEXA-Demo"

WI = ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM
REPLY = ActionType.PROPOSE_EMAIL_REPLY
FOLLOW_UP = ActionType.CREATE_FOLLOW_UP_TASK
NO_ACTION = ActionType.MARK_NO_ACTION


# ---------------------------------------------------------------------------------------------
# Canonical payloads (exactly what the policy layer would store: `model_dump(mode="json")`)
# ---------------------------------------------------------------------------------------------


def payload_for(action_type: ActionType, email: EmailMessage, **overrides: Any) -> dict[str, Any]:
    source = source_reference(email).model_dump(mode="json")
    base: dict[str, Any]
    match action_type:
        case ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM:
            base = {
                "kind": "create_azure_devops_work_item",
                "project": PROJECT,
                "work_item_type": "Bug",
                "title": "Investigate production login failures",
                "description": "Users cannot log in after the deployment.",
                "priority": 1,
                "tags": ["login", "category-incident"],
            }
        case ActionType.PROPOSE_EMAIL_REPLY:
            base = {
                "kind": "propose_email_reply",
                "to": [email.sender_address],
                "subject": f"Re: {email.subject}",
                "body": "Thanks, we are investigating.",
                "in_reply_to_internet_message_id": email.internet_message_id,
            }
        case ActionType.CREATE_FOLLOW_UP_TASK:
            base = {
                "kind": "create_follow_up_task",
                "title": "Check back with the requester",
                "description": "Confirm the fix with the customer.",
                "due_at": "2026-10-20T12:00:00+00:00",
            }
        case ActionType.MARK_NO_ACTION:
            base = {"kind": "mark_no_action", "reason": "Informational only."}
    return parse_payload({**base, "source_email": source, **overrides}).model_dump(mode="json")


def hash_of(action_type: ActionType, payload: dict[str, Any]) -> str:
    return compute_payload_hash(action_type.value, ACTION_TARGETS[action_type].value, payload)


# ---------------------------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------------------------


class Factory:
    """Creates committed rows. Keyword arguments override the deterministic defaults."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _save[T](self, obj: T) -> T:
        self.session.add(obj)
        await self.session.commit()
        return obj

    async def tenant(self, name: str = "Other Corp", **kw: Any) -> Tenant:
        return await self._save(Tenant(name=name, slug=kw.pop("slug", f"t-{uuid.uuid4().hex[:10]}"), **kw))

    async def user(
        self, tenant: Tenant, role: UserRole = UserRole.MEMBER, *, email: str | None = None, active: bool = True
    ) -> User:
        return await self._save(
            User(
                tenant_id=tenant.id,
                email=email or f"{role.value}-{uuid.uuid4().hex[:8]}@example.com",
                display_name=f"Test {role.value}",
                role=role,
                is_active=active,
            )
        )

    async def account(
        self,
        user: User,
        provider: ConnectionProvider = ConnectionProvider.DEMO_MAILBOX,
        *,
        status: ConnectionStatus = ConnectionStatus.ACTIVE,
        external_account_id: str | None = None,
        **kw: Any,
    ) -> ConnectedAccount:
        return await self._save(
            ConnectedAccount(
                tenant_id=user.tenant_id,
                user_id=user.id,
                provider=provider,
                external_account_id=external_account_id or f"ext-{uuid.uuid4().hex[:12]}",
                email=user.email,
                display_name=user.display_name,
                granted_scopes=["Mail.Read"],
                status=status,
                **kw,
            )
        )

    async def email(self, account: ConnectedAccount, **kw: Any) -> EmailMessage:
        values: dict[str, Any] = {
            "tenant_id": account.tenant_id,
            "owner_user_id": account.user_id,
            "connected_account_id": account.id,
            "provider_message_id": f"msg-{uuid.uuid4().hex[:12]}",
            "internet_message_id": INTERNET_MESSAGE_ID,
            "change_key": "ck-1",
            "subject": SUBJECT,
            "sender_name": "Dana Requester",
            "sender_address": SENDER,
            "received_at": RECEIVED_AT,
            "body_text": "Production users cannot log in since the 09:00 deployment.",
            "body_preview": "Production users cannot log in",
            "content_hash": "0" * 64,
            "analysis_status": EmailAnalysisStatus.NOT_ANALYZED,
        }
        values.update(kw)
        return await self._save(EmailMessage(**values))

    async def run(
        self, email: EmailMessage, status: WorkflowStatus = WorkflowStatus.PENDING_APPROVAL, **kw: Any
    ) -> WorkflowRun:
        values: dict[str, Any] = {
            "tenant_id": email.tenant_id,
            "owner_user_id": email.owner_user_id,
            "email_message_id": email.id,
            "status": status,
            "current_step": status.value,
            "trigger": WorkflowTrigger.MANUAL,
            "metrics": {},
        }
        values.update(kw)
        return await self._save(WorkflowRun(**values))

    async def action(
        self,
        run: WorkflowRun,
        action_type: ActionType = WI,
        *,
        status: ActionStatus = ActionStatus.PENDING_APPROVAL,
        payload: dict[str, Any] | None = None,
        payload_hash: str | None = None,
        version: int = 1,
        validation_errors: list[str] | None = None,
        slot: int = 0,
        **kw: Any,
    ) -> ProposedAction:
        email = await self.session.get(EmailMessage, run.email_message_id)
        assert email is not None
        data = payload if payload is not None else payload_for(action_type, email)
        values: dict[str, Any] = {
            "tenant_id": run.tenant_id,
            "owner_user_id": run.owner_user_id,
            "workflow_run_id": run.id,
            "email_message_id": run.email_message_id,
            "analysis_attempt": 1,
            "action_type": action_type,
            "target_integration": ACTION_TARGETS[action_type],
            "status": status,
            "version": version,
            "payload": data,
            "payload_hash": payload_hash or hash_of(action_type, data),
            "title": f"{action_type.value} proposal",
            "rationale": "Proposed by the agent.",
            "expected_impact": "Work is tracked.",
            "required_permissions": REQUIRED_PERMISSIONS[action_type],
            "idempotency_key": idempotency_key(run.tenant_id, run.email_message_id, action_type, slot),
            "validation_errors": validation_errors or [],
            "validation_warnings": [],
            "source": "agent",
        }
        values.update(kw)
        return await self._save(ProposedAction(**values))

    async def approval(
        self,
        action: ProposedAction,
        user: User,
        *,
        decision: ApprovalDecision = ApprovalDecision.APPROVED,
        version: int | None = None,
        payload_hash: str | None = None,
        invalidated: bool = False,
        created_at: datetime | None = None,
    ) -> ActionApproval:
        now = datetime.now(UTC)
        return await self._save(
            ActionApproval(
                tenant_id=action.tenant_id,
                action_id=action.id,
                decision=decision,
                decided_by_user_id=user.id,
                action_version=action.version if version is None else version,
                payload_hash=payload_hash or action.payload_hash,
                reason="Looks right.",
                created_at=created_at or now,
                invalidated_at=now if invalidated else None,
                invalidated_reason="test" if invalidated else None,
            )
        )

    async def approved_action(
        self, run: WorkflowRun, approver: User, action_type: ActionType = WI, **kw: Any
    ) -> tuple[ProposedAction, ActionApproval]:
        action = await self.action(run, action_type, status=kw.pop("status", ActionStatus.APPROVED), **kw)
        return action, await self.approval(action, approver)

    async def execution(
        self,
        action: ProposedAction,
        approval: ActionApproval,
        user: User,
        *,
        status: ExecutionStatus,
        attempt: int = 1,
        started_at: datetime | None = None,
        **kw: Any,
    ) -> ActionExecution:
        return await self._save(
            ActionExecution(
                tenant_id=action.tenant_id,
                action_id=action.id,
                approval_id=approval.id,
                requested_by_user_id=user.id,
                attempt_number=attempt,
                status=status,
                action_version=action.version,
                payload_hash=action.payload_hash,
                started_at=started_at or datetime.now(UTC),
                **kw,
            )
        )


# ---------------------------------------------------------------------------------------------
# Committed-state reader
# ---------------------------------------------------------------------------------------------


class Fresh:
    """Reads through a new session: only committed data is visible."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get[T](self, model: type[T], obj_id: uuid.UUID) -> T | None:
        async with self.db.session() as s:
            return await s.get(model, obj_id)

    async def all[T](self, model: type[T], *where: Any) -> list[T]:
        async with self.db.session() as s:
            return list((await s.execute(select(model).where(*where))).scalars().all())

    async def audits(self, event_type: str | None = None, **filters: Any) -> list[AuditEvent]:
        clauses = [getattr(AuditEvent, k) == v for k, v in filters.items()]
        if event_type is not None:
            clauses.append(AuditEvent.event_type == event_type)
        return await self.all(AuditEvent, *clauses)

    async def jobs(self, **filters: Any) -> list[Job]:
        return await self.all(Job, *[getattr(Job, k) == v for k, v in filters.items()])


# ---------------------------------------------------------------------------------------------
# A small tenant: mailbox owner, a second member, approver, admin and viewer
# ---------------------------------------------------------------------------------------------


@dataclass
class World:
    tenant: Tenant
    owner: User
    other: User
    approver: User
    admin: User
    viewer: User
    account: ConnectedAccount
    email: EmailMessage
    run: WorkflowRun

    @staticmethod
    def actor(user: User, ip: str | None = None) -> Actor:
        return actor_from_user(user, ip)


@pytest.fixture
def factory(session: AsyncSession) -> Factory:
    return Factory(session)


@pytest.fixture
def fresh(db: Database) -> Fresh:
    return Fresh(db)


@pytest.fixture
async def world(factory: Factory, tenant: Tenant) -> World:
    owner = await factory.user(tenant, UserRole.MEMBER, email="owner@example.com")
    account = await factory.account(owner)
    email = await factory.email(account)
    return World(
        tenant=tenant,
        owner=owner,
        other=await factory.user(tenant, UserRole.MEMBER, email="other@example.com"),
        approver=await factory.user(tenant, UserRole.APPROVER, email="approver@example.com"),
        admin=await factory.user(tenant, UserRole.ADMIN, email="admin@example.com"),
        viewer=await factory.user(tenant, UserRole.VIEWER, email="viewer@example.com"),
        account=account,
        email=email,
        run=await factory.run(email),
    )


@pytest.fixture(autouse=True)
def _no_correlation_id() -> Iterator[None]:
    set_correlation_id(None)
    yield
    set_correlation_id(None)


# ---------------------------------------------------------------------------------------------
# Azure DevOps: mock client factory with failure injection
# ---------------------------------------------------------------------------------------------


class TrackingMockClient(MockAzureDevOpsClient):
    """The in-memory mock adapter, recording whether the service closed it."""

    def __init__(self, fail_mode: MockFailMode = "none") -> None:
        super().__init__(fail_mode=fail_mode)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class FakeAdo:
    """`AdoClientFactory` handing out `TrackingMockClient`s (or raising `error` before any client exists)."""

    fail_mode: MockFailMode = "none"
    error: BaseException | None = None
    clients: list[TrackingMockClient] = field(default_factory=list)
    actors: list[Actor] = field(default_factory=list)

    async def factory(self, _session: AsyncSession, actor: Actor) -> AzureDevOpsClient:
        self.actors.append(actor)
        if self.error is not None:
            raise self.error
        client = TrackingMockClient(self.fail_mode)
        self.clients.append(client)
        return client

    def items(self) -> list[Any]:
        return MockAzureDevOpsClient().items()


@pytest.fixture
def ado() -> Iterator[FakeAdo]:
    MockAzureDevOpsClient.reset()
    yield FakeAdo()
    MockAzureDevOpsClient.reset()
