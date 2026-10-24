"""LangGraph workflow for one email analysis run (thread_id == workflow_runs.id).

prepare → classify → extract → validate → plan → review_gate ⇄ wait_for_review (interrupt)
wait_for_review → execute_action | reconcile_action → review_gate
wait_for_review → reanalyze → prepare
review_gate / analysis halt → finalize → END

Nodes open short database transactions and never hold one across an LLM or integration call.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.base import AgentError, AgentProviderError, EmailAnalysisAgent, LLMUsage
from app.agents.sanitize import build_email_context
from app.agents.validation import validate_analysis
from app.core.config import Settings
from app.core.errors import AppError, NotFound
from app.core.security import compute_payload_hash
from app.core.timeutil import utcnow
from app.db.models import EmailAnalysis, EmailMessage, ProposedAction, WorkflowRun
from app.domain.enums import (
    ACTION_TARGETS,
    ActionStatus,
    ActionType,
    ActorType,
    AuditOutcome,
    EmailAnalysisStatus,
    FailureCategory,
    WorkflowEventType,
    WorkflowStatus,
)
from app.domain.state_machine import derive_review_status, ensure_workflow_transition
from app.observability.logging import get_logger
from app.observability.tracing import start_span
from app.schemas.actions import REQUIRED_PERMISSIONS
from app.schemas.ai import (
    AI_SCHEMA_VERSION,
    ClassificationOutput,
    ExtractionOutput,
    PlanningContext,
    ValidatedAnalysis,
    ValidatedExtraction,
)
from app.schemas.email import EmailContext, InjectionFlag
from app.services.action_policy import ActionPolicy, idempotency_key
from app.services.approval_service import supersede_open_actions
from app.services.audit import record_audit, record_permission_denied, record_workflow_event
from app.services.authz import AuditedPermissionDenied
from app.services.execution_service import ExecutionService
from app.services.identity import load_actor
from app.services.settings_service import get_tenant_settings
from app.workflows.state import WorkflowState

log = get_logger(__name__)

W = WorkflowStatus
_TERMINAL_REVIEW = (W.COMPLETED, W.REJECTED, W.CANCELLED)


@dataclass
class WorkflowDeps:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    agent: EmailAnalysisAgent
    execution: ExecutionService


class _Halt(Exception):
    """Run was cancelled while analysis was in progress."""


def set_run_status(run: WorkflowRun, status: WorkflowStatus, step: str | None = None) -> None:
    ensure_workflow_transition(run.status, status)
    run.status = status
    run.current_step = step or status.value


def email_context(email: EmailMessage, settings: Settings) -> EmailContext:
    return build_email_context(
        email_id=email.id,
        subject=email.subject or "",
        sender_name=email.sender_name,
        sender_address=email.sender_address,
        to=[r.get("address") or "" for r in email.to_recipients if r.get("address")],
        cc=[r.get("address") or "" for r in email.cc_recipients if r.get("address")],
        received_at=email.received_at,
        body_text=email.body_text or "",
        has_attachments=email.has_attachments,
        attachment_names=[str(a.get("name", "")) for a in email.attachments],
        max_chars=settings.llm_max_email_chars,
    )


def _usage_details(usage: LLMUsage) -> dict[str, Any]:
    return usage.model_dump(mode="json")


def _accumulate(run: WorkflowRun, usage: LLMUsage) -> None:
    m = dict(run.metrics or {})
    m["llm_calls"] = int(m.get("llm_calls", 0)) + 1
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        m[key] = int(m.get(key, 0)) + int(getattr(usage, key) or 0)
    if usage.estimated_cost_usd is not None:
        m["estimated_cost_usd"] = round(float(m.get("estimated_cost_usd", 0.0)) + usage.estimated_cost_usd, 6)
    m["llm_latency_ms"] = int(m.get("llm_latency_ms", 0)) + usage.latency_ms
    run.metrics = m


def _sanitized_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """Tool arguments for the timeline: short structural fields only (no free text from the email)."""
    keep = ("title", "work_item_type", "priority", "tags", "subject", "due_date")
    out: dict[str, Any] = {}
    for key in keep:
        if key in args:
            value = args[key]
            out[key] = value[:120] if isinstance(value, str) else value
    out["argument_keys"] = sorted(args)
    return out


async def _thread_duplicate_warnings(session: AsyncSession, email: EmailMessage) -> list[str]:
    """Warn when another email in the same conversation already has a live or completed work item.

    Different emails are not replays, so idempotency keys do not block them; a human decides.
    """
    if not email.conversation_id:
        return []
    rows = (
        await session.execute(
            select(ProposedAction.title, ProposedAction.status, ProposedAction.external_id, EmailMessage.subject)
            .join(EmailMessage, EmailMessage.id == ProposedAction.email_message_id)
            .where(
                EmailMessage.tenant_id == email.tenant_id,
                EmailMessage.connected_account_id == email.connected_account_id,
                EmailMessage.conversation_id == email.conversation_id,
                EmailMessage.id != email.id,
                ProposedAction.action_type == ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM,
                ProposedAction.status.not_in([ActionStatus.REJECTED, ActionStatus.CANCELLED, ActionStatus.SUPERSEDED]),
            )
            .limit(3)
        )
    ).all()
    warnings = []
    for title, status, external_id, subject in rows:
        ref = f" (#{external_id})" if external_id else ""
        warnings.append(
            f"Possible duplicate: this conversation already has work item '{title}'{ref} ({status.value}) "
            f"from email '{subject[:80]}'. Reject this proposal if it covers the same issue."
        )
    return warnings


def build_graph(deps: WorkflowDeps) -> StateGraph[WorkflowState]:
    settings = deps.settings
    sf = deps.session_factory

    async def load(session: AsyncSession, run_id: str, *, for_update: bool = False) -> tuple[WorkflowRun, EmailMessage]:
        stmt = select(WorkflowRun).where(WorkflowRun.id == uuid.UUID(run_id))
        if for_update:
            stmt = stmt.with_for_update()
        run = (await session.execute(stmt)).scalar_one_or_none()
        if run is None:
            raise NotFound("Workflow run not found.")
        email = await session.get(EmailMessage, run.email_message_id)
        if email is None:
            raise NotFound("Email not found.")
        return run, email

    async def fail(run_id: str, step: str, exc: Exception) -> None:
        category = (
            FailureCategory.LLM_PROVIDER
            if isinstance(exc, AgentProviderError)
            else FailureCategory.LLM_OUTPUT
            if isinstance(exc, AgentError)
            else FailureCategory.INTERNAL
        )
        message = exc.message if isinstance(exc, AppError) else type(exc).__name__
        async with sf() as s:
            run, email = await load(s, run_id, for_update=True)
            if run.status not in _TERMINAL_REVIEW:
                set_run_status(run, W.FAILED, step)
            run.error_category = category
            run.error_message = message[:2000]
            email.analysis_status = EmailAnalysisStatus.FAILED
            record_workflow_event(
                s,
                tenant_id=run.tenant_id,
                workflow_run_id=run.id,
                step=step,
                event_type=WorkflowEventType.STEP_FAILED,
                status="failure",
                message=message[:500],
                details={"failure_category": category.value, "retryable": isinstance(exc, AgentProviderError)},
            )
            await s.commit()

    def analysis_step(name: str, fn: Callable[[WorkflowState], Awaitable[dict[str, Any]]]):  # type: ignore[no-untyped-def]
        async def node(state: WorkflowState) -> dict[str, Any]:
            started = time.perf_counter()
            with start_span(f"nuvexa.workflow.{name}", {"nuvexa.workflow_run_id": state["run_id"]}):
                try:
                    update = await fn(state)
                except _Halt:
                    return {"halt": True}
                except Exception as exc:
                    log.warning("workflow_step_failed", step=name, run_id=state["run_id"], error=type(exc).__name__)
                    await fail(state["run_id"], name, exc)
                    raise
            log.info(
                "workflow_step_completed",
                step=name,
                run_id=state["run_id"],
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            return update

        return node

    async def _guard(run: WorkflowRun) -> None:
        if run.status is W.CANCELLED:
            raise _Halt

    # ------------------------------------------------------------------ analysis nodes

    async def prepare(state: WorkflowState) -> dict[str, Any]:
        attempt = int(state.get("attempt") or 0) + 1
        async with sf() as s:
            run, email = await load(s, state["run_id"], for_update=True)
            await _guard(run)
            if email.body_text is None and email.body_purged_at is not None:
                raise AppError("The email body was purged by the retention policy; it cannot be analyzed.")
            ctx = email_context(email, settings)
            run.analysis_attempt = attempt
            run.started_at = run.started_at or utcnow()
            run.model_provider = deps.agent.provider_name
            run.model_name = deps.agent.model_name
            run.prompt_version = deps.agent.prompt_version
            run.schema_version = AI_SCHEMA_VERSION
            run.error_category = None
            run.error_message = None
            set_run_status(run, W.NORMALIZED, "prepare")
            email.analysis_status = EmailAnalysisStatus.ANALYZING
            email.content_changed_since_analysis = False
            record_workflow_event(
                s,
                tenant_id=run.tenant_id,
                workflow_run_id=run.id,
                step="prepare",
                event_type=WorkflowEventType.STEP_COMPLETED,
                message="Email loaded, normalized and screened as untrusted content.",
                details={
                    "attempt": attempt,
                    "body_chars": len(ctx.body_text),
                    "truncated": ctx.truncated,
                    "injection_rules": sorted({f.rule for f in ctx.injection_flags}),
                },
            )
            await s.commit()
        return {
            "attempt": attempt,
            "halt": False,
            "decision": None,
            "review_done": False,
            "classification": None,
            "extraction": None,
        }

    async def classify(state: WorkflowState) -> dict[str, Any]:
        async with sf() as s:
            run, email = await load(s, state["run_id"])
            await _guard(run)
            ctx = email_context(email, settings)
            taxonomy = (await get_tenant_settings(s, run.tenant_id, settings)).taxonomy
        result = await deps.agent.classify(ctx, taxonomy)
        async with sf() as s:
            run, _ = await load(s, state["run_id"], for_update=True)
            set_run_status(run, W.CLASSIFIED, "classify")
            _accumulate(run, result.usage)
            record_workflow_event(
                s,
                tenant_id=run.tenant_id,
                workflow_run_id=run.id,
                step="classify",
                event_type=WorkflowEventType.LLM_CALL,
                latency_ms=result.usage.latency_ms,
                message=f"Classified as {result.output.category.value} ({result.output.priority.value}).",
                details={
                    "usage": _usage_details(result.usage),
                    "category": result.output.category.value,
                    "priority": result.output.priority.value,
                    "urgency": result.output.urgency.value,
                    "prompt_version": deps.agent.prompt_version,
                    "schema_version": AI_SCHEMA_VERSION,
                },
            )
            await s.commit()
        return {"classification": result.output.model_dump(mode="json")}

    async def extract(state: WorkflowState) -> dict[str, Any]:
        classification = ClassificationOutput.model_validate(state["classification"])
        async with sf() as s:
            run, email = await load(s, state["run_id"])
            await _guard(run)
            ctx = email_context(email, settings)
        result = await deps.agent.extract(ctx, classification)
        async with sf() as s:
            run, _ = await load(s, state["run_id"], for_update=True)
            set_run_status(run, W.EXTRACTED, "extract")
            _accumulate(run, result.usage)
            record_workflow_event(
                s,
                tenant_id=run.tenant_id,
                workflow_run_id=run.id,
                step="extract",
                event_type=WorkflowEventType.LLM_CALL,
                latency_ms=result.usage.latency_ms,
                message="Structured information extracted.",
                details={
                    "usage": _usage_details(result.usage),
                    "evidence_count": len(result.output.evidence),
                    "proposed_field_count": len(result.output.proposed_fields),
                },
            )
            await s.commit()
        return {"extraction": result.output.model_dump(mode="json")}

    async def validate(state: WorkflowState) -> dict[str, Any]:
        classification = ClassificationOutput.model_validate(state["classification"])
        extraction = ExtractionOutput.model_validate(state["extraction"])
        async with sf() as s:
            run, email = await load(s, state["run_id"], for_update=True)
            await _guard(run)
            ts = await get_tenant_settings(s, run.tenant_id, settings)
            ctx = email_context(email, settings)
            validated = validate_analysis(
                ctx,
                classification,
                extraction,
                thresholds=ts.confidence,
                enabled_categories=ts.taxonomy.enabled_categories,
            )
            attempt = int(state.get("attempt") or 1)
            analysis = (
                await s.execute(
                    select(EmailAnalysis).where(
                        EmailAnalysis.workflow_run_id == run.id, EmailAnalysis.attempt == attempt
                    )
                )
            ).scalar_one_or_none()
            if analysis is None:
                analysis = EmailAnalysis(
                    tenant_id=run.tenant_id,
                    workflow_run_id=run.id,
                    email_message_id=email.id,
                    attempt=attempt,
                    created_at=utcnow(),
                )
                s.add(analysis)
            c = validated.classification
            analysis.classification = c.model_dump(mode="json")
            analysis.extraction = validated.extraction.model_dump(mode="json")
            analysis.category = c.category
            analysis.priority = c.priority
            analysis.urgency = c.urgency
            analysis.action_recommended = c.action_recommended
            analysis.needs_more_info = c.needs_more_info
            analysis.confidence_score = validated.confidence_score
            analysis.confidence_bucket = validated.confidence_bucket
            analysis.confidence_method = validated.confidence_method
            analysis.injection_flags = [f.model_dump(mode="json") for f in validated.injection_flags]
            analysis.validation_warnings = validated.warnings
            analysis.prompt_version = deps.agent.prompt_version
            analysis.schema_version = AI_SCHEMA_VERSION
            analysis.model_provider = deps.agent.provider_name
            analysis.model_name = deps.agent.model_name
            email.category = c.category
            email.priority = c.priority
            set_run_status(run, W.VALIDATED, "validate")
            record_workflow_event(
                s,
                tenant_id=run.tenant_id,
                workflow_run_id=run.id,
                step="validate",
                event_type=WorkflowEventType.STEP_COMPLETED,
                message=(
                    f"Validated; heuristic confidence {validated.confidence_score:.2f} "
                    f"({validated.confidence_bucket.value})."
                ),
                details={
                    "warnings": len(validated.warnings),
                    "injection_flags": len(validated.injection_flags),
                    "confidence_score": validated.confidence_score,
                    "confidence_bucket": validated.confidence_bucket.value,
                },
            )
            await s.flush()
            analysis_id = str(analysis.id)
            await s.commit()
        return {"analysis_id": analysis_id}

    async def plan(state: WorkflowState) -> dict[str, Any]:
        raw_extraction = ExtractionOutput.model_validate(state["extraction"])
        attempt = int(state.get("attempt") or 1)
        async with sf() as s:
            run, email = await load(s, state["run_id"])
            await _guard(run)
            ts = await get_tenant_settings(s, run.tenant_id, settings)
            ctx = email_context(email, settings)
            analysis = await s.get(EmailAnalysis, uuid.UUID(str(state["analysis_id"])))
            if analysis is None:
                raise NotFound("Analysis not found.")
            validated = ValidatedAnalysis(
                classification=ClassificationOutput.model_validate(analysis.classification),
                extraction=ValidatedExtraction.model_validate(analysis.extraction),
                warnings=list(analysis.validation_warnings),
                injection_flags=[InjectionFlag.model_validate(f) for f in analysis.injection_flags],
                confidence_score=analysis.confidence_score,
                confidence_bucket=analysis.confidence_bucket,
                confidence_method=analysis.confidence_method,
            )
            completed = (
                (
                    await s.execute(
                        select(ProposedAction).where(
                            ProposedAction.email_message_id == email.id, ProposedAction.status == ActionStatus.COMPLETED
                        )
                    )
                )
                .scalars()
                .all()
            )
            # Plan only from validated data: enabled category, grounded owner/deadline/references.
            classification = validated.classification
            ve = validated.extraction
            extraction = raw_extraction.model_copy(
                update={
                    "suggested_owner": ve.suggested_owner,
                    "organization": ve.organization,
                    "deadline": ve.deadline.date().isoformat() if ve.deadline else None,
                    "references": ve.reference_urls + ve.reference_ids,
                    "evidence": ve.evidence,
                    "proposed_fields": ve.proposed_fields,
                }
            )
            planning = PlanningContext(
                allowed_action_types=list(ACTION_TARGETS),
                allowed_work_item_types=ts.azure_devops.allowed_work_item_types,
                default_work_item_type=ts.azure_devops.work_item_type,
                category_work_item_type=ts.azure_devops.category_work_item_types.get(classification.category),
                azure_devops_available=True,
                already_completed_actions=[f"{a.action_type.value}: {a.title}" for a in completed],
            )
        result = await deps.agent.plan(ctx, classification, extraction, planning)

        async with sf() as s:
            run, email = await load(s, state["run_id"], for_update=True)
            await _guard(run)
            _accumulate(run, result.usage)
            record_workflow_event(
                s,
                tenant_id=run.tenant_id,
                workflow_run_id=run.id,
                step="plan",
                event_type=WorkflowEventType.LLM_CALL,
                latency_ms=result.usage.latency_ms,
                message=f"Planner proposed {len(result.tool_calls)} action(s).",
                details={"usage": _usage_details(result.usage)},
            )
            for call in result.tool_calls:
                record_workflow_event(
                    s,
                    tenant_id=run.tenant_id,
                    workflow_run_id=run.id,
                    step="plan",
                    event_type=WorkflowEventType.TOOL_CALL,
                    status="proposed",
                    message=f"Tool '{call.tool_name}' called (proposal only — nothing executed).",
                    details={"tool": call.tool_name, "arguments": _sanitized_tool_args(call.arguments)},
                )
            for rejected in result.rejected_calls:
                record_workflow_event(
                    s,
                    tenant_id=run.tenant_id,
                    workflow_run_id=run.id,
                    step="plan",
                    event_type=WorkflowEventType.TOOL_CALL,
                    status="rejected",
                    message="Tool call rejected by schema validation.",
                    details=dict(rejected),
                )

            specs = ActionPolicy(ts, settings).build_proposals(email, validated, result.tool_calls)
            thread_warnings = await _thread_duplicate_warnings(s, email)
            for spec in specs:
                if spec.action_type is ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM:
                    spec.warnings.extend(thread_warnings)
            injection_warning = (
                ["Prompt-injection indicators were detected in the source email. Review carefully."]
                if validated.injection_flags
                else []
            )
            action_ids: list[str] = []
            for spec in specs:
                key = idempotency_key(run.tenant_id, email.id, spec.action_type, spec.slot)
                live = (
                    await s.execute(
                        select(ProposedAction).where(
                            ProposedAction.idempotency_key == key,
                            ProposedAction.status.not_in(
                                [ActionStatus.REJECTED, ActionStatus.CANCELLED, ActionStatus.SUPERSEDED]
                            ),
                        )
                    )
                ).scalar_one_or_none()
                if live is not None:
                    if live.workflow_run_id == run.id and live.analysis_attempt == attempt:
                        action_ids.append(str(live.id))  # node re-run after a crash
                        continue
                    record_workflow_event(
                        s,
                        tenant_id=run.tenant_id,
                        workflow_run_id=run.id,
                        step="plan",
                        event_type=WorkflowEventType.STEP_COMPLETED,
                        status="skipped",
                        message=f"Duplicate prevented: an equivalent action already exists ({live.status.value}).",
                        details={
                            "existing_action_id": str(live.id),
                            "action_type": spec.action_type.value,
                            "external_id": live.external_id,
                        },
                    )
                    continue
                target = ACTION_TARGETS[spec.action_type]
                action = ProposedAction(
                    tenant_id=run.tenant_id,
                    owner_user_id=run.owner_user_id,
                    workflow_run_id=run.id,
                    email_message_id=email.id,
                    analysis_attempt=attempt,
                    action_type=spec.action_type,
                    target_integration=target,
                    status=ActionStatus.PENDING_APPROVAL,
                    version=1,
                    payload=spec.payload,
                    payload_hash=compute_payload_hash(spec.action_type.value, target.value, spec.payload),
                    title=spec.title,
                    rationale=spec.rationale,
                    expected_impact=spec.expected_impact,
                    required_permissions=REQUIRED_PERMISSIONS[spec.action_type],
                    idempotency_key=key,
                    validation_errors=spec.errors,
                    validation_warnings=injection_warning + spec.warnings,
                    source="agent",
                )
                s.add(action)
                await s.flush()
                action_ids.append(str(action.id))
            set_run_status(run, W.ACTION_PROPOSED, "plan")
            record_audit(
                s,
                tenant_id=run.tenant_id,
                actor_type=ActorType.AGENT,
                event_type="actions_proposed",
                resource_type="workflow_run",
                resource_id=run.id,
                workflow_run_id=run.id,
                details={"action_ids": action_ids, "attempt": attempt},
            )
            await s.commit()
        return {"action_ids": action_ids}

    # ------------------------------------------------------------------ review loop

    async def review_gate(state: WorkflowState) -> dict[str, Any]:
        async with sf() as s:
            run, email = await load(s, state["run_id"], for_update=True)
            if run.status is W.CANCELLED:
                return {"review_done": True, "decision": None}
            statuses = list(
                (await s.execute(select(ProposedAction.status).where(ProposedAction.workflow_run_id == run.id)))
                .scalars()
                .all()
            )
            target = derive_review_status(statuses)
            if target in _TERMINAL_REVIEW:
                return {"review_done": True, "decision": None}
            if run.status is not target:
                set_run_status(run, target, "review")
            now = utcnow()
            if target is W.PENDING_APPROVAL and run.awaiting_review_since is None:
                run.awaiting_review_since = now
            if (
                target is not W.PENDING_APPROVAL
                and run.awaiting_review_since is not None
                and "approval_wait_seconds" not in (run.metrics or {})
            ):
                run.metrics = {
                    **(run.metrics or {}),
                    "approval_wait_seconds": round((now - run.awaiting_review_since).total_seconds(), 1),
                }
            email.analysis_status = EmailAnalysisStatus.AWAITING_REVIEW
            await s.commit()
        return {"review_done": False, "decision": None}

    async def wait_for_review(state: WorkflowState) -> dict[str, Any]:
        # No side effects before interrupt(): this node re-executes from the top when resumed.
        decision = interrupt({"run_id": state["run_id"], "awaiting": "human_review"})
        async with sf() as s:
            run, _ = await load(s, state["run_id"])
            record_workflow_event(
                s,
                tenant_id=run.tenant_id,
                workflow_run_id=run.id,
                step="review",
                event_type=WorkflowEventType.RESUME,
                message=f"Resumed: {decision.get('type', 'unknown')}",
                details={k: v for k, v in decision.items() if k in ("type", "action_id", "version")},
            )
            await s.commit()
        return {"decision": decision}

    async def _run_action_operation(state: WorkflowState, op: str) -> dict[str, Any]:
        decision = state.get("decision") or {}
        run_id = state["run_id"]
        started = time.perf_counter()
        async with sf() as s:
            run, _ = await load(s, run_id, for_update=True)
            if op == "execute" and run.status in (W.APPROVED, W.FAILED, W.PENDING_APPROVAL):
                set_run_status(run, W.EXECUTING, "execute")
                await s.commit()
        async with sf() as s:
            status, message = "failure", ""
            details: dict[str, Any] = {"action_id": decision.get("action_id"), "operation": op}
            try:
                actor = await load_actor(s, uuid.UUID(str(decision.get("actor_user_id"))))
                action_id = uuid.UUID(str(decision.get("action_id")))
                if op == "execute":
                    execution = await deps.execution.execute(
                        s,
                        actor,
                        action_id,
                        expected_version=decision.get("version"),
                        expected_payload_hash=decision.get("payload_hash"),
                    )
                else:
                    execution = await deps.execution.reconcile(s, actor, action_id)
                status = execution.status.value
                message = f"{op.capitalize()} finished: {execution.status.value}."
                details.update(
                    execution_id=str(execution.id),
                    external_id=execution.external_id,
                    deduplicated=execution.deduplicated,
                    retry_count=execution.retry_count,
                    error_category=execution.error_category.value if execution.error_category else None,
                )
            except AuditedPermissionDenied as exc:
                await s.rollback()
                await record_permission_denied(sf, exc)
                message = f"{op.capitalize()} denied: {exc.message}"
            except AppError as exc:
                await s.rollback()
                message = f"{op.capitalize()} not performed: {exc.message}"
                details["error_code"] = exc.code
        async with sf() as s:
            run, _ = await load(s, run_id)
            record_workflow_event(
                s,
                tenant_id=run.tenant_id,
                workflow_run_id=run.id,
                step=op,
                event_type=WorkflowEventType.INTEGRATION_CALL,
                status=status,
                message=message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                details=details,
            )
            await s.commit()
        return {"decision": None}

    async def execute_action(state: WorkflowState) -> dict[str, Any]:
        return await _run_action_operation(state, "execute")

    async def reconcile_action(state: WorkflowState) -> dict[str, Any]:
        return await _run_action_operation(state, "reconcile")

    async def reanalyze(state: WorkflowState) -> dict[str, Any]:
        async with sf() as s:
            run, _ = await load(s, state["run_id"], for_update=True)
            busy = (
                await s.execute(
                    select(ProposedAction.id).where(
                        ProposedAction.workflow_run_id == run.id,
                        ProposedAction.status.in_([ActionStatus.EXECUTING, ActionStatus.UNCERTAIN]),
                    )
                )
            ).first()
            if busy is not None or run.status is W.CANCELLED:
                record_workflow_event(
                    s,
                    tenant_id=run.tenant_id,
                    workflow_run_id=run.id,
                    step="reanalyze",
                    event_type=WorkflowEventType.STEP_FAILED,
                    status="skipped",
                    message="Re-analysis skipped: an action is executing or uncertain, or the run was cancelled.",
                )
                await s.commit()
                return {"decision": None, "halt": True}
            superseded = await supersede_open_actions(s, run, "re-analysis requested")
            record_workflow_event(
                s,
                tenant_id=run.tenant_id,
                workflow_run_id=run.id,
                step="reanalyze",
                event_type=WorkflowEventType.STEP_STARTED,
                message="Re-analysis started.",
                details={"superseded_action_ids": [str(a) for a in superseded]},
            )
            await s.commit()
        return {"decision": None, "halt": False}

    async def finalize(state: WorkflowState) -> dict[str, Any]:
        async with sf() as s:
            run, email = await load(s, state["run_id"], for_update=True)
            statuses = list(
                (await s.execute(select(ProposedAction.status).where(ProposedAction.workflow_run_id == run.id)))
                .scalars()
                .all()
            )
            if run.status is not W.CANCELLED:
                target = derive_review_status(statuses)
                if target not in _TERMINAL_REVIEW:  # analysis halted mid-way without a cancel
                    target = W.CANCELLED
                set_run_status(run, target, "finalize")
            run.completed_at = run.completed_at or utcnow()
            run.current_step = "finalize"
            email.analysis_status = EmailAnalysisStatus.DONE
            record_workflow_event(
                s,
                tenant_id=run.tenant_id,
                workflow_run_id=run.id,
                step="finalize",
                event_type=WorkflowEventType.STATUS_CHANGED,
                message=f"Workflow {run.status.value}.",
            )
            record_audit(
                s,
                tenant_id=run.tenant_id,
                actor_type=ActorType.SYSTEM,
                event_type="workflow_finished",
                resource_type="workflow_run",
                resource_id=run.id,
                workflow_run_id=run.id,
                outcome=AuditOutcome.SUCCESS,
                details={"status": run.status.value},
            )
            await s.commit()
        return {"review_done": True}

    # ------------------------------------------------------------------ wiring

    def after_analysis(next_node: str) -> Callable[[WorkflowState], str]:
        def route(state: WorkflowState) -> str:
            return "finalize" if state.get("halt") else next_node

        return route

    def after_gate(state: WorkflowState) -> str:
        return "finalize" if state.get("review_done") else "wait_for_review"

    def after_wait(state: WorkflowState) -> str:
        decision_type = (state.get("decision") or {}).get("type", "refresh")
        return {
            "execute": "execute_action",
            "reconcile": "reconcile_action",
            "reanalyze": "reanalyze",
            "cancel": "finalize",
        }.get(decision_type, "review_gate")

    def after_reanalyze(state: WorkflowState) -> str:
        return "review_gate" if state.get("halt") else "prepare"

    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
    graph.add_node("prepare", analysis_step("prepare", prepare))
    graph.add_node("classify", analysis_step("classify", classify))
    graph.add_node("extract", analysis_step("extract", extract))
    graph.add_node("validate", analysis_step("validate", validate))
    graph.add_node("plan", analysis_step("plan", plan))
    graph.add_node("review_gate", review_gate)
    graph.add_node("wait_for_review", wait_for_review)
    graph.add_node("execute_action", execute_action)
    graph.add_node("reconcile_action", reconcile_action)
    graph.add_node("reanalyze", reanalyze)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "prepare")
    for current, nxt in (
        ("prepare", "classify"),
        ("classify", "extract"),
        ("extract", "validate"),
        ("validate", "plan"),
        ("plan", "review_gate"),
    ):
        graph.add_conditional_edges(current, after_analysis(nxt), [nxt, "finalize"])
    graph.add_conditional_edges("review_gate", after_gate, ["wait_for_review", "finalize"])
    graph.add_conditional_edges(
        "wait_for_review",
        after_wait,
        ["execute_action", "reconcile_action", "reanalyze", "finalize", "review_gate"],
    )
    graph.add_edge("execute_action", "review_gate")
    graph.add_edge("reconcile_action", "review_gate")
    graph.add_conditional_edges("reanalyze", after_reanalyze, ["prepare", "review_gate"])
    graph.add_edge("finalize", END)
    return graph
