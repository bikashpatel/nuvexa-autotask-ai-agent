"""Behaviour pinned down by mutation testing (docs/TESTING_STRATEGY.md#mutation-testing).

Each test kills mutants that survived the original suite because every test had a single action in the
database: queries that lost their `action_id` filter, a claim without its status guard, and audit events
missing their attribution fields.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Conflict, InvalidStateTransition
from app.db.models import ActionExecution, ProposedAction
from app.domain.enums import ActionStatus, ActionType, ExecutionStatus
from app.services import execution_service as execution_module
from app.services.approval_service import ApprovalService, current_approval
from app.services.authz import AuditedPermissionDenied
from app.services.execution_service import ExecutionService
from app.services.settings_service import get_tenant_settings
from tests.unit.services.conftest import Factory, FakeAdo, Fresh, World, hash_of, payload_for

WI = ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM


@pytest.fixture
def svc(settings: Settings, ado: FakeAdo) -> ExecutionService:
    return ExecutionService(settings, ado.factory)


async def _second_run(factory: Factory, world: World):  # type: ignore[no-untyped-def]
    email = await factory.email(world.account, subject="Second request", provider_message_id=f"p-{uuid.uuid4()}")
    return await factory.run(email)


async def test_executing_one_action_never_claims_another(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    # Arrange: two approved actions for two different emails
    a, _ = await factory.approved_action(world.run, world.owner)
    b, _ = await factory.approved_action(await _second_run(factory, world), world.owner)

    # Act
    await svc.execute(session, world.actor(world.owner), a.id)

    # Assert: only the executed action changed state
    assert (await fresh.get(ProposedAction, a.id)).status is ActionStatus.COMPLETED  # type: ignore[union-attr]
    assert (await fresh.get(ProposedAction, b.id)).status is ActionStatus.APPROVED  # type: ignore[union-attr]


async def test_an_approval_never_authorizes_a_different_action_with_an_identical_payload(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, ado: FakeAdo
) -> None:
    # Arrange: same payload and hash on two actions; only the first is approved
    payload = payload_for(WI, world.email)
    a, _ = await factory.approved_action(world.run, world.owner, payload=payload)
    b = await factory.action(
        await _second_run(factory, world),
        WI,
        status=ActionStatus.APPROVED,
        payload=payload,
        payload_hash=hash_of(WI, payload),
    )
    assert a.payload_hash == b.payload_hash

    # Act / Assert
    assert await current_approval(session, b) is None
    with pytest.raises(AuditedPermissionDenied):
        await svc.execute(session, world.actor(world.owner), b.id)
    assert ado.items() == []


async def test_editing_one_action_keeps_other_actions_approvals_valid(
    settings: Settings, session: AsyncSession, factory: Factory, world: World
) -> None:
    # Arrange
    a, _ = await factory.approved_action(world.run, world.owner)
    b, _ = await factory.approved_action(await _second_run(factory, world), world.owner)

    # Act: edit A
    edited = {**a.payload, "title": "Edited title"}
    await ApprovalService(settings).update(session, world.actor(world.owner), a.id, expected_version=1, payload=edited)

    # Assert: A's approval is gone, B's is untouched
    await session.refresh(b)
    assert await current_approval(session, b) is not None
    assert (await session.get(ProposedAction, a.id)).status is ActionStatus.PENDING_APPROVAL  # type: ignore[union-attr]


async def test_another_actions_execution_history_is_never_reused(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World, ado: FakeAdo, fresh: Fresh
) -> None:
    # Arrange: A already has a succeeded execution, C has a live RUNNING one; B is approved and untouched
    a, approval_a = await factory.approved_action(world.run, world.owner, status=ActionStatus.COMPLETED)
    await factory.execution(a, approval_a, world.owner, status=ExecutionStatus.SUCCEEDED)
    other_run = await _second_run(factory, world)
    c, approval_c = await factory.approved_action(other_run, world.owner, status=ActionStatus.EXECUTING)
    await factory.execution(c, approval_c, world.owner, status=ExecutionStatus.RUNNING)
    b, _ = await factory.approved_action(await _second_run(factory, world), world.owner)

    # Act
    execution = await svc.execute(session, world.actor(world.owner), b.id)

    # Assert: B was really executed (own record, own external item), not short-circuited or blocked
    assert execution.action_id == b.id
    assert execution.status is ExecutionStatus.SUCCEEDED
    assert len(ado.items()) == 1
    assert [e.action_id for e in await fresh.all(ActionExecution, ActionExecution.action_id == b.id)] == [b.id]


async def test_an_action_cancelled_between_the_checks_and_the_claim_is_not_executed(
    svc: ExecutionService,
    session: AsyncSession,
    settings: Settings,
    factory: Factory,
    world: World,
    ado: FakeAdo,
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if settings.is_postgres:
        pytest.skip(
            "on PostgreSQL the row lock serializes this; covered by tests/integration/test_postgres_concurrency"
        )
    # Arrange: settings row exists, so the checks below only read
    await get_tenant_settings(session, world.tenant.id, settings)
    await session.commit()
    action, _ = await factory.approved_action(world.run, world.owner)
    action_id = action.id  # the service rolls back on conflict, which expires `action`
    real_get_settings = execution_module.get_tenant_settings

    async def cancel_concurrently(*args: Any, **kwargs: Any) -> Any:
        async with fresh.db.session() as other:  # another request cancels the action meanwhile
            await other.execute(
                update(ProposedAction).where(ProposedAction.id == action_id).values(status=ActionStatus.CANCELLED)
            )
            await other.commit()
        return await real_get_settings(*args, **kwargs)

    monkeypatch.setattr(execution_module, "get_tenant_settings", cancel_concurrently)

    # Act / Assert
    with pytest.raises(Conflict):
        await svc.execute(session, world.actor(world.owner), action_id)
    assert ado.items() == []
    assert (await fresh.get(ProposedAction, action_id)).status is ActionStatus.CANCELLED  # type: ignore[union-attr]


async def test_review_and_execution_events_are_fully_attributed(
    svc: ExecutionService, settings: Settings, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    # Arrange
    service = ApprovalService(settings)
    actor = world.actor(world.owner)
    edit = await factory.action(world.run, WI, slot=1)
    approve = await factory.action(await _second_run(factory, world), WI)
    reject = await factory.action(await _second_run(factory, world), WI)
    cancel = await factory.action(await _second_run(factory, world), WI)

    # Act
    await service.update(session, actor, edit.id, expected_version=1, payload={**edit.payload, "title": "New"})
    await service.approve(session, actor, approve.id, version=1, payload_hash=approve.payload_hash, comment=None)
    await service.reject(session, actor, reject.id, reason="duplicate")
    await service.cancel(session, actor, cancel.id, reason=None)
    await svc.execute(session, actor, approve.id)

    # Assert: every event names the tenant, the actor, the action, its run and the resource
    for action, kinds in (
        (edit, ("action_edited",)),
        (approve, ("action_approved", "execution_started", "external_write")),
        (reject, ("action_rejected",)),
        (cancel, ("action_cancelled",)),
    ):
        events = [e for e in await fresh.audits(action_id=action.id) if e.event_type in kinds]
        assert {e.event_type for e in events} == set(kinds)
        for event in events:
            assert event.tenant_id == world.tenant.id
            assert event.actor_user_id == world.owner.id
            assert event.resource_type == "action"
            assert event.resource_id == str(action.id)
            assert event.workflow_run_id == action.workflow_run_id


async def test_denied_executions_are_attributed_to_the_denied_actor(
    svc: ExecutionService, session: AsyncSession, factory: Factory, world: World
) -> None:
    action = await factory.action(world.run, WI, status=ActionStatus.APPROVED)  # approved status, no approval row
    with pytest.raises(AuditedPermissionDenied) as info:
        await svc.execute(session, world.actor(world.owner), action.id)
    assert info.value.actor.user_id == world.owner.id
    assert info.value.resource_type == "action"
    assert info.value.resource_id == str(action.id)


# --------------------------------------------------------------------------------------- action policy


def _policy_inputs(world: World) -> tuple[Any, Any]:
    from app.domain.enums import ConfidenceBucket, EmailCategory, Priority, Urgency
    from app.schemas.ai import ClassificationOutput, ValidatedAnalysis, ValidatedExtraction

    analysis = ValidatedAnalysis(
        classification=ClassificationOutput(
            category=EmailCategory.INCIDENT,
            rationale="Outage.",
            urgency=Urgency.IMMEDIATE,
            priority=Priority.HIGH,
            action_recommended=True,
            needs_more_info=False,
            missing_information=[],
            extraction_warnings=[],
            confidence=0.9,
        ),
        extraction=ValidatedExtraction(source_email_id=world.email.id, summary="Investigate", description="d"),
        confidence_score=0.9,
        confidence_bucket=ConfidenceBucket.HIGH,
    )
    return world.email, analysis


def _call(tool: str, action_type: ActionType, **arguments: Any) -> Any:
    from app.schemas.ai import PlannedToolCall

    return PlannedToolCall(tool_name=tool, action_type=action_type, arguments=arguments)  # type: ignore[arg-type]


def _wi(**overrides: Any) -> Any:
    args = {
        "title": "Investigate",
        "description": "d",
        "work_item_type": "Bug",
        "priority": "high",
        "tags": [],
        "rationale": "r",
        "expected_impact": "i",
    }
    return _call("propose_azure_devops_work_item", WI, **{**args, **overrides})


def _reply(**overrides: Any) -> Any:
    args = {"subject": "Re: x", "body": "Thanks", "rationale": "r", "expected_impact": "i"}
    return _call("propose_email_reply", ActionType.PROPOSE_EMAIL_REPLY, **{**args, **overrides})


def _follow_up(**overrides: Any) -> Any:
    args = {"title": "Check", "description": "d", "due_date": None, "rationale": "r", "expected_impact": "i"}
    return _call("propose_follow_up_task", ActionType.CREATE_FOLLOW_UP_TASK, **{**args, **overrides})


def _policy(settings: Settings) -> Any:
    from app.schemas.settings import TenantSettings
    from app.services.action_policy import ActionPolicy

    return ActionPolicy(TenantSettings.model_validate({"azure_devops": {"project": "NUVEXA-Demo"}}), settings)


def test_a_duplicate_tool_call_does_not_drop_later_proposals(settings: Settings, world: World) -> None:
    email, analysis = _policy_inputs(world)
    specs = _policy(settings).build_proposals(email, analysis, [_wi(), _wi(title="dup"), _reply()])
    assert [s.action_type for s in specs] == [WI, ActionType.PROPOSE_EMAIL_REPLY]
    assert specs[0].payload["title"] == "Investigate"


def test_overlong_model_text_is_clipped_to_exact_schema_limits(settings: Settings, world: World) -> None:
    email, analysis = _policy_inputs(world)
    long = "x" * 5000
    wi, follow_up, reply = _policy(settings).build_proposals(
        email,
        analysis,
        [
            _wi(title=long, description="y" * 40_000, rationale=long, expected_impact=long),
            _follow_up(title=long, description="z" * 20_000, rationale=long, expected_impact=long),
            _reply(subject=long, rationale=long, expected_impact=long),
        ],
    )
    for spec in (wi, follow_up, reply):
        assert not spec.errors, spec.errors
        assert len(spec.rationale) == 2000
        assert len(spec.expected_impact) == 2000
    assert len(wi.payload["title"]) == 255
    assert len(wi.payload["description"]) == 32_000
    assert len(follow_up.payload["title"]) == 255
    assert len(follow_up.payload["description"]) == 10_000
    assert len(reply.payload["subject"]) == 255
    assert isinstance(wi.payload["source_email"]["email_id"], str)  # JSON-serialized provenance


async def test_failure_paths_are_fully_attributed_in_the_audit_trail(
    svc: ExecutionService, settings: Settings, session: AsyncSession, factory: Factory, world: World, fresh: Fresh
) -> None:
    actor = world.actor(world.owner)
    # blocked: executing a rejected action
    rejected = await factory.action(world.run, WI, status=ActionStatus.REJECTED)
    with pytest.raises(InvalidStateTransition):
        await svc.execute(session, actor, rejected.id)
    # revalidation failure: approved payload now violates policy (project removed from the allow-list)
    run_b = await _second_run(factory, world)
    bad_payload = {**payload_for(WI, world.email), "work_item_type": "Not-Allowed-Type"}
    invalid, _ = await factory.approved_action(run_b, world.owner, payload=bad_payload)
    await svc.execute(session, actor, invalid.id)
    # reconcile: an uncertain action whose item does not exist
    run_c = await _second_run(factory, world)
    uncertain, approval = await factory.approved_action(run_c, world.owner, status=ActionStatus.UNCERTAIN)
    await factory.execution(uncertain, approval, world.owner, status=ExecutionStatus.UNCERTAIN)
    await svc.reconcile(session, actor, uncertain.id)
    # approval invalidated by an edit
    run_d = await _second_run(factory, world)
    edited, _ = await factory.approved_action(run_d, world.owner)
    await ApprovalService(settings).update(
        session, actor, edited.id, expected_version=1, payload={**edited.payload, "title": "Changed"}
    )

    for action, kind in (
        (rejected, "execution_blocked"),
        (invalid, "external_write"),
        (uncertain, "execution_reconciled"),
        (edited, "approval_invalidated"),
    ):
        [event] = await fresh.audits(kind, action_id=action.id)
        assert event.tenant_id == world.tenant.id, kind
        assert event.actor_user_id == world.owner.id, kind
        assert event.resource_id == str(action.id), kind
        assert event.workflow_run_id == action.workflow_run_id, kind
