"""Contracts for the email analysis agent (LLM providers and the deterministic heuristic agent)."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from app.core.errors import AppError
from app.domain.enums import FailureCategory
from app.schemas.ai import ClassificationOutput, ExtractionOutput, PlannedToolCall, PlanningContext
from app.schemas.email import EmailContext
from app.schemas.settings import TaxonomyConfig


class LLMUsage(BaseModel):
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int = 0
    estimated_cost_usd: float | None = None
    attempts: int = 1


class ClassificationResult(BaseModel):
    output: ClassificationOutput
    usage: LLMUsage


class ExtractionResult(BaseModel):
    output: ExtractionOutput
    usage: LLMUsage


class PlanningResult(BaseModel):
    tool_calls: list[PlannedToolCall]
    # Tool calls the model attempted that were rejected (unknown tool, invalid args) — sanitized.
    rejected_calls: list[dict[str, str]]
    usage: LLMUsage


class AgentError(AppError):
    """A model call failed. `category` becomes the run's `error_category` (e.g. rejected credentials are an
    authorization problem, an unknown model or deployment a configuration problem, not bad model output)."""

    status_code = 502
    code = "agent_error"
    public_message = "The analysis model failed."
    retryable = False
    category: FailureCategory = FailureCategory.LLM_PROVIDER

    def __init__(
        self,
        message: str | None = None,
        *,
        category: FailureCategory | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        if category is not None:
            self.category = category


class AgentProviderError(AgentError):
    """Network/rate-limit/5xx or an unprocessable response from the model provider (retryable at the job level)."""

    code = "agent_provider_error"
    retryable = True
    category = FailureCategory.LLM_PROVIDER


class AgentOutputError(AgentError):
    """Refusal, truncated or schema-invalid output (not retried automatically)."""

    code = "agent_output_error"
    category = FailureCategory.LLM_OUTPUT


class EmailAnalysisAgent(Protocol):
    provider_name: str
    model_name: str
    prompt_version: str

    async def classify(self, email: EmailContext, taxonomy: TaxonomyConfig) -> ClassificationResult: ...

    async def extract(self, email: EmailContext, classification: ClassificationOutput) -> ExtractionResult: ...

    async def plan(
        self,
        email: EmailContext,
        classification: ClassificationOutput,
        extraction: ExtractionOutput,
        context: PlanningContext,
    ) -> PlanningResult: ...
