"""Runs and resumes the compiled LangGraph workflow (worker side)."""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.db.models import ProposedAction, WorkflowRun
from app.domain.enums import WorkflowEventType, WorkflowStatus
from app.domain.state_machine import derive_review_status
from app.observability.logging import get_logger
from app.services.audit import record_permission_denied, record_workflow_event
from app.services.authz import AuditedPermissionDenied
from app.services.identity import load_actor
from app.workflows.graph import WorkflowDeps, build_graph, set_run_status
from app.workflows.state import ReviewDecision, WorkflowState

log = get_logger(__name__)


class WorkflowEngine:
    def __init__(self, deps: WorkflowDeps, checkpointer: BaseCheckpointSaver[str]) -> None:
        self.deps = deps
        self.graph = build_graph(deps).compile(checkpointer=checkpointer)

    @staticmethod
    def _config(run_id: uuid.UUID) -> RunnableConfig:
        return {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 100}

    async def start(self, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        config = self._config(run_id)
        snapshot = await self.graph.aget_state(config)
        if snapshot.interrupts:
            return  # already waiting for review (job re-delivered)
        if snapshot.next:
            await self.graph.ainvoke(None, config, durability="sync")  # retry failed step
            return
        if snapshot.values:
            return  # already finished
        initial: WorkflowState = {"run_id": str(run_id), "tenant_id": str(tenant_id), "attempt": 0}
        await self.graph.ainvoke(initial, config, durability="sync")

    async def resume(self, run_id: uuid.UUID, tenant_id: uuid.UUID, decision: ReviewDecision) -> None:
        config = self._config(run_id)
        snapshot = await self.graph.aget_state(config)
        if snapshot.interrupts:
            resume: Command[Any] = Command(resume=dict(decision))
            await self.graph.ainvoke(resume, config, durability="sync")
            return
        if snapshot.next and decision.get("type") in ("reanalyze", "refresh"):
            # A previous analysis step failed; re-run it from the checkpoint.
            await self.graph.ainvoke(None, config, durability="sync")
            return
        if not snapshot.values and decision.get("type") == "reanalyze":
            await self.start(run_id, tenant_id)
            return
        # The graph is not waiting (finished, or checkpoint unavailable). Execution safety never depends on
        # the graph, so fall back to the services directly and resynchronize the run status.
        await self._fallback(run_id, decision)

    async def _fallback(self, run_id: uuid.UUID, decision: ReviewDecision) -> None:
        sf: async_sessionmaker[AsyncSession] = self.deps.session_factory
        decision_type = decision.get("type")
        if decision_type in ("execute", "reconcile"):
            async with sf() as s:
                try:
                    actor = await load_actor(s, uuid.UUID(str(decision.get("actor_user_id"))))
                    action_id = uuid.UUID(str(decision.get("action_id")))
                    if decision_type == "execute":
                        await self.deps.execution.execute(
                            s,
                            actor,
                            action_id,
                            expected_version=decision.get("version"),
                            expected_payload_hash=decision.get("payload_hash"),
                        )
                    else:
                        await self.deps.execution.reconcile(s, actor, action_id)
                except AuditedPermissionDenied as exc:
                    await s.rollback()
                    await record_permission_denied(sf, exc)
                except AppError as exc:
                    await s.rollback()
                    log.warning("workflow_fallback_operation_failed", code=exc.code, run_id=str(run_id))
        await self.sync_run_status(run_id, note=f"graph not waiting; handled '{decision_type}' directly")

    async def sync_run_status(self, run_id: uuid.UUID, *, note: str) -> None:
        async with self.deps.session_factory() as s:
            run = (
                await s.execute(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update())
            ).scalar_one_or_none()
            if run is None or run.status in (WorkflowStatus.CANCELLED,):
                return
            statuses = list(
                (await s.execute(select(ProposedAction.status).where(ProposedAction.workflow_run_id == run_id)))
                .scalars()
                .all()
            )
            if not statuses:
                return
            target = derive_review_status(statuses)
            if target is not run.status:
                try:
                    set_run_status(run, target, "review")
                except AppError:
                    return
            record_workflow_event(
                s,
                tenant_id=run.tenant_id,
                workflow_run_id=run.id,
                step="review",
                event_type=WorkflowEventType.STATUS_CHANGED,
                message=note,
            )
            await s.commit()
