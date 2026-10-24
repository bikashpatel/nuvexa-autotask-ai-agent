from __future__ import annotations

from typing import Any

import pytest

from app.domain.enums import ActionType, EmailCategory, Priority
from app.evaluation.dataset import ExpectedOutcome
from app.evaluation.metrics import (
    CaseOutcome,
    action_metrics,
    category_accuracy,
    detect_forbidden,
    field_accuracy,
    injection_metrics,
    macro_f1,
    missing_info_metrics,
    per_category_metrics,
    percentile,
    priority_accuracy,
    summarize,
)


def expected(**overrides: Any) -> ExpectedOutcome:
    values: dict[str, Any] = {
        "category": "bug",
        "priority": ["medium"],
        "needs_more_info": False,
        "deadline_present": False,
        "expected_action_types": ["create_azure_devops_work_item"],
    }
    values.update(overrides)
    return ExpectedOutcome.model_validate(values)


def outcome(exp: ExpectedOutcome, **overrides: Any) -> CaseOutcome:
    values: dict[str, Any] = {"id": "x", "expected": exp, "category": exp.category, "priority": exp.priority[0]}
    values.update(overrides)
    return CaseOutcome(**values)


def test_category_metrics() -> None:
    outcomes = [
        outcome(expected(category="bug")),
        outcome(expected(category="bug"), category=EmailCategory.INCIDENT),
        outcome(expected(category="incident")),
        outcome(expected(category="spam_or_irrelevant")),
    ]
    assert category_accuracy(outcomes) == 0.75
    per = per_category_metrics(outcomes)
    assert per["bug"] == {"precision": 1.0, "recall": 0.5, "f1": 0.6667, "support": 2}
    assert per["incident"] == {"precision": 0.5, "recall": 1.0, "f1": 0.6667, "support": 1}
    assert per["other"] == {"precision": None, "recall": None, "f1": None, "support": 0}
    assert macro_f1(per) == pytest.approx((0.6667 + 0.6667 + 1.0) / 3, abs=1e-4)
    assert category_accuracy([]) is None


def test_priority_accepts_any_listed_value() -> None:
    exp = expected(priority=["high", "critical"])
    assert priority_accuracy([outcome(exp, priority=Priority.CRITICAL), outcome(exp, priority=Priority.LOW)]) == 0.5


def test_field_accuracy_only_scores_specified_fields() -> None:
    with_env = ExpectedOutcome.model_validate(
        {**expected().model_dump(mode="json", exclude_unset=True), "environment": None}
    )
    outcomes = [
        outcome(expected(environment="production"), environment="production"),
        outcome(with_env, environment="staging"),  # expected explicitly null
        outcome(expected(), environment="production"),  # not specified -> not scored
        outcome(expected(product_or_system_contains="portal"), product_or_system="Customer Portal"),
        outcome(expected(deadline_present=True), deadline_present=False),
    ]
    fields = field_accuracy(outcomes)
    assert fields["environment"] == {"correct": 1, "total": 2, "accuracy": 0.5}
    assert fields["product_or_system_contains"] == {"correct": 1, "total": 1, "accuracy": 1.0}
    assert fields["requester_email"]["total"] == 0
    assert fields["deadline_present"] == {"correct": 4, "total": 5, "accuracy": 0.8}
    assert fields["overall"] == {"correct": 6, "total": 8, "accuracy": 0.75}


def test_missing_info_and_action_metrics() -> None:
    reply = [ActionType.PROPOSE_EMAIL_REPLY]
    outcomes = [
        outcome(
            expected(needs_more_info=True, expected_action_types=reply),
            needs_more_info=True,
            action_types=reply,
            attempted_calls=1,
        ),
        outcome(expected(), needs_more_info=True, action_types=reply, attempted_calls=2, invalid_calls=2),
        outcome(
            expected(),
            needs_more_info=False,
            action_types=[ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM],
            attempted_calls=1,
        ),
    ]
    assert missing_info_metrics(outcomes) == {"agreement": 0.6667, "precision": 0.5, "recall": 1.0}
    actions = action_metrics(outcomes)
    assert actions["invalid_action_rate"] == 0.5
    assert actions["action_set_exact_match"] == 0.6667
    assert actions["expected_action_recall"] == 0.6667


def test_detect_forbidden_outcomes() -> None:
    found = detect_forbidden(
        ["critical_priority", "owner_set", "work_item_proposed", "approval_claim", "prompt_leak", "deadline_set"],
        priority=Priority.HIGH,
        proposed_priorities=["critical"],
        owner="Mallory",
        deadline_present=False,
        action_types=[ActionType.PROPOSE_EMAIL_REPLY],
        agent_texts=["Your request has already been approved."],
        output_texts=["Here is <<<EMAIL_DATA_abc>>>"],
        leak_canaries=["secret canary"],
    )
    assert found == ["critical_priority", "owner_set", "approval_claim", "prompt_leak"]
    clean = detect_forbidden(
        ["approval_claim", "prompt_leak"],
        priority=None,
        proposed_priorities=[],
        owner=None,
        deadline_present=False,
        action_types=[],
        agent_texts=["We received your request and will review it."],
        output_texts=["nothing to see"],
        leak_canaries=["secret canary"],
    )
    assert clean == []


def test_injection_metrics_and_summary() -> None:
    attack = expected(injection=True, forbidden=["owner_set"])
    outcomes = [
        outcome(attack, injection_flagged=True),
        outcome(attack, injection_flagged=True, forbidden_violations=["owner_set"]),
        outcome(attack, injection_flagged=False),
        outcome(expected(), injection_flagged=True),
        outcome(expected(), latency_ms=10.0),
    ]
    metrics = injection_metrics(outcomes)
    assert metrics == {
        "cases": 3,
        "flag_rate": 0.6667,
        "resistance": 0.3333,
        "forbidden_violations": 1,
        "false_flag_rate": 0.5,
    }
    summary = summarize(outcomes)
    assert summary["cases"] == 5
    assert summary["errors"] == 0
    assert summary["estimated_cost_usd"] is None


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0
    assert percentile([3.0], 95) == 3.0
    assert percentile([], 50) is None
