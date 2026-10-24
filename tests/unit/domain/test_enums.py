"""Invariants of the shared domain enumerations."""

from __future__ import annotations

from app.domain.enums import (
    ACTION_TARGETS,
    ACTIVE_WORKFLOW_STATUSES,
    OPEN_ACTION_STATUSES,
    RELEASED_ACTION_STATUSES,
    TERMINAL_WORKFLOW_STATUSES,
    ActionStatus,
    ActionType,
    TargetIntegration,
    WorkflowStatus,
)
from app.schemas.actions import PAYLOAD_KIND_BY_ACTION, REQUIRED_PERMISSIONS


def test_workflow_status_partition() -> None:
    assert set(WorkflowStatus) == TERMINAL_WORKFLOW_STATUSES | ACTIVE_WORKFLOW_STATUSES
    assert not TERMINAL_WORKFLOW_STATUSES & ACTIVE_WORKFLOW_STATUSES
    assert {WorkflowStatus.COMPLETED, WorkflowStatus.REJECTED, WorkflowStatus.CANCELLED} == TERMINAL_WORKFLOW_STATUSES


def test_every_action_type_has_target_payload_kind_and_permissions() -> None:
    assert set(ACTION_TARGETS) == set(ActionType)
    assert set(PAYLOAD_KIND_BY_ACTION) == set(ActionType)
    assert set(REQUIRED_PERMISSIONS) == set(ActionType)
    for action_type, kind in PAYLOAD_KIND_BY_ACTION.items():
        assert kind == action_type.value
    assert ACTION_TARGETS[ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM] is TargetIntegration.AZURE_DEVOPS
    assert ACTION_TARGETS[ActionType.MARK_NO_ACTION] is TargetIntegration.NONE


def test_released_and_open_action_statuses_are_disjoint() -> None:
    assert not RELEASED_ACTION_STATUSES & OPEN_ACTION_STATUSES
    assert ActionStatus.COMPLETED not in RELEASED_ACTION_STATUSES  # completed work keeps its idempotency key
    assert ActionStatus.EXECUTING not in OPEN_ACTION_STATUSES


def test_enum_values_are_stable_strings() -> None:
    # Values are persisted as VARCHAR; renaming one would need a data migration.
    assert WorkflowStatus.PENDING_APPROVAL == "pending_approval"
    assert ActionStatus.UNCERTAIN.value == "uncertain"
    assert str(ActionType.PROPOSE_EMAIL_REPLY) == "propose_email_reply"
