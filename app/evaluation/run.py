"""Run an analysis agent over the evaluation dataset: classify -> extract -> validate -> plan.

    uv run python -m app.evaluation.run --provider heuristic
    uv run python -m app.evaluation.run --provider openai --limit 5     # paid: uses OPENAI_* settings

Results are printed as a markdown table and written as JSON (default `evaluation-results/<timestamp>.json`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.agents.base import AgentError, EmailAnalysisAgent, LLMUsage
from app.agents.factory import build_analysis_agent
from app.agents.prompts import PROMPT_LEAK_CANARIES
from app.agents.sanitize import build_email_context
from app.agents.validation import validate_analysis
from app.core.config import LLMProvider, Settings
from app.domain.enums import ActionType
from app.evaluation.dataset import DEFAULT_DATASET, EvalEmail, load_dataset
from app.evaluation.metrics import CaseOutcome, detect_forbidden, summarize, summarize_by_split
from app.schemas.ai import AI_SCHEMA_VERSION, PLANNING_TOOLS, PlannedToolCall, PlanningContext
from app.schemas.settings import AzureDevOpsDefaults, ConfidenceThresholds, TaxonomyConfig

DEFAULT_MAX_CHARS = 12_000
_AGENT_VOICE_KEYS = frozenset({"rationale", "expected_impact", "body", "subject", "reason"})


class EvaluationReport(BaseModel):
    dataset: str
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    generated_at: datetime
    metrics: dict[str, Any]
    metrics_by_split: dict[str, dict[str, Any]] = Field(default_factory=dict)
    cases: list[CaseOutcome] = Field(default_factory=list)

    def to_json(self) -> str:
        data = self.model_dump(mode="json")
        for raw, case in zip(data["cases"], self.cases, strict=True):
            raw["expected"] = case.expected.model_dump(mode="json", exclude_unset=True)
        return json.dumps(data, indent=2, ensure_ascii=False)


def default_planning_context(category_work_item_type: str | None = None) -> PlanningContext:
    defaults = AzureDevOpsDefaults()
    return PlanningContext(
        allowed_action_types=list(ActionType),
        allowed_work_item_types=defaults.allowed_work_item_types,
        default_work_item_type=defaults.work_item_type,
        category_work_item_type=category_work_item_type,
        azure_devops_available=True,
    )


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def _invalid_calls(calls: list[PlannedToolCall], context: PlanningContext, expected: list[ActionType]) -> int:
    invalid = 0
    for call in calls:
        _, model = PLANNING_TOOLS[call.tool_name]
        try:
            model.model_validate(call.arguments)
        except ValidationError:
            invalid += 1
            continue
        if call.action_type not in context.allowed_action_types or call.action_type not in expected:
            invalid += 1
    return invalid


def _add_usage(outcome: CaseOutcome, usage: LLMUsage) -> None:
    outcome.input_tokens += usage.input_tokens or 0
    outcome.output_tokens += usage.output_tokens or 0
    if usage.estimated_cost_usd is not None:
        outcome.estimated_cost_usd = round((outcome.estimated_cost_usd or 0.0) + usage.estimated_cost_usd, 6)


async def evaluate_case(
    agent: EmailAnalysisAgent,
    case: EvalEmail,
    *,
    taxonomy: TaxonomyConfig,
    thresholds: ConfidenceThresholds,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> CaseOutcome:
    ctx = build_email_context(
        email_id=case.email_id,
        subject=case.subject,
        sender_name=case.sender_name,
        sender_address=case.sender_address,
        to=case.to,
        cc=case.cc,
        received_at=case.received_at,
        body_text=case.body,
        has_attachments=bool(case.attachment_names),
        attachment_names=case.attachment_names,
        max_chars=max_chars,
    )
    outcome = CaseOutcome(
        id=case.id,
        split=case.split,
        expected=case.expected,
        injection_flagged=bool(ctx.injection_flags),
        injection_rules=[f.rule for f in ctx.injection_flags],
    )
    started = time.perf_counter()
    try:
        classified = await agent.classify(ctx, taxonomy)
        _add_usage(outcome, classified.usage)
        extracted = await agent.extract(ctx, classified.output)
        _add_usage(outcome, extracted.usage)
        validated = validate_analysis(
            ctx,
            classified.output,
            extracted.output,
            thresholds=thresholds,
            enabled_categories=taxonomy.enabled_categories,
        )
        context = default_planning_context(
            AzureDevOpsDefaults().category_work_item_types.get(validated.classification.category)
        )
        planned = await agent.plan(ctx, validated.classification, extracted.output, context)
        _add_usage(outcome, planned.usage)
    except AgentError as exc:
        outcome.error = f"{exc.code}: {exc.message}"
        outcome.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return outcome
    outcome.latency_ms = round((time.perf_counter() - started) * 1000, 2)

    classification, extraction = validated.classification, validated.extraction
    outcome.category = classification.category
    outcome.priority = classification.priority
    outcome.needs_more_info = classification.needs_more_info
    outcome.environment = extraction.environment
    outcome.product_or_system = extraction.product_or_system
    outcome.deadline_present = extraction.deadline is not None
    outcome.requester_email = extraction.requester_email
    outcome.action_types = list(dict.fromkeys(c.action_type for c in planned.tool_calls))
    outcome.attempted_calls = len(planned.tool_calls) + len(planned.rejected_calls)
    outcome.invalid_calls = len(planned.rejected_calls) + _invalid_calls(
        planned.tool_calls, context, case.expected.expected_action_types
    )
    outcome.confidence_score = validated.confidence_score
    outcome.confidence_bucket = validated.confidence_bucket.value
    outcome.warnings = len(validated.warnings)

    tool_args = [c.arguments for c in planned.tool_calls]
    agent_texts = [classification.rationale] + [
        s for args in tool_args for key, value in args.items() if key in _AGENT_VOICE_KEYS for s in _strings(value)
    ]
    output_texts = [
        *_strings(classification.model_dump(mode="json")),
        *_strings(extracted.output.model_dump(mode="json")),
    ]
    output_texts += _strings(tool_args)
    outcome.forbidden_violations = detect_forbidden(
        case.expected.forbidden,
        priority=classification.priority,
        proposed_priorities=[str(a.get("priority", "")) for a in tool_args],
        owner=extraction.suggested_owner,
        deadline_present=outcome.deadline_present,
        action_types=outcome.action_types,
        agent_texts=agent_texts,
        output_texts=output_texts,
        leak_canaries=PROMPT_LEAK_CANARIES,
    )
    return outcome


async def run_evaluation(
    agent: EmailAnalysisAgent,
    cases: Sequence[EvalEmail],
    *,
    dataset_name: str = DEFAULT_DATASET.name,
    taxonomy: TaxonomyConfig | None = None,
    thresholds: ConfidenceThresholds | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> EvaluationReport:
    taxonomy = taxonomy or TaxonomyConfig()
    thresholds = thresholds or ConfidenceThresholds()
    outcomes = [
        await evaluate_case(agent, case, taxonomy=taxonomy, thresholds=thresholds, max_chars=max_chars)
        for case in cases
    ]
    return EvaluationReport(
        dataset=dataset_name,
        provider=agent.provider_name,
        model=agent.model_name,
        prompt_version=agent.prompt_version,
        schema_version=AI_SCHEMA_VERSION,
        generated_at=datetime.now(UTC),
        metrics=summarize(outcomes),
        metrics_by_split=summarize_by_split(outcomes) if len({o.split for o in outcomes}) > 1 else {},
        cases=outcomes,
    )


def _fmt(value: object, pct: bool = True) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and pct:
        return f"{value * 100:.1f}%"
    return str(value)


def _metric_rows(m: dict[str, Any]) -> list[tuple[str, str]]:
    fields, actions, injection, latency = m["fields"], m["actions"], m["injection"], m["latency_ms"]
    return [
        ("Cases (errors)", f"{m['cases']} ({m['errors']})"),
        ("Category accuracy", _fmt(m["category_accuracy"])),
        ("Macro F1", _fmt(m["macro_f1"])),
        ("Priority accuracy", _fmt(m["priority_accuracy"])),
        ("Field accuracy (overall)", _fmt(fields["overall"]["accuracy"])),
        ("- environment", _fmt(fields["environment"]["accuracy"])),
        ("- product/system", _fmt(fields["product_or_system_contains"]["accuracy"])),
        ("- deadline presence", _fmt(fields["deadline_present"]["accuracy"])),
        ("- requester email", _fmt(fields["requester_email"]["accuracy"])),
        ("Missing-info agreement", _fmt(m["missing_info"]["agreement"])),
        ("Invalid action proposal rate", _fmt(actions["invalid_action_rate"])),
        ("Action set exact match", _fmt(actions["action_set_exact_match"])),
        ("Injection flag rate", _fmt(injection["flag_rate"])),
        ("Injection resistance", _fmt(injection["resistance"])),
        ("False injection flags (benign)", _fmt(injection["false_flag_rate"])),
        ("Latency p50 / p95 (ms)", f"{_fmt(latency['p50'], False)} / {_fmt(latency['p95'], False)}"),
        ("Tokens (in / out)", f"{m['tokens']['input']} / {m['tokens']['output']}"),
        ("Estimated cost (USD)", _fmt(m["estimated_cost_usd"], pct=False)),
    ]


def render_markdown(report: EvaluationReport) -> str:
    columns = {"all": report.metrics, **report.metrics_by_split}
    table = [_metric_rows(m) for m in columns.values()]
    lines = [
        f"## Evaluation: {report.provider} / {report.model} (prompt {report.prompt_version}, dataset {report.dataset})",
        "",
        "| Metric | " + " | ".join(columns) + " |",
        "|---|" + "---|" * len(columns),
    ]
    for index, (name, _) in enumerate(table[0]):
        lines.append(f"| {name} | " + " | ".join(rows[index][1] for rows in table) + " |")
    lines += ["", "| Category (all) | Precision | Recall | F1 | Support |", "|---|---|---|---|---|"]
    for category, cm in report.metrics["per_category"].items():
        lines.append(
            f"| {category} | {_fmt(cm['precision'])} | {_fmt(cm['recall'])} | {_fmt(cm['f1'])} | {cm['support']} |"
        )
    misses = [c for c in report.cases if c.error or c.category != c.expected.category]
    if misses:
        lines += ["", "Misclassified / failed: " + ", ".join(f"{c.id} ({c.error or c.category})" for c in misses)]
    return "\n".join(lines)


def _settings_for(provider: LLMProvider) -> Settings:
    return Settings(llm_provider=provider)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate an analysis agent on the synthetic email dataset.")
    parser.add_argument("--provider", choices=[p.value for p in LLMProvider], default=LLMProvider.HEURISTIC.value)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=None, help="JSON output path")
    parser.add_argument("--limit", type=int, default=None, help="Evaluate only the first N records")
    args = parser.parse_args(argv)

    provider = LLMProvider(args.provider)
    try:
        settings = _settings_for(provider)
    except ValidationError as exc:
        print(f"Configuration error for provider '{provider.value}': {exc.errors()[0].get('msg')}", file=sys.stderr)
        return 2
    if provider is not LLMProvider.HEURISTIC:
        print(
            f"Calling {provider.value} model '{settings.llm_model_label}' - this makes paid API calls.", file=sys.stderr
        )
    cases = load_dataset(args.dataset)
    if args.limit is not None:
        cases = cases[: max(0, args.limit)]
    agent = build_analysis_agent(settings)

    async def _run() -> EvaluationReport:
        try:
            return await run_evaluation(
                agent, cases, dataset_name=args.dataset.name, max_chars=settings.llm_max_email_chars
            )
        finally:
            close = getattr(agent, "aclose", None)
            if callable(close):
                await close()

    report = asyncio.run(_run())
    output: Path = (
        args.output or Path("evaluation-results") / f"{report.generated_at:%Y%m%dT%H%M%SZ}-{provider.value}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.to_json(), encoding="utf-8")
    print(render_markdown(report))
    print(f"\nWrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
