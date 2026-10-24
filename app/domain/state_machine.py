"""Explicit, deterministic state transition tables. Every status change goes through these checks."""

from __future__ import annotations

from app.core.errors import InvalidStateTransition
from app.domain.enums import ActionStatus, WorkflowStatus

A = ActionStatus
W = WorkflowStatus

ACTION_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    A.PENDING_APPROVAL: frozenset({A.PENDING_APPROVAL, A.APPROVED, A.REJECTED, A.CANCELLED, A.SUPERSEDED}),
    # approved → pending_approval happens when the payload is edited (approval invalidated)
    A.APPROVED: frozenset({A.PENDING_APPROVAL, A.EXECUTING, A.FAILED, A.REJECTED, A.CANCELLED, A.SUPERSEDED}),
    A.EXECUTING: frozenset({A.COMPLETED, A.FAILED, A.UNCERTAIN}),
    # failed: nothing was created (confirmed) → may retry under the still-valid approval or be edited
    A.FAILED: frozenset({A.EXECUTING, A.PENDING_APPROVAL, A.CANCELLED, A.SUPERSEDED}),
    # uncertain: only reconciliation may resolve it
    A.UNCERTAIN: frozenset({A.COMPLETED, A.FAILED}),
    A.COMPLETED: frozenset(),
    A.REJECTED: frozenset(),
    A.CANCELLED: frozenset(),
    A.SUPERSEDED: frozenset(),
}

EDITABLE_ACTION_STATUSES = frozenset({A.PENDING_APPROVAL, A.APPROVED, A.FAILED})
EXECUTABLE_ACTION_STATUSES = frozenset({A.APPROVED, A.FAILED})


WORKFLOW_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    W.RECEIVED: frozenset({W.NORMALIZED, W.FAILED, W.CANCELLED}),
    W.NORMALIZED: frozenset({W.CLASSIFIED, W.FAILED, W.CANCELLED}),
    W.CLASSIFIED: frozenset({W.EXTRACTED, W.FAILED, W.CANCELLED}),
    W.EXTRACTED: frozenset({W.VALIDATED, W.FAILED, W.CANCELLED}),
    W.VALIDATED: frozenset({W.ACTION_PROPOSED, W.FAILED, W.CANCELLED}),
    W.ACTION_PROPOSED: frozenset({W.PENDING_APPROVAL, W.COMPLETED, W.FAILED, W.CANCELLED}),
    W.PENDING_APPROVAL: frozenset(
        {W.PENDING_APPROVAL, W.APPROVED, W.EXECUTING, W.COMPLETED, W.REJECTED, W.CANCELLED, W.NORMALIZED, W.FAILED}
    ),
    W.APPROVED: frozenset(
        {W.PENDING_APPROVAL, W.EXECUTING, W.COMPLETED, W.REJECTED, W.CANCELLED, W.NORMALIZED, W.FAILED}
    ),
    W.EXECUTING: frozenset({W.EXECUTING, W.PENDING_APPROVAL, W.APPROVED, W.COMPLETED, W.FAILED, W.REJECTED}),
    # failed runs are recoverable: retry analysis (→ normalized) or continue review/execution
    # (a failed analysis step may be retried, so every analysis status is reachable again)
    W.FAILED: frozenset(
        {
            W.NORMALIZED,
            W.CLASSIFIED,
            W.EXTRACTED,
            W.VALIDATED,
            W.ACTION_PROPOSED,
            W.PENDING_APPROVAL,
            W.APPROVED,
            W.EXECUTING,
            W.COMPLETED,
            W.CANCELLED,
            W.REJECTED,
            W.FAILED,
        }
    ),
    W.COMPLETED: frozenset(),
    W.REJECTED: frozenset(),
    W.CANCELLED: frozenset(),
}


def can_transition_action(current: ActionStatus, target: ActionStatus) -> bool:
    return target in ACTION_TRANSITIONS[current]


def ensure_action_transition(current: ActionStatus, target: ActionStatus) -> None:
    if not can_transition_action(current, target):
        raise InvalidStateTransition(f"Action cannot move from '{current}' to '{target}'.")


def can_transition_workflow(current: WorkflowStatus, target: WorkflowStatus) -> bool:
    # Re-entering the same non-terminal status is allowed (graph nodes are re-run after crashes/interrupts).
    if current == target:
        return current not in (W.COMPLETED, W.REJECTED, W.CANCELLED)
    return target in WORKFLOW_TRANSITIONS[current]


def ensure_workflow_transition(current: WorkflowStatus, target: WorkflowStatus) -> None:
    if not can_transition_workflow(current, target):
        raise InvalidStateTransition(f"Workflow cannot move from '{current}' to '{target}'.")


def derive_review_status(action_statuses: list[ActionStatus]) -> WorkflowStatus:
    """Workflow status once proposals exist, computed from the statuses of its live actions."""
    live = [s for s in action_statuses if s is not A.SUPERSEDED]
    if not live:
        return W.COMPLETED
    if any(s is A.EXECUTING for s in live):
        return W.EXECUTING
    if any(s in (A.FAILED, A.UNCERTAIN) for s in live):
        return W.FAILED
    if any(s is A.PENDING_APPROVAL for s in live):
        return W.PENDING_APPROVAL
    if any(s is A.APPROVED for s in live):
        return W.APPROVED
    if all(s is A.REJECTED for s in live):
        return W.REJECTED
    if all(s in (A.REJECTED, A.CANCELLED) for s in live) and any(s is A.CANCELLED for s in live):
        return W.CANCELLED
    return W.COMPLETED
