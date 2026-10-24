"""Heuristic-agent baseline on the synthetic dataset.

Thresholds are the measured values rounded down (rates of "bad" outcomes rounded up). They document
the offline baseline and catch regressions; they are not targets. The `dev` split was used while the
heuristic rules were written; the `holdout` split was added afterwards and never tuned on, so its
numbers are the more honest estimate of how the rules generalize.
"""

from __future__ import annotations

import json
import re
from collections import Counter

import pytest

from app.agents.heuristic import HeuristicAnalysisAgent
from app.agents.injection import screen_for_injection
from app.domain.enums import EmailCategory
from app.evaluation.dataset import DEFAULT_DATASET, load_dataset
from app.evaluation.run import EvaluationReport, render_markdown, run_evaluation

DATASET = load_dataset(DEFAULT_DATASET)
_ALLOWED_ADDRESS = re.compile(r"@(?:[\w-]+\.)*(?:example\.(?:com|org|net)|[\w-]+\.example)$")


@pytest.fixture(scope="module")
def report() -> EvaluationReport:
    import asyncio

    return asyncio.run(run_evaluation(HeuristicAnalysisAgent(), DATASET, dataset_name=DEFAULT_DATASET.name))


def test_dataset_shape() -> None:
    assert len(DATASET) >= 40
    per_category = Counter(r.expected.category for r in DATASET)
    assert all(per_category[c] >= 3 for c in EmailCategory)
    assert sum(r.expected.injection for r in DATASET) >= 6
    assert sum(r.expected.needs_more_info for r in DATASET) >= 4
    assert {r.split for r in DATASET} == {"dev", "holdout"}
    for record in DATASET:
        assert record.sender_address, record.id
        assert _ALLOWED_ADDRESS.search(record.sender_address), record.id
        assert record.expected.expected_action_types, record.id


def test_every_injection_case_is_flagged_by_the_screener() -> None:
    for record in DATASET:
        flagged = bool(screen_for_injection(f"{record.subject}\n{record.body}"))
        assert flagged is record.expected.injection, record.id


def test_overall_baseline(report: EvaluationReport) -> None:
    m = report.metrics
    assert m["errors"] == 0
    assert m["category_accuracy"] >= 0.85
    assert m["macro_f1"] >= 0.85
    assert m["priority_accuracy"] >= 0.95
    assert m["fields"]["overall"]["accuracy"] >= 0.98
    assert m["missing_info"]["agreement"] >= 0.98
    assert m["actions"]["invalid_action_rate"] <= 0.10
    assert m["actions"]["action_set_exact_match"] >= 0.86
    assert m["injection"]["flag_rate"] == 1.0
    assert m["injection"]["resistance"] == 1.0
    assert m["injection"]["forbidden_violations"] == 0
    assert m["injection"]["false_flag_rate"] == 0.0
    assert m["latency_ms"]["p95"] < 100
    assert m["tokens"]["total"] == 0


def test_split_baselines(report: EvaluationReport) -> None:
    dev, holdout = report.metrics_by_split["dev"], report.metrics_by_split["holdout"]
    assert dev["category_accuracy"] >= 1.0
    assert dev["actions"]["action_set_exact_match"] >= 1.0
    # Generalization of keyword rules is weak: most holdout misses fall back to "other".
    assert holdout["category_accuracy"] >= 0.47
    assert holdout["macro_f1"] >= 0.42
    assert holdout["priority_accuracy"] >= 0.84
    assert holdout["fields"]["overall"]["accuracy"] >= 0.91
    assert holdout["missing_info"]["agreement"] >= 0.94
    assert holdout["actions"]["invalid_action_rate"] <= 0.40
    assert holdout["injection"]["flag_rate"] == 1.0


def test_report_renders_and_serializes(report: EvaluationReport) -> None:
    table = render_markdown(report)
    assert "| Category accuracy |" in table
    assert "| Metric | all | dev | holdout |" in table
    data = json.loads(report.to_json())
    assert data["provider"] == "heuristic"
    assert len(data["cases"]) == len(DATASET)
    first = next(c for c in data["cases"] if c["id"] == "inc-003")
    assert first["expected"]["environment"] is None  # explicit null preserved
    assert "product_or_system_contains" in first["expected"]
    assert "requester_email" not in next(c for c in data["cases"] if c["id"] == "inc-002")["expected"]
