"""Exhaustive checks of the action/workflow transition tables and review-status derivation."""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.errors import Conflict, InvalidStateTransition
from app.domain.enums import (
    ACTIVE_WORKFLOW_STATUSES,
    TERMINAL_WORKFLOW_STATUSES,
    ActionStatus,
    WorkflowStatus,
)
from app.domain.state_machine import (
    ACTION_TRANSITIONS,
    EDITABLE_ACTION_STATUSES,
    EXECUTABLE_ACTION_STATUSES,
    WORKFLOW_TRANSITIONS,
    can_transition_action,
    can_transition_workflow,
    derive_review_status,
    ensure_action_transition,
    ensure_workflow_transition,
)

A = ActionStatus
W = WorkflowStatus

# Independent restatement of the documented lifecycle (docs/architecture.md §5) — not derived from the table.
EXPECTED_ACTION_EDGES: set[tuple[ActionStatus, ActionStatus]] = {
    (A.PENDING_APPROVAL, A.PENDING_APPROVAL),  # edit (version++)
    (A.PENDING_APPROVAL, A.APPROVED),
    (A.PENDING_APPROVAL, A.REJECTED),
    (A.PENDING_APPROVAL, A.CANCELLED),
    (A.PENDING_APPROVAL, A.SUPERSEDED),
    (A.APPROVED, A.PENDING_APPROVAL),  # edit invalidates approval
    (A.APPROVED, A.EXECUTING),
    (A.APPROVED, A.FAILED),  # revalidation failure before the claim
    (A.APPROVED, A.REJECTED),
    (A.APPROVED, A.CANCELLED),
    (A.APPROVED, A.SUPERSEDED),
    (A.EXECUTING, A.COMPLETED),
    (A.EXECUTING, A.FAILED),
    (A.EXECUTING, A.UNCERTAIN),
    (A.FAILED, A.EXECUTING),  # retry under a still-valid approval
    (A.FAILED, A.PENDING_APPROVAL),
    (A.FAILED, A.CANCELLED),
    (A.FAILED, A.SUPERSEDED),
    (A.UNCERTAIN, A.COMPLETED),
    (A.UNCERTAIN, A.FAILED),
}

TERMINAL_ACTIONS = {A.COMPLETED, A.REJECTED, A.CANCELLED, A.SUPERSEDED}
ALL_ACTION_PAIRS = list(itertools.product(ActionStatus, ActionStatus))
ALL_WORKFLOW_PAIRS = list(itertools.product(WorkflowStatus, WorkflowStatus))


def test_tables_cover_every_status() -> None:
    assert set(ACTION_TRANSITIONS) == set(ActionStatus)
    assert set(WORKFLOW_TRANSITIONS) == set(WorkflowStatus)


def test_action_table_matches_documented_lifecycle() -> None:
    table_edges = {(src, dst) for src, targets in ACTION_TRANSITIONS.items() for dst in targets}
    assert table_edges == EXPECTED_ACTION_EDGES


@pytest.mark.parametrize(("current", "target"), ALL_ACTION_PAIRS, ids=lambda s: s.value)
def test_every_action_pair(current: ActionStatus, target: ActionStatus) -> None:
    allowed = (current, target) in EXPECTED_ACTION_EDGES
    assert can_transition_action(current, target) is allowed
    if allowed:
        ensure_action_transition(current, target)
    else:
        with pytest.raises(InvalidStateTransition) as info:
            ensure_action_transition(current, target)
        assert current.value in info.value.message
        assert target.value in info.value.message
        assert info.value.status_code == 409
        assert info.value.code == "invalid_state_transition"


@pytest.mark.parametrize("terminal", sorted(TERMINAL_ACTIONS), ids=lambda s: s.value)
def test_terminal_actions_have_no_exit(terminal: ActionStatus) -> None:
    assert ACTION_TRANSITIONS[terminal] == frozenset()
    for target in ActionStatus:
        assert not can_transition_action(terminal, target)


def test_uncertain_can_only_be_resolved_by_reconciliation() -> None:
    assert ACTION_TRANSITIONS[A.UNCERTAIN] == {A.COMPLETED, A.FAILED}
    assert not can_transition_action(A.UNCERTAIN, A.EXECUTING)


def test_pending_cannot_jump_to_execution() -> None:
    assert not can_transition_action(A.PENDING_APPROVAL, A.EXECUTING)
    assert not can_transition_action(A.PENDING_APPROVAL, A.COMPLETED)


def test_editable_and_executable_sets() -> None:
    assert {A.PENDING_APPROVAL, A.APPROVED, A.FAILED} == EDITABLE_ACTION_STATUSES
    assert {A.APPROVED, A.FAILED} == EXECUTABLE_ACTION_STATUSES
    for status in EXECUTABLE_ACTION_STATUSES:
        assert can_transition_action(status, A.EXECUTING)


def _expected_workflow(current: WorkflowStatus, target: WorkflowStatus) -> bool:
    if current == target:
        return current not in TERMINAL_WORKFLOW_STATUSES
    return target in WORKFLOW_TRANSITIONS[current]


