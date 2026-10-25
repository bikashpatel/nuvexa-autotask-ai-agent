from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    Conflict,
    IntegrationError,
    IntegrationNotConfigured,
    InvalidStateTransition,
    NotFound,
    StaleVersion,
)
from app.core.timeutil import utcnow
from app.db.models import (
    ActionApproval,
    ActionExecution,
    AppSettings,
    EmailMessage,
    FollowUpTask,
    ProposedAction,
    ReplyDraft,
    WorkflowRun,
)
from app.domain.enums import (
    ActionStatus,
    AuditOutcome,
    EmailAnalysisStatus,
    ExecutionStatus,
    FailureCategory,
    UserRole,
    WorkflowStatus,
)
from app.integrations.azure_devops.base import CreateOutcomeStatus
from app.integrations.azure_devops.mapping import build_create_request, idempotency_tag
from app.integrations.azure_devops.mock import MockAzureDevOpsClient, MockWorkItem
from app.schemas.actions import AzureDevOpsWorkItemPayload, NoActionPayload
from app.schemas.settings import TenantSettings
from app.services import execution_service as execution_module
from app.services.authz import Actor, AuditedPermissionDenied, Perm
from app.services.execution_service import ExecutionService, _category, _sanitize_summary
from tests.unit.services.conftest import (
    FOLLOW_UP,
    NO_ACTION,
    REPLY,
    WI,
    Factory,
    FakeAdo,
    Fresh,
    TrackingMockClient,
    World,
    payload_for,
)

S = ActionStatus
E = ExecutionStatus


@pytest.fixture
def svc(settings: Settings, ado: FakeAdo) -> ExecutionService:
    return ExecutionService(settings, ado.factory)


async def _approved(factory: Factory, world: World, action_type: Any = WI, **kw: Any) -> tuple[ProposedAction, Any]:
    return await factory.approved_action(world.run, world.approver, action_type, **kw)


async def _viewer_owned_run(factory: Factory, world: World) -> WorkflowRun:
    account = await factory.account(world.viewer)
    return await factory.run(await factory.email(account))


async def _tenant_settings(session: AsyncSession, world: World, **ado: Any) -> None:
    ts = TenantSettings.model_validate({"azure_devops": {"project": "NUVEXA-Demo", **ado}})
    session.add(AppSettings(tenant_id=world.tenant.id, settings=ts.model_dump(mode="json"), version=1))
    await session.commit()


async def _precreate_work_item(action: ProposedAction) -> MockWorkItem:
    """An item already carrying the action's idempotency tag (e.g. written by a crashed earlier attempt)."""
    request = build_create_request(
        AzureDevOpsWorkItemPayload.model_validate(action.payload), idempotency_key=action.idempotency_key
    )
    outcome = await MockAzureDevOpsClient().create_work_item_idempotent(request)
    assert outcome.status is CreateOutcomeStatus.CREATED
    [item] = MockAzureDevOpsClient().items()
    return item


def _forged_actor(world: World, user_id: uuid.UUID, role: UserRole = UserRole.ADMIN) -> Actor:
    return Actor.build(
        user_id=user_id, tenant_id=world.tenant.id, role=role, email="forged@example.com", display_name="Forged"
    )


async def _executions(fresh: Fresh, action: ProposedAction | uuid.UUID) -> list[ActionExecution]:
    action_id = action if isinstance(action, uuid.UUID) else action.id
    rows = await fresh.all(ActionExecution, ActionExecution.action_id == action_id)
    return sorted(rows, key=lambda e: e.attempt_number)


async def _action(fresh: Fresh, action: ProposedAction) -> ProposedAction:
    stored = await fresh.get(ProposedAction, action.id)
    assert stored is not None
    return stored


# ---------------------------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------------------------


async def test_preflight_accepts_approved_action(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, ado: FakeAdo
) -> None:
    action, _ = await _approved(factory, world)
    result = await svc.preflight(
        session, world.actor(world.owner), action.id, version=1, payload_hash=action.payload_hash
    )
    assert result.id == action.id
    assert ado.clients == []  # preflight never contacts the integration


