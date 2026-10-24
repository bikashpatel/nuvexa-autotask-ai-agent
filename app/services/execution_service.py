"""The ONLY code path that performs writes for proposed actions.

Every call re-establishes, from the database, that the actor may execute the action, that a current
approval exists for exactly this version and payload hash, that the payload is still valid, and that
it has not already been executed. Nothing passed in by the LLM or the workflow graph is trusted.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Conflict, IntegrationError, InvalidStateTransition, StaleVersion
from app.core.security import compute_payload_hash
from app.core.timeutil import utcnow
from app.db.models import ActionExecution, EmailMessage, FollowUpTask, ProposedAction, ReplyDraft, User
from app.domain.enums import (
    ActionStatus,
    ActionType,
    AuditOutcome,
    EmailAnalysisStatus,
    ExecutionStatus,
    FailureCategory,
)
from app.domain.state_machine import EXECUTABLE_ACTION_STATUSES, ensure_action_transition
from app.integrations.azure_devops.base import CreateOutcome, CreateOutcomeStatus
from app.integrations.azure_devops.mapping import build_create_request, idempotency_tag
from app.observability.logging import get_logger
from app.observability.tracing import start_span
from app.schemas.actions import (
    AzureDevOpsWorkItemPayload,
    EmailReplyDraftPayload,
    FollowUpTaskPayload,
    NoActionPayload,
)
from app.services.action_policy import ActionPolicy, AnyPayload
from app.services.approval_service import current_approval, load_action
from app.services.audit import record_audit
from app.services.authz import Actor, AuditedPermissionDenied, Perm, require_owner_or
from app.services.integration_clients import AdoClientFactory
from app.services.settings_service import get_tenant_settings

log = get_logger(__name__)

_OUTCOME_TO_STATUS = {
    CreateOutcomeStatus.CREATED: (ExecutionStatus.SUCCEEDED, ActionStatus.COMPLETED),
    CreateOutcomeStatus.EXISTING: (ExecutionStatus.SUCCEEDED, ActionStatus.COMPLETED),
    CreateOutcomeStatus.FAILED: (ExecutionStatus.FAILED, ActionStatus.FAILED),
    CreateOutcomeStatus.UNCERTAIN: (ExecutionStatus.UNCERTAIN, ActionStatus.UNCERTAIN),
}


class ExecutionService:
    def __init__(self, settings: Settings, ado_client_factory: AdoClientFactory) -> None:
        self.settings = settings
        self.ado_client_factory = ado_client_factory

    # ------------------------------------------------------------------ pre-flight (API, synchronous)

    async def preflight(
        self, session: AsyncSession, actor: Actor, action_id: uuid.UUID, *, version: int, payload_hash: str
    ) -> ProposedAction:
        """Fast checks for immediate user feedback before queuing. `execute` repeats all of them."""
        action = await load_action(session, actor, action_id)
        await self._check_actor(session, actor, action)
        if action.version != version or action.payload_hash != payload_hash:
            raise StaleVersion("The action changed after you loaded it. Review the latest version.")
        if action.status is ActionStatus.PENDING_APPROVAL:
            raise self._deny(actor, action, "The action has not been approved.")
        if action.status not in EXECUTABLE_ACTION_STATUSES:
            raise InvalidStateTransition(f"An action in status '{action.status}' cannot be executed.")
        if await current_approval(session, action) is None:
            raise self._deny(actor, action, "The action has no valid approval for its current version.")
        return action

    # ------------------------------------------------------------------ execution

    async def execute(
        self,
        session: AsyncSession,
        actor: Actor,
        action_id: uuid.UUID,
        *,
        expected_version: int | None = None,
        expected_payload_hash: str | None = None,
    ) -> ActionExecution:
        action = await load_action(session, actor, action_id, for_update=True)
        await self._check_actor(session, actor, action)

        if action.status is ActionStatus.COMPLETED:
            existing = await self._succeeded_execution(session, action)
            if existing is not None:
                return existing
        if action.status is ActionStatus.EXECUTING:
            if await self._has_live_execution(session, action):
                raise Conflict("The action is already executing.")
            # A worker died mid-write: the outcome is unknown, never re-send blindly.
            return await self._reconcile_locked(session, actor, action, reason="stale execution lease")
        if action.status is ActionStatus.PENDING_APPROVAL:
            raise self._deny(actor, action, "The action has not been approved.")
        if action.status not in EXECUTABLE_ACTION_STATUSES:
            record_audit(
                session,
                tenant_id=action.tenant_id,
                actor=actor,
                event_type="execution_blocked",
                resource_type="action",
                resource_id=action.id,
                action_id=action.id,
                workflow_run_id=action.workflow_run_id,
                outcome=AuditOutcome.DENIED,
                details={"status": action.status.value},
            )
            await session.commit()
            raise InvalidStateTransition(f"An action in status '{action.status}' cannot be executed.")
        if expected_version is not None and expected_version != action.version:
            raise StaleVersion()
        if expected_payload_hash is not None and expected_payload_hash != action.payload_hash:
            raise StaleVersion()

        recomputed = compute_payload_hash(action.action_type.value, action.target_integration.value, action.payload)
        if recomputed != action.payload_hash:
            record_audit(
                session,
                tenant_id=action.tenant_id,
                actor=actor,
                event_type="payload_integrity_failure",
                resource_type="action",
                resource_id=action.id,
                action_id=action.id,
                outcome=AuditOutcome.DENIED,
            )
            await session.commit()
            raise Conflict("Stored payload does not match its approved hash; execution refused.")

        approval = await current_approval(session, action)
        if approval is None:
            raise self._deny(actor, action, "The action has no valid approval for its current version.")

        previous = await self._succeeded_execution(session, action)
        if previous is not None:  # repair a status that lagged behind a successful write
            action.status = ActionStatus.COMPLETED
            await session.commit()
            return previous

        email = await session.get(EmailMessage, action.email_message_id)
        policy = ActionPolicy(await get_tenant_settings(session, action.tenant_id, self.settings), self.settings)
        attempt = await self._next_attempt(session, action)
        try:
            payload = policy.validate_payload(action.action_type, action.payload)
            issues = policy.policy_issues(action.action_type, payload, email)
        except (ValidationError, ValueError) as exc:
            payload, issues = None, [f"Payload failed revalidation: {exc}"[:500]]
        if issues or payload is None:
            return await self._record_preflight_failure(session, actor, action, approval.id, attempt, issues)

        # Claim: approved|failed → executing, guarded by status and version (concurrent executors lose here).
        result = await session.execute(
            update(ProposedAction)
            .where(
                ProposedAction.id == action.id,
                ProposedAction.status.in_(list(EXECUTABLE_ACTION_STATUSES)),
                ProposedAction.version == action.version,
            )
            .values(status=ActionStatus.EXECUTING, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            await session.rollback()
            raise Conflict("The action was modified or executed concurrently.")
        execution = ActionExecution(
            tenant_id=action.tenant_id,
            action_id=action.id,
            approval_id=approval.id,
            requested_by_user_id=actor.user_id,
            attempt_number=attempt,
            status=ExecutionStatus.RUNNING,
            action_version=action.version,
            payload_hash=action.payload_hash,
            started_at=utcnow(),
        )
        session.add(execution)
        record_audit(
            session,
            tenant_id=action.tenant_id,
            actor=actor,
            event_type="execution_started",
            resource_type="action",
            resource_id=action.id,
            action_id=action.id,
            workflow_run_id=action.workflow_run_id,
            details={
                "attempt": attempt,
                "approval_id": str(approval.id),
                "version": action.version,
                "target": action.target_integration.value,
            },
        )
        await session.commit()  # durable "executing" marker before any external call
        await session.refresh(action)

        with start_span(
            "nuvexa.action.execute",
            {
                "nuvexa.action_type": action.action_type.value,
                "nuvexa.attempt": attempt,
                "nuvexa.target": action.target_integration.value,
            },
        ):
            outcome = await self._dispatch(session, actor, action, payload, email)
        return await self._finish(session, actor, action, execution, outcome)

    async def reconcile(self, session: AsyncSession, actor: Actor, action_id: uuid.UUID) -> ActionExecution:
        action = await load_action(session, actor, action_id, for_update=True)
        await self._check_actor(session, actor, action)
        if action.status is ActionStatus.EXECUTING and await self._has_live_execution(session, action):
            raise Conflict("The action is still executing.")
        if action.status not in (ActionStatus.UNCERTAIN, ActionStatus.EXECUTING):
            raise InvalidStateTransition("Only uncertain actions can be reconciled.")
        return await self._reconcile_locked(session, actor, action, reason="manual reconcile")

    # ------------------------------------------------------------------ internals

    async def _check_actor(self, session: AsyncSession, actor: Actor, action: ProposedAction) -> None:
        user = await session.get(User, actor.user_id)
        if user is None or not user.is_active or user.tenant_id != action.tenant_id:
            raise self._deny(actor, action, "The user is not active in this tenant.")
        require_owner_or(
            actor,
            action.owner_user_id,
            Perm.ACTION_EXECUTE,
            Perm.ACTION_APPROVE_ANY,
            resource_type="action",
            resource_id=str(action.id),
        )

    @staticmethod
    def _deny(actor: Actor, action: ProposedAction, message: str) -> AuditedPermissionDenied:
        return AuditedPermissionDenied(message, actor=actor, resource_type="action", resource_id=str(action.id))

    @staticmethod
    async def _succeeded_execution(session: AsyncSession, action: ProposedAction) -> ActionExecution | None:
        stmt = select(ActionExecution).where(
            ActionExecution.action_id == action.id, ActionExecution.status == ExecutionStatus.SUCCEEDED
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _has_live_execution(self, session: AsyncSession, action: ProposedAction) -> bool:
        cutoff = utcnow() - timedelta(seconds=self.settings.job_lease_seconds)
        stmt = select(func.count()).where(
            ActionExecution.action_id == action.id,
            ActionExecution.status == ExecutionStatus.RUNNING,
            ActionExecution.started_at >= cutoff,
        )
        return bool((await session.execute(stmt)).scalar_one())

    @staticmethod
    async def _next_attempt(session: AsyncSession, action: ProposedAction) -> int:
        stmt = select(func.coalesce(func.max(ActionExecution.attempt_number), 0)).where(
            ActionExecution.action_id == action.id
        )
        return int((await session.execute(stmt)).scalar_one()) + 1

    async def _record_preflight_failure(
        self,
        session: AsyncSession,
        actor: Actor,
        action: ProposedAction,
        approval_id: uuid.UUID,
        attempt: int,
        issues: list[str],
    ) -> ActionExecution:
        now = utcnow()
        execution = ActionExecution(
            tenant_id=action.tenant_id,
            action_id=action.id,
            approval_id=approval_id,
            requested_by_user_id=actor.user_id,
            attempt_number=attempt,
            status=ExecutionStatus.FAILED,
            action_version=action.version,
            payload_hash=action.payload_hash,
            started_at=now,
            finished_at=now,
            latency_ms=0,
            error_category=FailureCategory.VALIDATION,
            error_message="; ".join(issues)[:2000],
        )
        session.add(execution)
        if action.status is not ActionStatus.FAILED:
            ensure_action_transition(action.status, ActionStatus.FAILED)
            action.status = ActionStatus.FAILED
        action.last_error = execution.error_message
        record_audit(
            session,
            tenant_id=action.tenant_id,
            actor=actor,
            event_type="external_write",
            resource_type="action",
            resource_id=action.id,
            action_id=action.id,
            workflow_run_id=action.workflow_run_id,
            outcome=AuditOutcome.FAILURE,
            details={"stage": "revalidation", "issues": issues},
        )
        await session.commit()
        return execution

    async def _dispatch(
        self,
        session: AsyncSession,
        actor: Actor,
        action: ProposedAction,
        payload: AnyPayload,
        email: EmailMessage | None,
    ) -> CreateOutcome:
        started = time.perf_counter()
        try:
            match payload:
                case AzureDevOpsWorkItemPayload():
                    client = await self.ado_client_factory(session, actor)
                    try:
                        request = build_create_request(payload, idempotency_key=action.idempotency_key)
                        return await client.create_work_item_idempotent(request)
                    finally:
                        await client.aclose()
                case EmailReplyDraftPayload():
                    return await self._store_reply_draft(session, action, payload)
                case FollowUpTaskPayload():
                    return await self._create_follow_up(session, action, payload)
                case NoActionPayload():
                    if email is not None:
                        email.analysis_status = EmailAnalysisStatus.DONE
                    return CreateOutcome(status=CreateOutcomeStatus.CREATED, response_summary={"no_action": True})
        except IntegrationError as exc:
            category = _category(exc.category)
            log.warning("execution_integration_error", category=category.value, action_id=str(action.id))
            return CreateOutcome(
                status=CreateOutcomeStatus.FAILED,
                error_category=category,
                error_message=exc.message[:2000],
                http_status=exc.status,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            # Unknown failure after the claim: for external targets the write may have happened.
            log.exception("execution_unexpected_error", action_id=str(action.id))
            external = action.action_type is ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM
            return CreateOutcome(
                status=CreateOutcomeStatus.UNCERTAIN if external else CreateOutcomeStatus.FAILED,
                error_category=FailureCategory.AMBIGUOUS if external else FailureCategory.INTERNAL,
                error_message=f"{type(exc).__name__}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

    async def _store_reply_draft(
        self, session: AsyncSession, action: ProposedAction, payload: EmailReplyDraftPayload
    ) -> CreateOutcome:
        existing = (
            await session.execute(select(ReplyDraft).where(ReplyDraft.action_id == action.id))
        ).scalar_one_or_none()
        if existing is None:
            existing = ReplyDraft(
                tenant_id=action.tenant_id,
                owner_user_id=action.owner_user_id,
                action_id=action.id,
                email_message_id=action.email_message_id,
                to_recipients=[str(a) for a in payload.to],
                subject=payload.subject,
                body=payload.body,
            )
            session.add(existing)
            await session.flush()
            status = CreateOutcomeStatus.CREATED
        else:
            status = CreateOutcomeStatus.EXISTING
        return CreateOutcome(status=status, response_summary={"reply_draft_id": str(existing.id), "sent": False})

    async def _create_follow_up(
        self, session: AsyncSession, action: ProposedAction, payload: FollowUpTaskPayload
    ) -> CreateOutcome:
        existing = (
            await session.execute(select(FollowUpTask).where(FollowUpTask.action_id == action.id))
        ).scalar_one_or_none()
        if existing is None:
            existing = FollowUpTask(
                tenant_id=action.tenant_id,
                owner_user_id=action.owner_user_id,
                action_id=action.id,
                email_message_id=action.email_message_id,
                title=payload.title,
                description=payload.description,
                due_at=payload.due_at,
            )
            session.add(existing)
            await session.flush()
            status = CreateOutcomeStatus.CREATED
        else:
            status = CreateOutcomeStatus.EXISTING
        return CreateOutcome(status=status, response_summary={"follow_up_task_id": str(existing.id)})

    async def _finish(
        self,
        session: AsyncSession,
        actor: Actor,
        action: ProposedAction,
        execution: ActionExecution,
        outcome: CreateOutcome,
    ) -> ActionExecution:
        exec_status, action_status = _OUTCOME_TO_STATUS[outcome.status]
        execution.status = exec_status
        execution.finished_at = utcnow()
        execution.latency_ms = outcome.latency_ms or int(
            (execution.finished_at - execution.started_at).total_seconds() * 1000
        )
        execution.retry_count = outcome.retry_count
        execution.provider_status_code = outcome.http_status
        execution.provider_response = _sanitize_summary(outcome.response_summary)
        execution.deduplicated = outcome.status is CreateOutcomeStatus.EXISTING
        execution.error_category = outcome.error_category
        execution.error_message = outcome.error_message
        if outcome.work_item is not None:
            execution.external_id = str(outcome.work_item.id)
            execution.external_url = outcome.work_item.url
        ensure_action_transition(action.status, action_status)
        action.status = action_status
        action.external_id = execution.external_id or action.external_id
        action.external_url = execution.external_url or action.external_url
        action.last_error = outcome.error_message
        if action_status is ActionStatus.COMPLETED:
            action.executed_at = execution.finished_at
        record_audit(
            session,
            tenant_id=action.tenant_id,
            actor=actor,
            event_type="external_write",
            resource_type="action",
            resource_id=action.id,
            action_id=action.id,
            workflow_run_id=action.workflow_run_id,
            outcome=AuditOutcome.SUCCESS if exec_status is ExecutionStatus.SUCCEEDED else AuditOutcome.FAILURE,
            details={
                "target": action.target_integration.value,
                "result": outcome.status.value,
                "external_id": execution.external_id,
                "deduplicated": execution.deduplicated,
                "retry_count": outcome.retry_count,
                "error_category": outcome.error_category.value if outcome.error_category else None,
                "attempt": execution.attempt_number,
            },
        )
        await session.commit()
        return execution

    async def _reconcile_locked(
        self, session: AsyncSession, actor: Actor, action: ProposedAction, *, reason: str
    ) -> ActionExecution:
        running = (
            await session.execute(
                select(ActionExecution)
                .where(ActionExecution.action_id == action.id)
                .order_by(ActionExecution.attempt_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if running is None:
            raise Conflict("No execution attempt exists to reconcile.")
        if running.status is ExecutionStatus.RUNNING:
            running.status = ExecutionStatus.UNCERTAIN
            running.finished_at = utcnow()
            running.error_category = FailureCategory.AMBIGUOUS
            running.error_message = f"Outcome unknown ({reason})."
            action.status = ActionStatus.UNCERTAIN
            # Persist "uncertain" before the lookup so a lookup failure cannot roll it back to "executing".
            await session.commit()
            await session.refresh(action)

        found_ref = None
        if action.action_type is ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM:
            project = str(action.payload.get("project", ""))
            client = await self.ado_client_factory(session, actor)
            try:
                found_ref = await client.find_by_idempotency_tag(project, idempotency_tag(action.idempotency_key))
            finally:
                await client.aclose()
            confirmed = found_ref is not None
        else:
            model = {ActionType.PROPOSE_EMAIL_REPLY: ReplyDraft, ActionType.CREATE_FOLLOW_UP_TASK: FollowUpTask}.get(
                action.action_type
            )
            confirmed = (
                model is None
                or (
                    await session.execute(select(model).where(model.action_id == action.id))  # type: ignore[attr-defined]
                ).first()
                is not None
            )

        execution = ActionExecution(
            tenant_id=action.tenant_id,
            action_id=action.id,
            approval_id=running.approval_id,
            requested_by_user_id=actor.user_id,
            attempt_number=running.attempt_number + 1,
            status=ExecutionStatus.SUCCEEDED if confirmed else ExecutionStatus.FAILED,
            action_version=action.version,
            payload_hash=action.payload_hash,
            started_at=utcnow(),
            finished_at=utcnow(),
            latency_ms=0,
            deduplicated=confirmed,
            external_id=str(found_ref.id) if found_ref else None,
            external_url=found_ref.url if found_ref else None,
            provider_response={"reconciled": True, "reason": reason},
            error_category=None if confirmed else FailureCategory.AMBIGUOUS,
            error_message=None if confirmed else "Reconciled: the external item does not exist; safe to retry.",
        )
        session.add(execution)
        new_status = ActionStatus.COMPLETED if confirmed else ActionStatus.FAILED
        ensure_action_transition(action.status, new_status)
        action.status = new_status
        if confirmed:
            action.external_id = execution.external_id or action.external_id
            action.external_url = execution.external_url or action.external_url
            action.executed_at = execution.finished_at
            action.last_error = None
        else:
            action.last_error = execution.error_message
        record_audit(
            session,
            tenant_id=action.tenant_id,
            actor=actor,
            event_type="execution_reconciled",
            resource_type="action",
            resource_id=action.id,
            action_id=action.id,
            workflow_run_id=action.workflow_run_id,
            outcome=AuditOutcome.SUCCESS,
            details={"found": confirmed, "external_id": execution.external_id, "reason": reason},
        )
        await session.commit()
        return execution


_CATEGORY_ALIASES = {"not_configured": FailureCategory.CONFIGURATION}


def _category(value: str) -> FailureCategory:
    if value in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[value]
    try:
        return FailureCategory(value)
    except ValueError:
        return FailureCategory.INTERNAL


def _sanitize_summary(summary: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "rev",
        "state",
        "url",
        "reply_draft_id",
        "follow_up_task_id",
        "no_action",
        "sent",
        "project",
        "work_item_type",
        "reconciled",
    }
    return {k: v for k, v in summary.items() if k in allowed}