@pytest.mark.parametrize(("current", "target"), ALL_WORKFLOW_PAIRS, ids=lambda s: s.value)
def test_every_workflow_pair(current: WorkflowStatus, target: WorkflowStatus) -> None:
    allowed = _expected_workflow(current, target)
    assert can_transition_workflow(current, target) is allowed
    if allowed:
        ensure_workflow_transition(current, target)
    else:
        with pytest.raises(InvalidStateTransition) as info:
            ensure_workflow_transition(current, target)
        assert isinstance(info.value, Conflict)
        assert f"'{current}'" in info.value.message
        assert f"'{target}'" in info.value.message


@pytest.mark.parametrize("status", list(WorkflowStatus), ids=lambda s: s.value)
def test_workflow_self_transition_rule(status: WorkflowStatus) -> None:
    # Re-entering a non-terminal status is allowed (graph nodes re-run after crashes); terminal ones are final.
    assert can_transition_workflow(status, status) is (status in ACTIVE_WORKFLOW_STATUSES)


@pytest.mark.parametrize("status", sorted(TERMINAL_WORKFLOW_STATUSES), ids=lambda s: s.value)
def test_terminal_workflows_have_no_exit(status: WorkflowStatus) -> None:
    assert WORKFLOW_TRANSITIONS[status] == frozenset()
    assert not any(can_transition_workflow(status, t) for t in WorkflowStatus)


def test_analysis_pipeline_is_linear() -> None:
    chain = [W.RECEIVED, W.NORMALIZED, W.CLASSIFIED, W.EXTRACTED, W.VALIDATED, W.ACTION_PROPOSED]
    for current, nxt in itertools.pairwise(chain):
        assert can_transition_workflow(current, nxt)
        for skipped in chain[chain.index(nxt) + 1 :]:
            assert not can_transition_workflow(current, skipped), (current, skipped)
    for status in chain:
        assert can_transition_workflow(status, W.FAILED)
        assert can_transition_workflow(status, W.CANCELLED)


def test_failed_runs_are_recoverable_to_every_non_initial_status() -> None:
    assert WORKFLOW_TRANSITIONS[W.FAILED] == set(WorkflowStatus) - {W.RECEIVED}


def test_reanalysis_paths() -> None:
    for status in (W.PENDING_APPROVAL, W.APPROVED, W.FAILED):
        assert can_transition_workflow(status, W.NORMALIZED)
    assert not can_transition_workflow(W.EXECUTING, W.NORMALIZED)


# --------------------------------------------------------------------------- derive_review_status


def _oracle(statuses: list[ActionStatus]) -> WorkflowStatus:
    live = [s for s in statuses if s != A.SUPERSEDED]
    if not live:
        return W.COMPLETED
    present = set(live)
    if A.EXECUTING in present:
        return W.EXECUTING
    if present & {A.FAILED, A.UNCERTAIN}:
        return W.FAILED
    if A.PENDING_APPROVAL in present:
        return W.PENDING_APPROVAL
    if A.APPROVED in present:
        return W.APPROVED
    if present == {A.REJECTED}:
        return W.REJECTED
    if present <= {A.REJECTED, A.CANCELLED}:
        return W.CANCELLED
    return W.COMPLETED


@given(st.lists(st.sampled_from(list(ActionStatus)), max_size=12))
def test_derive_review_status_matches_oracle(statuses: list[ActionStatus]) -> None:
    assert derive_review_status(statuses) is _oracle(statuses)


@given(st.lists(st.sampled_from(list(ActionStatus)), max_size=8), st.randoms())
def test_derive_review_status_is_order_independent(statuses: list[ActionStatus], rnd) -> None:  # type: ignore[no-untyped-def]
    shuffled = list(statuses)
    rnd.shuffle(shuffled)
    assert derive_review_status(statuses) is derive_review_status(shuffled)


@given(st.lists(st.sampled_from(list(ActionStatus)), max_size=8), st.integers(min_value=1, max_value=4))
def test_superseded_actions_are_ignored(statuses: list[ActionStatus], extra: int) -> None:
    assert derive_review_status(statuses + [A.SUPERSEDED] * extra) is derive_review_status(statuses)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], W.COMPLETED),
        ([A.SUPERSEDED], W.COMPLETED),
        ([A.EXECUTING, A.FAILED, A.PENDING_APPROVAL], W.EXECUTING),
        ([A.UNCERTAIN, A.PENDING_APPROVAL], W.FAILED),
        ([A.FAILED, A.APPROVED], W.FAILED),
        ([A.PENDING_APPROVAL, A.APPROVED, A.COMPLETED], W.PENDING_APPROVAL),
        ([A.APPROVED, A.COMPLETED, A.REJECTED], W.APPROVED),
        ([A.REJECTED, A.REJECTED], W.REJECTED),
        ([A.REJECTED, A.CANCELLED], W.CANCELLED),
        ([A.CANCELLED], W.CANCELLED),
        ([A.CANCELLED, A.COMPLETED], W.COMPLETED),
        ([A.REJECTED, A.COMPLETED], W.COMPLETED),
        ([A.COMPLETED, A.SUPERSEDED], W.COMPLETED),
    ],
)
def test_derive_review_status_examples(statuses: list[ActionStatus], expected: WorkflowStatus) -> None:
    assert derive_review_status(statuses) is expected


def test_derived_status_is_reachable_from_pending_approval() -> None:
    # review_gate moves a run from pending_approval to whatever its actions imply.
    for statuses in itertools.product(ActionStatus, repeat=2):
        target = derive_review_status(list(statuses))
        assert can_transition_workflow(W.PENDING_APPROVAL, target), (statuses, target)
