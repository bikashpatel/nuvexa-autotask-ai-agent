"""Human review of proposed actions: edit, approve, reject, cancel.

Approvals are bound to `(action_id, version, payload_hash)`. Editing creates a new version and
invalidates every existing approval. Nothing here performs an external write.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Conflict, StaleVersion, ValidationFailed
from app.core.security import compute_payload_hash
from app.core.timeutil import utcnow
from app.db.models import ActionApproval, EmailMessage, ProposedAction, WorkflowRun
from app.domain.enums import ActionStatus, ApprovalDecision, AuditOutcome, JobType
from app.domain.state_machine import EDITABLE_ACTION_STATUSES, ensure_action_transition
from app.repositories.scoping import get_visible
from app.services.action_policy import ActionPolicy, payload_title
from app.services.audit import record_audit
from app.services.authz import Actor, Perm, require_owner_or
from app.services.settings_service import get_tenant_settings
from app.workers.queue import enqueue


async def load_action(
    session: AsyncSession, actor: Actor, action_id: uuid.UUID, *, for_update: bool = False
) -> ProposedAction:
    return await get_visible(session, ProposedAction, actor, action_id, for_update=for_update, label="Action")


async def current_approval(session: AsyncSession, action: ProposedAction) -> ActionApproval | None:
    """The approval that currently authorizes execution, if any."""
    stmt = (
        select(ActionApproval)
        .where(
            ActionApproval.action_id == action.id,
            ActionApproval.tenant_id == action.tenant_id,
            ActionApproval.decision == ApprovalDecision.APPROVED,
            ActionApproval.invalidated_at.is_(None),
            ActionApproval.action_version == action.version,
            ActionApproval.payload_hash == action.payload_hash,
        )
        .order_by(ActionApproval.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def invalidate_approvals(session: AsyncSession, action: ProposedAction, reason: str) -> int:
    result = await session.execute(
        update(ActionApproval)
        .where(
            ActionApproval.action_id == action.id,
            ActionApproval.decision == ApprovalDecision.APPROVED,
            ActionApproval.invalidated_at.is_(None),
        )
        .values(invalidated_at=utcnow(), invalidated_reason=reason[:255])
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def enqueue_workflow_refresh(session: AsyncSession, action: ProposedAction) -> None:
    """Ask the workflow graph to re-evaluate the run after a review decision."""
    await enqueue(
        session,
        job_type=JobType.RESUME_WORKFLOW,
        payload={"workflow_run_id": str(action.workflow_run_id), "command": {"type": "refresh"}},
        tenant_id=action.tenant_id,
        dedupe_key=f"refresh:{action.workflow_run_id}",
        concurrency_key=f"run:{action.workflow_run_id}",
    )


def _check_hash_integrity(action: ProposedAction) -> None:
    expected = compute_payload_hash(action.action_type.value, action.target_integration.value, action.payload)
    if expected != action.payload_hash:
        raise Conflict("Stored payload does not match its hash; the action cannot be reviewed.")


class ApprovalService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _policy(self, session: AsyncSession, tenant_id: uuid.UUID) -> ActionPolicy:
        return ActionPolicy(await get_tenant_settings(session, tenant_id, self.settings), self.settings)

    async def update(
        self,
        session: AsyncSession,
        actor: Actor,
        action_id: uuid.UUID,
        *,
        expected_version: int,
        payload: dict[str, Any],
    ) -> ProposedAction:
        action = await load_action(session, actor, action_id, for_update=True)
        require_owner_or(
            actor,
            action.owner_user_id,
            Perm.ACTION_EDIT,
            Perm.ACTION_APPROVE_ANY,
            resource_type="action",
            resource_id=str(action.id),
        )
        if action.status not in EDITABLE_ACTION_STATUSES:
            raise Conflict(f"An action in status '{action.status}' cannot be edited.")
        if action.version != expected_version:
            raise StaleVersion()
        email = await session.get(EmailMessage, action.email_message_id)
        policy = await self._policy(session, actor.tenant_id)
        # Provenance and recipients are system-controlled and cannot be edited.
        data = {**payload, "kind": action.payload.get("kind"), "source_email": action.payload.get("source_email")}
        if action.payload.get("kind") == "propose_email_reply":
            data["to"] = action.payload.get("to")
            data["in_reply_to_internet_message_id"] = action.payload.get("in_reply_to_internet_message_id")
        try:
            parsed = policy.validate_payload(action.action_type, data)
        except (ValidationError, ValueError) as exc:
            details = (
                [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
                if isinstance(exc, ValidationError)
                else [{"msg": str(exc)}]
            )
            raise ValidationFailed("The edited action is invalid.", details=details) from exc
        issues = policy.policy_issues(action.action_type, parsed, email)
        if issues:
            raise ValidationFailed("The edited action violates policy.", details=[{"msg": i} for i in issues])

        new_payload = parsed.model_dump(mode="json")
        new_hash = compute_payload_hash(action.action_type.value, action.target_integration.value, new_payload)
        if new_hash == action.payload_hash:
            return action
        changed = sorted(k for k in new_payload if new_payload.get(k) != action.payload.get(k))
        old_version = action.version
        invalidated = await invalidate_approvals(session, action, f"payload edited (v{old_version}→v{old_version + 1})")
        if action.status is not ActionStatus.PENDING_APPROVAL:
            ensure_action_transition(action.status, ActionStatus.PENDING_APPROVAL)
            action.status = ActionStatus.PENDING_APPROVAL
        action.payload = new_payload
        action.payload_hash = new_hash
        action.version = old_version + 1
        action.title = payload_title(parsed)
        action.validation_errors = []
        action.source = "user_edited"
        action.updated_by_user_id = actor.user_id
        action.last_error = None
        record_audit(
            session,
            tenant_id=actor.tenant_id,
            actor=actor,
            event_type="action_edited",
            resource_type="action",
            resource_id=action.id,
            action_id=action.id,
            workflow_run_id=action.workflow_run_id,
            details={"from_version": old_version, "to_version": action.version, "changed_fields": changed},
        )
        if invalidated:
            record_audit(
                session,
                tenant_id=actor.tenant_id,
                actor=actor,
                event_type="approval_invalidated",
                resource_type="action",
                resource_id=action.id,
                action_id=action.id,
                workflow_run_id=action.workflow_run_id,
                details={"reason": "payload edited", "invalidated_approvals": invalidated},
            )
        await enqueue_workflow_refresh(session, action)
        await session.commit()
        return action

    async def approve(
        self,
        session: AsyncSession,
        actor: Actor,
        action_id: uuid.UUID,
        *,
        version: int,
        payload_hash: str,
        comment: str | None,
    ) -> ProposedAction:
        action = await load_action(session, actor, action_id, for_update=True)
        require_owner_or(
            actor,
            action.owner_user_id,
            Perm.ACTION_APPROVE,
            Perm.ACTION_APPROVE_ANY,
            resource_type="action",
            resource_id=str(action.id),
        )
        if action.version != version or action.payload_hash != payload_hash:
            raise StaleVersion("The action changed after you loaded it. Review the latest version before approving.")
        _check_hash_integrity(action)
        if action.status is ActionStatus.APPROVED and await current_approval(session, action) is not None:
            return action  # idempotent: the same version is already approved
        ensure_action_transition(action.status, ActionStatus.APPROVED)
        if action.validation_errors:
            raise Conflict(
                "Resolve validation errors before approving.", details=[{"msg": e} for e in action.validation_errors]
            )
        email = await session.get(EmailMessage, action.email_message_id)
        policy = await self._policy(session, actor.tenant_id)
        try:
            parsed = policy.validate_payload(action.action_type, action.payload)
        except (ValidationError, ValueError) as exc:
            raise Conflict("The action payload is invalid; edit it before approving.") from exc
        issues = policy.policy_issues(action.action_type, parsed, email)
        if issues:
            raise Conflict(
                "The action violates policy; edit it before approving.", details=[{"msg": i} for i in issues]
            )
        approval = ActionApproval(
            tenant_id=action.tenant_id,
            action_id=action.id,
            decision=ApprovalDecision.APPROVED,
            decided_by_user_id=actor.user_id,
            action_version=action.version,
            payload_hash=action.payload_hash,
            reason=comment,
            created_at=utcnow(),
        )
        session.add(approval)
        action.status = ActionStatus.APPROVED
        action.updated_by_user_id = actor.user_id
        await session.flush()
        record_audit(
            session,
            tenant_id=actor.tenant_id,
            actor=actor,
            event_type="action_approved",
            resource_type="action",
            resource_id=action.id,
            action_id=action.id,
            workflow_run_id=action.workflow_run_id,
            details={
                "approval_id": str(approval.id),
                "version": action.version,
                "payload_hash": action.payload_hash,
                "action_type": action.action_type.value,
                "own_action": action.owner_user_id == actor.user_id,
            },
        )
        await enqueue_workflow_refresh(session, action)
        await session.commit()
        return action

    async def reject(self, session: AsyncSession, actor: Actor, action_id: uuid.UUID, *, reason: str) -> ProposedAction:
        action = await load_action(session, actor, action_id, for_update=True)
        require_owner_or(
            actor,
            action.owner_user_id,
            Perm.ACTION_APPROVE,
            Perm.ACTION_APPROVE_ANY,
            resource_type="action",
            resource_id=str(action.id),
        )
        if action.status not in (ActionStatus.PENDING_APPROVAL, ActionStatus.APPROVED):
            raise Conflict(f"An action in status '{action.status}' cannot be rejected.")
        ensure_action_transition(action.status, ActionStatus.REJECTED)
        await invalidate_approvals(session, action, "action rejected")
        session.add(
            ActionApproval(
                tenant_id=action.tenant_id,
                action_id=action.id,
                decision=ApprovalDecision.REJECTED,
                decided_by_user_id=actor.user_id,
                action_version=action.version,
                payload_hash=action.payload_hash,
                reason=reason,
                created_at=utcnow(),
            )
        )
        action.status = ActionStatus.REJECTED
        action.updated_by_user_id = actor.user_id
        record_audit(
            session,
            tenant_id=actor.tenant_id,
            actor=actor,
            event_type="action_rejected",
            resource_type="action",
            resource_id=action.id,
            action_id=action.id,
            workflow_run_id=action.workflow_run_id,
            details={"version": action.version, "reason": reason},
        )
        await enqueue_workflow_refresh(session, action)
        await session.commit()
        return action

    async def cancel(
        self, session: AsyncSession, actor: Actor, action_id: uuid.UUID, *, reason: str | None
    ) -> ProposedAction:
        action = await load_action(session, actor, action_id, for_update=True)
        require_owner_or(
            actor,
            action.owner_user_id,
            Perm.ACTION_EDIT,
            Perm.ACTION_APPROVE_ANY,
            resource_type="action",
            resource_id=str(action.id),
        )
        ensure_action_transition(action.status, ActionStatus.CANCELLED)
        await invalidate_approvals(session, action, "action cancelled")
        action.status = ActionStatus.CANCELLED
        action.updated_by_user_id = actor.user_id
        record_audit(
            session,
            tenant_id=actor.tenant_id,
            actor=actor,
            event_type="action_cancelled",
            resource_type="action",
            resource_id=action.id,
            action_id=action.id,
            workflow_run_id=action.workflow_run_id,
            details={"version": action.version, "reason": reason},
        )
        await enqueue_workflow_refresh(session, action)
        await session.commit()
        return action


async def supersede_open_actions(session: AsyncSession, run: WorkflowRun, reason: str) -> list[uuid.UUID]:
    """Used by re-analysis: open proposals of the previous attempt release their idempotency keys."""
    stmt = select(ProposedAction).where(
        ProposedAction.workflow_run_id == run.id,
        ProposedAction.status.in_([ActionStatus.PENDING_APPROVAL, ActionStatus.APPROVED, ActionStatus.FAILED]),
    )
    actions = (await session.execute(stmt)).scalars().all()
    for action in actions:
        ensure_action_transition(action.status, ActionStatus.SUPERSEDED)
        await invalidate_approvals(session, action, reason)
        action.status = ActionStatus.SUPERSEDED
        record_audit(
            session,
            tenant_id=run.tenant_id,
            event_type="action_superseded",
            resource_type="action",
            resource_id=action.id,
            action_id=action.id,
            workflow_run_id=run.id,
            outcome=AuditOutcome.SUCCESS,
            details={"reason": reason},
        )
    return [a.id for a in actions]
