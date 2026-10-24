"""LangGraph state. Only small JSON-safe values; the database stays the system of record."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

DecisionType = Literal["refresh", "execute", "reconcile", "reanalyze", "cancel"]


class ReviewDecision(TypedDict, total=False):
    """Resume payload. A *request* only — authorization is re-checked by the services."""

    type: DecisionType
    action_id: str
    actor_user_id: str
    version: int
    payload_hash: str


class WorkflowState(TypedDict, total=False):
    run_id: str
    tenant_id: str
    attempt: int
    halt: bool
    classification: dict[str, Any] | None
    extraction: dict[str, Any] | None
    analysis_id: str | None
    action_ids: list[str]
    decision: ReviewDecision | None
    review_done: bool