async def test_preflight_accepts_failed_action_for_retry(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action, _ = await _approved(factory, world, status=S.FAILED)
    assert (
        await svc.preflight(
            session, world.actor(world.approver), action.id, version=1, payload_hash=action.payload_hash
        )
    ).id == action.id


@pytest.mark.parametrize(("version", "good_hash"), [(2, True), (1, False)])
async def test_preflight_rejects_stale_version_or_hash(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, version: int, good_hash: bool
) -> None:
    action, _ = await _approved(factory, world)
    with pytest.raises(StaleVersion):
        await svc.preflight(
            session,
            world.actor(world.owner),
            action.id,
            version=version,
            payload_hash=action.payload_hash if good_hash else "e" * 64,
        )


async def test_preflight_denies_unapproved_action(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run)
    with pytest.raises(AuditedPermissionDenied) as info:
        await svc.preflight(session, world.actor(world.owner), action.id, version=1, payload_hash=action.payload_hash)
    assert info.value.message == "The action has not been approved."
    assert info.value.resource_type == "action"
    assert info.value.resource_id == str(action.id)
    assert info.value.actor.user_id == world.owner.id


@pytest.mark.parametrize("status", [S.REJECTED, S.CANCELLED, S.COMPLETED, S.SUPERSEDED, S.EXECUTING, S.UNCERTAIN])
async def test_preflight_rejects_non_executable_status(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, status: ActionStatus
) -> None:
    action = await factory.action(world.run, status=status)
    with pytest.raises(InvalidStateTransition, match=f"status '{status}' cannot be executed"):
        await svc.preflight(session, world.actor(world.owner), action.id, version=1, payload_hash=action.payload_hash)


async def test_preflight_requires_current_approval(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run, status=S.APPROVED)
    await factory.approval(action, world.approver, invalidated=True)
    with pytest.raises(AuditedPermissionDenied, match="no valid approval for its current version"):
        await svc.preflight(session, world.actor(world.owner), action.id, version=1, payload_hash=action.payload_hash)


async def test_preflight_actor_checks(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action, _ = await _approved(factory, world)
    kw: dict[str, Any] = {"version": 1, "payload_hash": action.payload_hash}

    with pytest.raises(NotFound):
        await svc.preflight(session, world.actor(world.other), action.id, **kw)
    assert (await svc.preflight(session, world.actor(world.admin), action.id, **kw)).id == action.id

    with pytest.raises(AuditedPermissionDenied, match="not active in this tenant"):
        await svc.preflight(session, _forged_actor(world, uuid.uuid4()), action.id, **kw)  # no such user
    outsider = await factory.user(await factory.tenant(), UserRole.ADMIN)
    with pytest.raises(AuditedPermissionDenied, match="not active in this tenant"):
        await svc.preflight(session, _forged_actor(world, outsider.id), action.id, **kw)  # user of another tenant

    viewer_action, _ = await factory.approved_action(await _viewer_owned_run(factory, world), world.approver)
    with pytest.raises(AuditedPermissionDenied) as info:
        await svc.preflight(
            session, world.actor(world.viewer), viewer_action.id, version=1, payload_hash=viewer_action.payload_hash
        )
    assert info.value.permission == Perm.ACTION_EXECUTE


async def test_preflight_denies_deactivated_user(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action, _ = await _approved(factory, world)
    actor = world.actor(world.owner)  # issued while the user was active
    world.owner.is_active = False
    await session.commit()
    with pytest.raises(AuditedPermissionDenied, match="not active"):
        await svc.preflight(session, actor, action.id, version=1, payload_hash=action.payload_hash)


# ---------------------------------------------------------------------------------------------
# execute: Azure DevOps (mock adapter with failure injection)
# ---------------------------------------------------------------------------------------------


async def test_execute_creates_work_item_and_records_everything(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    action, approval = await _approved(factory, world)
    actor = world.actor(world.owner, ip="192.0.2.5")
    execution = await svc.execute(
        session, actor, action.id, expected_version=1, expected_payload_hash=action.payload_hash
    )

    [item] = ado.items()
    assert item.project == "NUVEXA-Demo"
    assert item.fields["System.Title"] == "Investigate production login failures"
    assert idempotency_tag(action.idempotency_key) in item.tags

    [stored] = await _executions(fresh, action)
    assert stored.id == execution.id
    assert stored.status is E.SUCCEEDED
    assert stored.attempt_number == 1
    assert stored.approval_id == approval.id
    assert stored.requested_by_user_id == world.owner.id
    assert (stored.action_version, stored.payload_hash) == (1, action.payload_hash)
    assert stored.external_id == str(item.id)
    assert stored.external_url == item.url
    assert stored.external_url.startswith("mock://")  # type: ignore[union-attr]
    assert stored.deduplicated is False
    assert stored.retry_count == 0
    assert stored.provider_status_code == 200
    assert stored.error_category is None
    assert stored.error_message is None
    assert stored.finished_at is not None
    assert stored.latency_ms is not None
    assert stored.latency_ms >= 0
    assert set(stored.provider_response) <= {"id", "rev", "url", "state"}  # "mock" marker is not persisted
    assert stored.provider_response["id"] == item.id

    done = await _action(fresh, action)
    assert done.status is S.COMPLETED
    assert done.external_id == str(item.id)
    assert done.external_url == item.url
    assert done.executed_at is not None
    assert done.last_error is None

    [started] = await fresh.audits("execution_started", action_id=action.id)
    assert started.details == {"attempt": 1, "approval_id": str(approval.id), "version": 1, "target": "azure_devops"}
    assert started.ip_address == "192.0.2.5"
    [write] = await fresh.audits("external_write", action_id=action.id)
    assert write.outcome is AuditOutcome.SUCCESS
    assert write.details == {
        "target": "azure_devops",
        "result": "created",
        "external_id": str(item.id),
        "deduplicated": False,
        "retry_count": 0,
        "error_category": None,
        "attempt": 1,
    }
    assert [c.closed for c in ado.clients] == [True]
    assert ado.actors[0].user_id == world.owner.id  # credentials are bound to the executing user


async def test_execute_is_idempotent_once_completed(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    action, _ = await _approved(factory, world)
    first = await svc.execute(session, world.actor(world.owner), action.id)
    again = await svc.execute(session, world.actor(world.owner), action.id)
    assert again.id == first.id
    assert len(ado.clients) == 1  # no second call to Azure DevOps
    assert len(ado.items()) == 1
    assert len(await _executions(fresh, action)) == 1
    assert len(await fresh.audits("external_write")) == 1


async def test_transient_failure_is_retried_within_the_adapter(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, ado: FakeAdo
) -> None:
    ado.fail_mode = "transient_once"
    action, _ = await _approved(factory, world)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.status is E.SUCCEEDED
    assert execution.retry_count == 1
    assert len(ado.items()) == 1


async def test_timeout_after_create_is_recovered_without_duplicate(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, ado: FakeAdo
) -> None:
    ado.fail_mode = "timeout_after_create_once"
    action, _ = await _approved(factory, world)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.status is E.SUCCEEDED
    assert len(ado.items()) == 1
    assert "recovered_after_ambiguous_error" not in execution.provider_response  # outside the allow-list


async def test_unconfirmable_outcome_is_uncertain_then_reconciled(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    ado.fail_mode = "lookup_unavailable_after_timeout"
    action, approval = await _approved(factory, world)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.status is E.UNCERTAIN
    assert execution.error_category is FailureCategory.AMBIGUOUS
    assert "Reconcile before retrying" in (execution.error_message or "")
    assert (await _action(fresh, action)).status is S.UNCERTAIN
    [write] = await fresh.audits("external_write")
    assert write.outcome is AuditOutcome.FAILURE
    assert write.details["result"] == "uncertain"
    assert write.details["error_category"] == "ambiguous"
    [item] = ado.items()  # the write did happen

    with pytest.raises(InvalidStateTransition):  # an uncertain action is never re-sent blindly
        await svc.execute(session, world.actor(world.owner), action.id)

    ado.fail_mode = "none"
    reconciled = await svc.reconcile(session, world.actor(world.owner), action.id)
    assert reconciled.status is E.SUCCEEDED
    assert reconciled.attempt_number == 2
    assert reconciled.approval_id == approval.id
    assert reconciled.deduplicated is True
    assert reconciled.external_id == str(item.id)
    assert reconciled.provider_response == {"reconciled": True, "reason": "manual reconcile"}
    done = await _action(fresh, action)
    assert done.status is S.COMPLETED
    assert done.external_id == str(item.id)
    assert done.last_error is None
    assert done.executed_at is not None
    [audit] = await fresh.audits("execution_reconciled")
    assert audit.details == {"found": True, "external_id": str(item.id), "reason": "manual reconcile"}
    assert len(ado.items()) == 1
    assert all(c.closed for c in ado.clients)


async def test_permanent_validation_error_fails_then_retry_succeeds(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    ado.fail_mode = "permanent_validation"
    action, _ = await _approved(factory, world)
    failed = await svc.execute(session, world.actor(world.owner), action.id)
    assert failed.status is E.FAILED
    assert failed.error_category is FailureCategory.VALIDATION
    assert failed.provider_status_code == 400
    assert failed.retry_count == 0
    assert ado.items() == []
    stored = await _action(fresh, action)
    assert stored.status is S.FAILED
    assert "TF401320" in (stored.last_error or "")
    assert stored.executed_at is None

    ado.fail_mode = "none"
    retried = await svc.execute(session, world.actor(world.owner), action.id)  # failed → executing is allowed
    assert retried.status is E.SUCCEEDED
    assert retried.attempt_number == 2
    assert len(ado.items()) == 1
    assert (await _action(fresh, action)).status is S.COMPLETED


async def test_authorization_error_fails_without_writing(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, ado: FakeAdo
) -> None:
    ado.fail_mode = "auth_error"
    action, _ = await _approved(factory, world)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.status is E.FAILED
    assert execution.error_category is FailureCategory.AUTHORIZATION
    assert "nothing was created" in (execution.error_message or "")
    assert ado.items() == []
    assert action.status is S.FAILED


async def test_existing_item_with_tag_is_deduplicated(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    action, _ = await _approved(factory, world)
    item = await _precreate_work_item(action)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.status is E.SUCCEEDED
    assert execution.deduplicated is True
    assert execution.external_id == str(item.id)
    assert len(ado.items()) == 1
    [write] = await fresh.audits("external_write")
    assert write.details["result"] == "existing"
    assert write.details["deduplicated"] is True


@pytest.mark.parametrize(
    ("error", "category", "status"),
    [
        (IntegrationNotConfigured("Connect your Azure DevOps account"), FailureCategory.CONFIGURATION, None),
        (IntegrationError("token revoked", category="authorization", status=401), FailureCategory.AUTHORIZATION, 401),
    ],
)
async def test_integration_error_before_any_write_fails_the_execution(
    svc: ExecutionService,
    session: AsyncSession,
    factory: Factory,
    world: World,
    ado: FakeAdo,
    error: IntegrationError,
    category: FailureCategory,
    status: int | None,
) -> None:
    ado.error = error
    action, _ = await _approved(factory, world)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.status is E.FAILED
    assert execution.error_category is category
    assert execution.error_message == error.message
    assert execution.provider_status_code == status
    assert action.status is S.FAILED
    assert action.last_error == error.message


async def test_unexpected_error_during_external_write_is_uncertain(
    svc: ExecutionService,
    session: AsyncSession,
    factory: Factory,
    world: World,
    ado: FakeAdo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(self: TrackingMockClient, request: Any) -> Any:
        raise RuntimeError("socket closed mid-request")

    monkeypatch.setattr(TrackingMockClient, "create_work_item_idempotent", explode)
    action, _ = await _approved(factory, world)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.status is E.UNCERTAIN
    assert execution.error_category is FailureCategory.AMBIGUOUS
    assert execution.error_message == "RuntimeError"  # exception text (could hold data) is not stored
    assert action.status is S.UNCERTAIN
    assert [c.closed for c in ado.clients] == [True]


async def test_unexpected_error_in_local_write_is_a_plain_failure(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(*_args: Any) -> Any:
        raise KeyError("draft")

    monkeypatch.setattr(ExecutionService, "_store_reply_draft", explode)
    action, _ = await _approved(factory, world, REPLY)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.status is E.FAILED
    assert execution.error_category is FailureCategory.INTERNAL
    assert execution.error_message == "KeyError"
    assert action.status is S.FAILED


# ---------------------------------------------------------------------------------------------
# execute: local targets
# ---------------------------------------------------------------------------------------------


async def test_reply_is_stored_as_draft_never_sent(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    action, _ = await _approved(factory, world, REPLY)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    [draft] = await fresh.all(ReplyDraft)
    assert draft.action_id == action.id
    assert draft.owner_user_id == world.owner.id
    assert draft.email_message_id == world.email.id
    assert draft.to_recipients == ["requester@example.com"]
    assert draft.subject == "Re: Production login failures"
    assert draft.body == "Thanks, we are investigating."
    assert execution.status is E.SUCCEEDED
    assert execution.provider_response == {"reply_draft_id": str(draft.id), "sent": False}
    assert execution.deduplicated is False
    assert execution.external_id is None
    assert execution.latency_ms is not None
    assert (await _action(fresh, action)).status is S.COMPLETED
    assert ado.clients == []
    [write] = await fresh.audits("external_write")
    assert write.details["target"] == "reply_drafts"


async def test_existing_reply_draft_is_reused(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action, _ = await _approved(factory, world, REPLY)
    session.add(
        ReplyDraft(
            tenant_id=world.tenant.id,
            owner_user_id=world.owner.id,
            action_id=action.id,
            email_message_id=world.email.id,
            to_recipients=["requester@example.com"],
            subject="Re: earlier",
            body="Written by a crashed attempt",
        )
    )
    await session.commit()
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    [draft] = await fresh.all(ReplyDraft)
    assert execution.deduplicated is True
    assert execution.provider_response["reply_draft_id"] == str(draft.id)
    assert draft.body == "Written by a crashed attempt"


async def test_follow_up_task_is_created_once(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action, _ = await _approved(factory, world, FOLLOW_UP)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    [task] = await fresh.all(FollowUpTask)
    assert task.action_id == action.id
    assert task.owner_user_id == world.owner.id
    assert task.title == "Check back with the requester"
    assert task.description == "Confirm the fix with the customer."
    assert task.due_at is not None
    assert task.due_at.isoformat() == "2026-10-20T12:00:00+00:00"
    assert execution.provider_response == {"follow_up_task_id": str(task.id)}
    assert execution.deduplicated is False


async def test_existing_follow_up_task_is_reused(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action, _ = await _approved(factory, world, FOLLOW_UP)
    session.add(
        FollowUpTask(
            tenant_id=world.tenant.id,
            owner_user_id=world.owner.id,
            action_id=action.id,
            email_message_id=world.email.id,
            title="Earlier attempt",
        )
    )
    await session.commit()
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    [task] = await fresh.all(FollowUpTask)
    assert execution.deduplicated is True
    assert execution.provider_response == {"follow_up_task_id": str(task.id)}
    assert task.title == "Earlier attempt"


async def test_no_action_marks_the_email_done(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    action, _ = await _approved(factory, world, NO_ACTION)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.provider_response == {"no_action": True}
    email = await fresh.get(EmailMessage, world.email.id)
    assert email is not None
    assert email.analysis_status is EmailAnalysisStatus.DONE
    assert (await _action(fresh, action)).status is S.COMPLETED
    assert ado.clients == []


async def test_dispatch_no_action_without_email(
    svc: ExecutionService, factory: Factory, session: AsyncSession, world: World
) -> None:
    action, _ = await _approved(factory, world, NO_ACTION)
    payload = NoActionPayload.model_validate(action.payload)
    outcome = await svc._dispatch(session, world.actor(world.owner), action, payload, None)
    assert outcome.status is CreateOutcomeStatus.CREATED
    assert outcome.response_summary == {"no_action": True}


# ---------------------------------------------------------------------------------------------
# execute: gates before any write
# ---------------------------------------------------------------------------------------------


async def test_execute_denies_unapproved_action(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    action = await factory.action(world.run)
    with pytest.raises(AuditedPermissionDenied, match="has not been approved"):
        await svc.execute(session, world.actor(world.owner), action.id)
    assert ado.clients == []
    assert await _executions(fresh, action) == []


@pytest.mark.parametrize("status", [S.REJECTED, S.CANCELLED, S.SUPERSEDED, S.UNCERTAIN, S.COMPLETED])
async def test_execute_blocked_status_is_audited(
    svc: ExecutionService,
    session: AsyncSession,
    factory: Factory,
    world: World,
    fresh: Fresh,
    ado: FakeAdo,
    status: ActionStatus,
) -> None:
    action = await factory.action(world.run, status=status)  # COMPLETED here has no success record
    await factory.approval(action, world.approver)
    with pytest.raises(InvalidStateTransition, match=f"status '{status}' cannot be executed"):
        await svc.execute(session, world.actor(world.owner), action.id)
    [audit] = await fresh.audits("execution_blocked", action_id=action.id)
    assert audit.outcome is AuditOutcome.DENIED
    assert audit.details == {"status": status.value}
    assert audit.actor_user_id == world.owner.id
    assert ado.clients == []


CLOSED_RUNS = [WorkflowStatus.CANCELLED, WorkflowStatus.REJECTED]


async def _in_closed_run(factory: Factory, world: World, run_status: WorkflowStatus, status: ActionStatus) -> Any:
    """Inconsistent state set up directly: an approved/failed action with a live approval in a closed run."""
    action, _ = await _approved(factory, world, status=status)
    world.run.status = run_status
    await factory.session.commit()
    return action


@pytest.mark.parametrize("status", [S.APPROVED, S.FAILED])
@pytest.mark.parametrize("run_status", CLOSED_RUNS)
async def test_execute_refuses_actions_of_a_closed_workflow(
    svc: ExecutionService,
    session: AsyncSession,
    factory: Factory,
    world: World,
    fresh: Fresh,
    ado: FakeAdo,
    run_status: WorkflowStatus,
    status: ActionStatus,
) -> None:
    action = await _in_closed_run(factory, world, run_status, status)
    with pytest.raises(InvalidStateTransition, match=f"workflow is {run_status}"):
        await svc.execute(session, world.actor(world.owner), action.id)
    [audit] = await fresh.audits("execution_blocked", action_id=action.id)
    assert (audit.outcome, audit.actor_user_id) == (AuditOutcome.DENIED, world.owner.id)
    assert audit.details == {"status": status.value, "workflow_status": run_status.value}
    assert ado.clients == []
    assert await _executions(fresh, action) == []
    assert (await _action(fresh, action)).status is status


@pytest.mark.parametrize("run_status", CLOSED_RUNS)
async def test_preflight_refuses_actions_of_a_closed_workflow(
    svc: ExecutionService,
    session: AsyncSession,
    factory: Factory,
    world: World,
    fresh: Fresh,
    run_status: WorkflowStatus,
) -> None:
    action = await _in_closed_run(factory, world, run_status, S.APPROVED)
    with pytest.raises(InvalidStateTransition, match=f"workflow is {run_status}"):
        await svc.preflight(
            session, world.actor(world.owner), action.id, version=action.version, payload_hash=action.payload_hash
        )
    [audit] = await fresh.audits("execution_blocked", action_id=action.id)
    assert audit.details == {"status": "approved", "workflow_status": run_status.value}


async def test_failed_actions_of_a_failed_workflow_stay_retryable(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    action = await _in_closed_run(factory, world, WorkflowStatus.FAILED, S.FAILED)  # FAILED is not closed
    actor = world.actor(world.owner)
    await svc.preflight(session, actor, action.id, version=action.version, payload_hash=action.payload_hash)
    execution = await svc.execute(session, actor, action.id)
    assert execution.status is E.SUCCEEDED
    assert len(ado.items()) == 1
    assert await fresh.audits("execution_blocked") == []


@pytest.mark.parametrize("field", ["version", "hash"])
async def test_execute_rejects_stale_expectations(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, ado: FakeAdo, field: str
) -> None:
    action, _ = await _approved(factory, world)
    kw: dict[str, Any] = {"expected_version": 1, "expected_payload_hash": action.payload_hash}
    kw["expected_version" if field == "version" else "expected_payload_hash"] = 2 if field == "version" else "0" * 64
    with pytest.raises(StaleVersion):
        await svc.execute(session, world.actor(world.owner), action.id, **kw)
    assert ado.clients == []


async def test_execute_refuses_tampered_payload(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    action, _ = await _approved(factory, world)
    action.payload = {**action.payload, "project": "Attacker-Project"}  # approved hash no longer matches
    await session.commit()
    with pytest.raises(Conflict, match="does not match its approved hash"):
        await svc.execute(session, world.actor(world.owner), action.id)
    [audit] = await fresh.audits("payload_integrity_failure", action_id=action.id)
    assert audit.outcome is AuditOutcome.DENIED
    assert ado.clients == []
    assert (await _action(fresh, action)).status is S.APPROVED


async def test_execute_requires_approval_for_current_version(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, ado: FakeAdo
) -> None:
    action = await factory.action(world.run, status=S.APPROVED, version=2)
    await factory.approval(action, world.approver, version=1)  # approval of an older version
    with pytest.raises(AuditedPermissionDenied, match="no valid approval"):
        await svc.execute(session, world.actor(world.owner), action.id)
    assert ado.clients == []


async def test_execute_denies_inactive_or_foreign_actor(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, ado: FakeAdo
) -> None:
    action, _ = await _approved(factory, world)
    outsider = await factory.user(await factory.tenant(), UserRole.ADMIN)
    with pytest.raises(AuditedPermissionDenied, match="not active in this tenant"):
        await svc.execute(session, _forged_actor(world, outsider.id), action.id)
    actor = world.actor(world.owner)
    world.owner.is_active = False
    await session.commit()
    with pytest.raises(AuditedPermissionDenied, match="not active"):
        await svc.execute(session, actor, action.id)
    with pytest.raises(NotFound):
        await svc.execute(session, world.actor(world.other), action.id)
    assert ado.clients == []


@pytest.mark.parametrize("lagging", [S.APPROVED, S.FAILED])
async def test_execute_repairs_status_after_previous_success(
    svc: ExecutionService,
    session: AsyncSession,
    factory: Factory,
    world: World,
    fresh: Fresh,
    ado: FakeAdo,
    lagging: ActionStatus,
) -> None:
    action, approval = await _approved(factory, world, status=lagging, last_error="stale error")
    finished = utcnow() - timedelta(minutes=3)
    previous = await factory.execution(
        action,
        approval,
        world.owner,
        status=E.SUCCEEDED,
        external_id="1001",
        external_url="https://dev.azure.com/x/_workitems/edit/1001",
        finished_at=finished,
    )
    result = await svc.execute(session, world.actor(world.owner), action.id)
    assert result.id == previous.id
    stored = await _action(fresh, action)
    assert stored.status is S.COMPLETED
    assert (stored.external_id, stored.external_url, stored.last_error) == ("1001", previous.external_url, None)
    assert stored.executed_at == finished
    assert ado.clients == []
    assert len(await _executions(fresh, action)) == 1
    # Invariant 9: the status change is audited (it used to be a silent update).
    [audit] = await fresh.audits("execution_status_repaired", action_id=action.id)
    assert (audit.outcome, audit.actor_user_id) == (AuditOutcome.SUCCESS, world.owner.id)
    assert audit.details == {"from_status": lagging.value, "execution_id": str(previous.id), "external_id": "1001"}


async def test_revalidation_policy_failure_records_failed_execution(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    action, approval = await _approved(factory, world)
    await _tenant_settings(session, world, allowed_projects=["Customer-Portal"])  # tightened after approval
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.status is E.FAILED
    assert execution.error_category is FailureCategory.VALIDATION
    assert execution.error_message == "Project 'NUVEXA-Demo' is not in the allowed project list."
    assert execution.approval_id == approval.id
    assert execution.latency_ms == 0
    assert execution.attempt_number == 1
    stored = await _action(fresh, action)
    assert stored.status is S.FAILED
    assert stored.last_error == execution.error_message
    [audit] = await fresh.audits("external_write")
    assert audit.outcome is AuditOutcome.FAILURE
    assert audit.details == {
        "stage": "revalidation",
        "issues": ["Project 'NUVEXA-Demo' is not in the allowed project list."],
    }
    assert await fresh.audits("execution_started") == []
    assert ado.clients == []


async def test_revalidation_failure_on_retry_keeps_failed_status(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action, approval = await _approved(factory, world, status=S.FAILED)
    await factory.execution(action, approval, world.owner, status=E.FAILED)
    await _tenant_settings(session, world, allowed_work_item_types=["Task"])
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.attempt_number == 2
    assert execution.error_message == "Work item type 'Bug' is not allowed."
    assert (await _action(fresh, action)).status is S.FAILED


@pytest.mark.parametrize("kind_mismatch", [False, True])
async def test_revalidation_schema_failure(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, ado: FakeAdo, kind_mismatch: bool
) -> None:
    payload = payload_for(FOLLOW_UP, world.email) if kind_mismatch else {**payload_for(WI, world.email), "priority": 7}
    action, _ = await _approved(factory, world, WI, payload=payload)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.status is E.FAILED
    assert execution.error_category is FailureCategory.VALIDATION
    assert (execution.error_message or "").startswith("Payload failed revalidation: ")
    assert ado.clients == []


async def test_revalidation_catches_altered_reply_recipient(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    payload = payload_for(REPLY, world.email, to=["attacker@example.net"])
    action, _ = await _approved(factory, world, REPLY, payload=payload)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.error_message == "Reply recipients must be exactly the original sender."
    assert await fresh.all(ReplyDraft) == []


async def test_reserved_field_is_a_validation_failure_not_an_uncertain_write(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    # Regression: a misconfigured allow-list let `System.AssignedTo` through policy; the request builder then
    # raised inside the write path and the action was marked UNCERTAIN although nothing was sent.
    await _tenant_settings(session, world, allowed_additional_fields=["System.AssignedTo"])
    payload = payload_for(WI, world.email, additional_fields={"System.AssignedTo": "ceo@example.com"})
    action, _ = await _approved(factory, world, WI, payload=payload)
    execution = await svc.execute(session, world.actor(world.owner), action.id)
    assert execution.status is E.FAILED
    assert execution.error_category is FailureCategory.VALIDATION
    assert "System.AssignedTo" in (execution.error_message or "")
    assert (await _action(fresh, action)).status is S.FAILED
    assert ado.clients == []
    assert ado.items() == []


async def test_concurrent_claim_loses_with_conflict(
    svc: ExecutionService,
    session: AsyncSession,
    factory: Factory,
    world: World,
    fresh: Fresh,
    ado: FakeAdo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action, _ = await _approved(factory, world)
    action_id = action.id
    original = ExecutionService._next_attempt

    async def racing(s: AsyncSession, a: ProposedAction) -> int:
        # Another executor edits/claims the row between our checks and the conditional UPDATE.
        await s.execute(
            update(ProposedAction)
            .where(ProposedAction.id == a.id)
            .values(version=a.version + 1)
            .execution_options(synchronize_session=False)
        )
        return await original(s, a)

    monkeypatch.setattr(ExecutionService, "_next_attempt", staticmethod(racing))
    with pytest.raises(Conflict, match="modified or executed concurrently"):
        await svc.execute(session, world.actor(world.owner), action_id)
    stored = await fresh.get(ProposedAction, action_id)
    assert stored is not None
    assert (stored.status, stored.version) == (S.APPROVED, 1)  # our transaction was rolled back
    assert await _executions(fresh, action_id) == []
    assert await fresh.audits("execution_started") == []
    assert ado.clients == []


async def test_execute_while_another_execution_is_live(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, ado: FakeAdo
) -> None:
    action, approval = await _approved(factory, world, status=S.EXECUTING)
    await factory.execution(action, approval, world.owner, status=E.RUNNING, started_at=utcnow())
    with pytest.raises(Conflict, match="already executing"):
        await svc.execute(session, world.actor(world.owner), action.id)
    assert ado.clients == []


async def test_execute_with_stale_lease_reconciles_instead_of_resending(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    action, approval = await _approved(factory, world, status=S.EXECUTING)
    crashed = await factory.execution(
        action, approval, world.owner, status=E.RUNNING, started_at=utcnow() - timedelta(minutes=10)
    )
    result = await svc.execute(session, world.actor(world.owner), action.id)
    assert result.status is E.FAILED  # the lookup confirmed nothing was created
    assert result.attempt_number == 2
    assert result.error_category is FailureCategory.AMBIGUOUS
    assert result.error_message == "Reconciled: the external item does not exist; safe to retry."
    assert result.provider_response == {"reconciled": True, "reason": "stale execution lease"}
    old = await fresh.get(ActionExecution, crashed.id)
    assert old is not None
    assert old.status is E.UNCERTAIN
    assert old.error_message == "Outcome unknown (stale execution lease)."
    assert old.finished_at is not None
    stored = await _action(fresh, action)
    assert stored.status is S.FAILED
    assert stored.last_error == result.error_message
    assert ado.items() == []  # nothing was re-sent
    [audit] = await fresh.audits("execution_reconciled")
    assert audit.details == {"found": False, "external_id": None, "reason": "stale execution lease"}


# ---------------------------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [S.APPROVED, S.COMPLETED, S.FAILED, S.PENDING_APPROVAL])
async def test_reconcile_only_for_uncertain_or_executing(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, status: ActionStatus
) -> None:
    action = await factory.action(world.run, status=status)
    with pytest.raises(InvalidStateTransition, match="Only uncertain actions can be reconciled"):
        await svc.reconcile(session, world.actor(world.owner), action.id)


async def test_reconcile_refuses_live_execution(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action, approval = await _approved(factory, world, status=S.EXECUTING)
    await factory.execution(action, approval, world.owner, status=E.RUNNING)
    with pytest.raises(Conflict, match="still executing"):
        await svc.reconcile(session, world.actor(world.owner), action.id)


async def test_reconcile_needs_a_previous_attempt(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action, _ = await _approved(factory, world, status=S.UNCERTAIN)
    with pytest.raises(Conflict, match="No execution attempt exists"):
        await svc.reconcile(session, world.actor(world.owner), action.id)


async def test_reconcile_finds_existing_work_item(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    action, approval = await _approved(factory, world, status=S.UNCERTAIN)
    await factory.execution(action, approval, world.owner, status=E.UNCERTAIN)
    item = await _precreate_work_item(action)
    result = await svc.reconcile(session, world.actor(world.approver), action.id)
    assert result.status is E.SUCCEEDED
    assert result.external_id == str(item.id)
    assert result.external_url == item.url
    assert result.requested_by_user_id == world.approver.id
    stored = await _action(fresh, action)
    assert stored.status is S.COMPLETED
    assert stored.external_url == item.url
    assert [c.closed for c in ado.clients] == [True]


async def test_reconcile_confirms_absence(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    action, approval = await _approved(factory, world, status=S.UNCERTAIN)
    await factory.execution(action, approval, world.owner, status=E.UNCERTAIN, attempt=3)
    result = await svc.reconcile(session, world.actor(world.owner), action.id)
    assert result.status is E.FAILED
    assert result.attempt_number == 4
    assert result.deduplicated is False
    assert result.external_id is None
    stored = await _action(fresh, action)
    assert stored.status is S.FAILED
    assert stored.last_error == "Reconciled: the external item does not exist; safe to retry."


async def test_reconcile_lookup_failure_keeps_uncertain_committed(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    ado.fail_mode = "auth_error"
    action, approval = await _approved(factory, world, status=S.EXECUTING)
    crashed = await factory.execution(
        action, approval, world.owner, status=E.RUNNING, started_at=utcnow() - timedelta(hours=1)
    )
    with pytest.raises(IntegrationError) as info:
        await svc.reconcile(session, world.actor(world.owner), action.id)
    assert info.value.category == FailureCategory.AUTHORIZATION
    assert (await _action(fresh, action)).status is S.UNCERTAIN  # not rolled back to "executing"
    old = await fresh.get(ActionExecution, crashed.id)
    assert old is not None
    assert old.status is E.UNCERTAIN
    assert old.error_message == "Outcome unknown (manual reconcile)."
    assert len(await _executions(fresh, action)) == 1
    assert await fresh.audits("execution_reconciled") == []
    assert [c.closed for c in ado.clients] == [True]


@pytest.mark.parametrize(
    ("action_type", "with_row", "expected"),
    [
        (REPLY, True, S.COMPLETED),
        (REPLY, False, S.FAILED),
        (FOLLOW_UP, True, S.COMPLETED),
        (FOLLOW_UP, False, S.FAILED),
        (NO_ACTION, False, S.COMPLETED),
    ],
)
async def test_reconcile_local_targets_check_their_own_rows(
    svc: ExecutionService,
    session: AsyncSession,
    factory: Factory,
    world: World,
    fresh: Fresh,
    ado: FakeAdo,
    action_type: Any,
    with_row: bool,
    expected: ActionStatus,
) -> None:
    action, approval = await _approved(factory, world, action_type, status=S.UNCERTAIN)
    await factory.execution(action, approval, world.owner, status=E.UNCERTAIN)
    common = {
        "tenant_id": world.tenant.id,
        "owner_user_id": world.owner.id,
        "action_id": action.id,
        "email_message_id": world.email.id,
    }
    if with_row and action_type is REPLY:
        session.add(ReplyDraft(**common, to_recipients=["requester@example.com"], subject="Re", body="b"))
    if with_row and action_type is FOLLOW_UP:
        session.add(FollowUpTask(**common, title="t"))
    await session.commit()
    result = await svc.reconcile(session, world.actor(world.owner), action.id)
    assert (await _action(fresh, action)).status is expected
    assert result.deduplicated is (expected is S.COMPLETED)
    assert ado.clients == []


async def test_reconcile_permissions(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action, _ = await _approved(factory, world, status=S.UNCERTAIN)
    with pytest.raises(NotFound):
        await svc.reconcile(session, world.actor(world.other), action.id)
    viewer_action, _ = await factory.approved_action(
        await _viewer_owned_run(factory, world), world.approver, status=S.UNCERTAIN
    )
    with pytest.raises(AuditedPermissionDenied) as info:
        await svc.reconcile(session, world.actor(world.viewer), viewer_action.id)
    assert info.value.permission == Perm.ACTION_EXECUTE


# ---------------------------------------------------------------------------------------------
# reconcile pre-flight (API) and execution liveness
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "running_since_seconds"),
    [(S.UNCERTAIN, None), (S.EXECUTING, 310), (S.EXECUTING, None)],  # stale lease, or no running row at all
)
async def test_reconcile_preflight_accepts_unknown_outcomes(
    svc: ExecutionService,
    session: AsyncSession,
    factory: Factory,
    world: World,
    status: ActionStatus,
    running_since_seconds: int | None,
) -> None:
    action, approval = await _approved(factory, world, status=status)
    if running_since_seconds is not None:
        started = utcnow() - timedelta(seconds=running_since_seconds)
        await factory.execution(action, approval, world.owner, status=E.RUNNING, started_at=started)
    assert not await svc.has_live_execution(session, action)
    checked = await svc.reconcile_preflight(session, world.actor(world.owner), action.id)
    assert checked.id == action.id


async def test_reconcile_preflight_refuses_a_live_execution(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action, approval = await _approved(factory, world, status=S.EXECUTING)
    started = utcnow() - timedelta(seconds=svc.settings.job_lease_seconds - 30)  # still inside the lease
    await factory.execution(action, approval, world.owner, status=E.RUNNING, started_at=started)
    assert await svc.has_live_execution(session, action)
    with pytest.raises(Conflict, match="still executing"):
        await svc.reconcile_preflight(session, world.actor(world.owner), action.id)


@pytest.mark.parametrize("status", [S.APPROVED, S.COMPLETED, S.FAILED, S.PENDING_APPROVAL, S.CANCELLED])
async def test_reconcile_preflight_refuses_settled_actions(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, status: ActionStatus
) -> None:
    action = await factory.action(world.run, status=status)
    with pytest.raises(InvalidStateTransition, match="Only uncertain actions can be reconciled"):
        await svc.reconcile_preflight(session, world.actor(world.owner), action.id)


async def test_reconcile_preflight_permissions(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action, _ = await _approved(factory, world, status=S.UNCERTAIN)
    with pytest.raises(NotFound):
        await svc.reconcile_preflight(session, world.actor(world.other), action.id)
    viewer_action, _ = await factory.approved_action(
        await _viewer_owned_run(factory, world), world.approver, status=S.UNCERTAIN
    )
    with pytest.raises(AuditedPermissionDenied) as info:
        await svc.reconcile_preflight(session, world.actor(world.viewer), viewer_action.id)
    assert info.value.permission == Perm.ACTION_EXECUTE
    assert (await svc.reconcile_preflight(session, world.actor(world.approver), action.id)).id == action.id


async def test_liveness_ignores_other_actions_and_finished_executions(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action, approval = await _approved(factory, world, status=S.EXECUTING)
    other, other_approval = await _approved(factory, world, REPLY, status=S.EXECUTING)
    await factory.execution(other, other_approval, world.owner, status=E.RUNNING)  # another action's live write
    await factory.execution(action, approval, world.owner, status=E.UNCERTAIN)  # finished, outcome unknown
    assert not await svc.has_live_execution(session, action)
    assert await svc.has_live_execution(session, other)


# ---------------------------------------------------------------------------------------------
# Write budget: the external create is bounded below the execution lease
# ---------------------------------------------------------------------------------------------


class _HangingClient(TrackingMockClient):
    """An Azure DevOps create that never returns (retry storm, slow-drip response)."""

    started = False

    async def create_work_item_idempotent(self, request: Any) -> Any:
        self.started = True
        await asyncio.Event().wait()


def test_write_budget_leaves_room_inside_the_lease(svc: ExecutionService) -> None:
    assert svc.write_budget_seconds == svc.settings.job_lease_seconds * execution_module.WRITE_BUDGET_SHARE
    assert svc.write_budget_seconds < svc.settings.job_lease_seconds


async def test_external_write_over_budget_is_uncertain_and_reconcilable(
    settings: Settings,
    session: AsyncSession,
    factory: Factory,
    world: World,
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_module, "WRITE_BUDGET_SHARE", 0.0001)  # 30 ms of a 300 s lease
    hanging = _HangingClient()
    lookups = FakeAdo()

    async def factory_fn(s: AsyncSession, actor: Actor) -> Any:
        return hanging if not hanging.started else await lookups.factory(s, actor)

    svc = ExecutionService(settings, factory_fn)
    action, _ = await _approved(factory, world)
    # (wait_for: without the bound this would hang; with it the write is cut off after 30 ms)
    execution = await asyncio.wait_for(svc.execute(session, world.actor(world.owner), action.id), timeout=10)

    assert (execution.status, execution.error_category) == (E.UNCERTAIN, FailureCategory.AMBIGUOUS)
    assert execution.error_message is not None
    assert "outcome is unknown" in execution.error_message
    assert hanging.closed
    assert (await _action(fresh, action)).status is S.UNCERTAIN
    assert not await svc.has_live_execution(session, action)  # recorded, not left "running"
    reconciled = await svc.reconcile(session, world.actor(world.owner), action.id)  # the tag lookup decides
    assert reconciled.status is E.FAILED
    assert (await _action(fresh, action)).status is S.FAILED


# ---------------------------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------------------------


def test_sanitize_summary_keeps_only_allow_listed_keys() -> None:
    summary = {
        "id": 7,
        "rev": 2,
        "state": "New",
        "url": "mock://x",
        "project": "P",
        "work_item_type": "Bug",
        "reply_draft_id": "r",
        "follow_up_task_id": "f",
        "no_action": True,
        "sent": False,
        "reconciled": True,
        "headers": {"Authorization": "Bearer secret"},
        "access_token": "secret",
        "fields": {"System.Description": "email body"},
        "_links": {},
        "mock": True,
    }
    cleaned = _sanitize_summary(summary)
    assert set(cleaned) == {
        "id",
        "rev",
        "state",
        "url",
        "project",
        "work_item_type",
        "reply_draft_id",
        "follow_up_task_id",
        "no_action",
        "sent",
        "reconciled",
    }
    assert "secret" not in str(cleaned)
    assert _sanitize_summary({}) == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("timeout", FailureCategory.TIMEOUT),
        ("authorization", FailureCategory.AUTHORIZATION),
        ("not_configured", FailureCategory.CONFIGURATION),
        ("unknown", FailureCategory.INTERNAL),
    ],
)
def test_category_mapping(value: str, expected: FailureCategory) -> None:
    assert _category(value) is expected


async def test_executions_never_exceed_one_success_per_action(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh, ado: FakeAdo
) -> None:
    action, _ = await _approved(factory, world)
    for _ in range(3):
        await svc.execute(session, world.actor(world.owner), action.id)
    executions = await _executions(fresh, action)
    assert [e.status for e in executions] == [E.SUCCEEDED]
    assert (await fresh.all(ActionApproval))[0].invalidated_at is None
