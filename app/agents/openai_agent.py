"""OpenAI / Azure OpenAI `EmailAnalysisAgent`: strict structured outputs and strict proposal tools.

Errors are mapped to `AgentProviderError` (transient: connection, timeout, rate limit, 5xx),
`AgentOutputError` (refusal, truncation, content filter, schema-invalid output) or `AgentError`
(authentication, bad request, configuration). Error messages never contain email content.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

import openai
from openai import AsyncAzureOpenAI, AsyncOpenAI
from openai.types.chat import ChatCompletionFunctionToolParam, ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from app.agents import prompts
from app.agents.base import (
    AgentError,
    AgentOutputError,
    AgentProviderError,
    ClassificationResult,
    ExtractionResult,
    LLMUsage,
    PlanningResult,
)
from app.agents.tools import collect_tool_calls, completed_action_types, offered_tools
from app.core.config import LLMProvider, Settings
from app.core.errors import ConfigurationError
from app.observability.logging import get_logger
from app.observability.tracing import start_span
from app.schemas.ai import ClassificationOutput, ExtractionOutput, PlannedToolCall, PlanningContext
from app.schemas.email import EmailContext
from app.schemas.settings import TaxonomyConfig

log = get_logger(__name__)

AZURE_COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"
# Output budget per step (includes reasoning tokens on reasoning models).
MAX_COMPLETION_TOKENS: dict[str, int] = {"classify": 4096, "extract": 8192, "plan": 8192}
_SEMCONV_PROVIDER = {LLMProvider.OPENAI: "openai", LLMProvider.AZURE_OPENAI: "azure.ai.openai"}
_SAFE_CODE_RE = re.compile(r"[^a-z0-9_]")


def _safe_code(value: object) -> str:
    return _SAFE_CODE_RE.sub("", str(value or "").lower())[:40]


def map_provider_error(exc: Exception) -> AgentError | None:
    """Translate SDK/parsing exceptions into agent errors. Returns None for unrelated exceptions."""
    if isinstance(exc, AgentError):
        return exc
    if isinstance(exc, openai.LengthFinishReasonError):
        return AgentOutputError("The model output was truncated (length limit reached).")
    if isinstance(exc, openai.ContentFilterFinishReasonError):
        return AgentOutputError("The model output was blocked by the provider content filter.")
    if isinstance(exc, ValidationError | json.JSONDecodeError):
        return AgentOutputError("The model output did not match the expected schema.")
    if isinstance(exc, openai.APITimeoutError):
        return AgentProviderError("The model provider timed out.")
    if isinstance(exc, openai.APIConnectionError):
        return AgentProviderError("Could not connect to the model provider.")
    if isinstance(exc, openai.RateLimitError):
        return AgentProviderError("The model provider rate limit was exceeded.")
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if status >= 500 or status == 408:
            return AgentProviderError(f"The model provider returned HTTP {status}.")
        code = _safe_code(exc.code)
        if isinstance(exc, openai.BadRequestError) and code in {"content_filter", "content_policy_violation"}:
            return AgentOutputError("The provider content filter blocked the request.")
        if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
            return AgentError(f"The model provider rejected the credentials (HTTP {status}).")
        return AgentError(f"The model provider rejected the request (HTTP {status}{', ' + code if code else ''}).")
    if isinstance(exc, openai.OpenAIError):
        return AgentError(f"The model provider call failed ({type(exc).__name__}).")
    return None


def _strict_tool(name: str, model: type[BaseModel]) -> ChatCompletionFunctionToolParam:
    # Built with pydantic_function_tool (strict schema) but sent as a plain strict definition, so arguments
    # are validated per call by `collect_tool_calls`; one invalid call then cannot fail the whole response.
    function = openai.pydantic_function_tool(model, name=name)["function"]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": function.get("description") or "",
            "parameters": function.get("parameters", {}),
            "strict": True,
        },
    }


def _first_message(completion: Any) -> Any:
    choices = getattr(completion, "choices", None) or []
    if not choices:
        raise AgentOutputError("The model returned no choices.")
    choice = choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        raise AgentOutputError("The model output was truncated (length limit reached).")
    if finish_reason == "content_filter":
        raise AgentOutputError("The model output was blocked by the provider content filter.")
    message = getattr(choice, "message", None)
    if message is None:
        raise AgentOutputError("The model returned no message.")
    if getattr(message, "refusal", None):
        raise AgentOutputError("The model refused to analyze this email.")
    return message


def _parsed[M: BaseModel](message: Any, model: type[M]) -> M:
    parsed = getattr(message, "parsed", None)
    if isinstance(parsed, model):
        return parsed
    if parsed is None:
        raise AgentOutputError("The model returned no structured output.")
    try:
        return model.model_validate(parsed if not isinstance(parsed, BaseModel) else parsed.model_dump())
    except ValidationError as exc:
        raise AgentOutputError("The model output did not match the expected schema.") from exc


def _raw_tool_calls(message: Any) -> list[tuple[str, object]]:
    calls: list[tuple[str, object]] = []
    for call in getattr(message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        if getattr(call, "type", "function") != "function" or function is None:
            calls.append(("non_function_tool", None))
            continue
        arguments = getattr(function, "arguments", None)
        if arguments is None:
            arguments = getattr(function, "parsed_arguments", None)
        calls.append((str(getattr(function, "name", "")), arguments))
    return calls


class OpenAIAnalysisAgent:
    """`EmailAnalysisAgent` for OpenAI and Azure OpenAI. Pass `client` to inject a fake in tests."""

    prompt_version: str = prompts.PROMPT_VERSION

    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        if settings.llm_provider not in _SEMCONV_PROVIDER:
            raise ConfigurationError("OpenAIAnalysisAgent requires LLM_PROVIDER=openai or azure_openai.")
        self._settings = settings
        self.provider_name: str = settings.llm_provider.value
        self.model_name: str = settings.llm_model_label
        self._semconv_provider = _SEMCONV_PROVIDER[settings.llm_provider]
        self._credential: Any = None
        self._client: AsyncOpenAI = client if client is not None else self._build_client(settings)

    def _build_client(self, settings: Settings) -> AsyncOpenAI:
        if settings.llm_provider is LLMProvider.OPENAI:
            if settings.openai_api_key is None:
                raise ConfigurationError("OPENAI_API_KEY is not configured.")
            return AsyncOpenAI(
                api_key=settings.openai_api_key.get_secret_value(),
                base_url=settings.openai_base_url,
                max_retries=settings.llm_max_retries,
                timeout=settings.llm_timeout_seconds,
            )
        if not (settings.azure_openai_endpoint and settings.azure_openai_api_version):
            raise ConfigurationError("Azure OpenAI endpoint and API version are not configured.")
        if settings.azure_openai_use_entra_id:
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

            self._credential = DefaultAzureCredential()
            return AsyncAzureOpenAI(
                azure_endpoint=settings.azure_openai_endpoint,
                api_version=settings.azure_openai_api_version,
                azure_ad_token_provider=get_bearer_token_provider(self._credential, AZURE_COGNITIVE_SERVICES_SCOPE),
                max_retries=settings.llm_max_retries,
                timeout=settings.llm_timeout_seconds,
            )
        if settings.azure_openai_api_key is None:
            raise ConfigurationError("AZURE_OPENAI_API_KEY is not configured.")
        return AsyncAzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_version=settings.azure_openai_api_version,
            api_key=settings.azure_openai_api_key.get_secret_value(),
            max_retries=settings.llm_max_retries,
            timeout=settings.llm_timeout_seconds,
        )

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            await close()
        if self._credential is not None:
            await self._credential.close()

    # -- protocol ---------------------------------------------------------------------------------

    async def classify(self, email: EmailContext, taxonomy: TaxonomyConfig) -> ClassificationResult:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": prompts.classify_system_prompt(taxonomy)},
            {"role": "user", "content": prompts.classify_user_message(email)},
        ]
        output, usage = await self._complete(
            "classify",
            email,
            messages,
            lambda message: _parsed(message, ClassificationOutput),
            response_format=ClassificationOutput,
        )
        return ClassificationResult(output=output, usage=usage)

    async def extract(self, email: EmailContext, classification: ClassificationOutput) -> ExtractionResult:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": prompts.extract_system_prompt()},
            {"role": "user", "content": prompts.extract_user_message(email, classification)},
        ]
        output, usage = await self._complete(
            "extract",
            email,
            messages,
            lambda message: _parsed(message, ExtractionOutput),
            response_format=ExtractionOutput,
        )
        return ExtractionResult(output=output, usage=usage)

    async def plan(
        self,
        email: EmailContext,
        classification: ClassificationOutput,
        extraction: ExtractionOutput,
        context: PlanningContext,
    ) -> PlanningResult:
        offered = offered_tools(context)
        if not offered:
            return PlanningResult(
                tool_calls=[], rejected_calls=[], usage=LLMUsage(provider=self.provider_name, model=self.model_name)
            )
        completed = completed_action_types(context.already_completed_actions)
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": prompts.plan_system_prompt(context)},
            {"role": "user", "content": prompts.plan_user_message(email, classification, extraction, context)},
        ]

        def interpret(message: Any) -> tuple[list[PlannedToolCall], list[dict[str, str]]]:
            calls = _raw_tool_calls(message)
            if not calls:
                raise AgentOutputError("The model did not call any proposal tool.")
            return collect_tool_calls(calls, offered, completed=completed)

        (accepted, rejected), usage = await self._complete(
            "plan",
            email,
            messages,
            interpret,
            tools=[_strict_tool(name, model) for name, (_, model) in offered.items()],
            tool_choice="required",
        )
        if rejected:
            log.warning(
                "llm_tool_calls_rejected",
                step="plan",
                rejected=len(rejected),
                accepted=len(accepted),
                email_id=str(email.email_id),
            )
        return PlanningResult(tool_calls=accepted, rejected_calls=rejected, usage=usage)

    # -- internals --------------------------------------------------------------------------------

    def _usage(self, completion: Any, latency_ms: int) -> LLMUsage:
        usage = getattr(completion, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None)
        output_tokens = getattr(usage, "completion_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        cost = None
        in_price = self._settings.llm_input_cost_per_1m_tokens
        out_price = self._settings.llm_output_cost_per_1m_tokens
        if in_price is not None and out_price is not None and input_tokens is not None and output_tokens is not None:
            cost = round((input_tokens * in_price + output_tokens * out_price) / 1_000_000, 6)
        return LLMUsage(
            provider=self.provider_name,
            model=self.model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            estimated_cost_usd=cost,
        )

    async def _complete[T](
        self,
        step: str,
        email: EmailContext,
        messages: list[ChatCompletionMessageParam],
        interpret: Callable[[Any], T],
        **options: Any,
    ) -> tuple[T, LLMUsage]:
        max_tokens = self._settings.llm_max_completion_tokens or MAX_COMPLETION_TOKENS[step]
        request: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "timeout": self._settings.llm_timeout_seconds,
            **options,
        }
        if self._settings.llm_temperature is not None:
            request["temperature"] = self._settings.llm_temperature
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": self._semconv_provider,
            "gen_ai.request.model": self.model_name,
            "gen_ai.request.max_tokens": max_tokens,
            "nuvexa.agent.step": step,
            "nuvexa.prompt_version": prompts.PROMPT_VERSION,
            "nuvexa.email_id": str(email.email_id),
        }
        started = time.perf_counter()
        usage: LLMUsage | None = None
        with start_span("gen_ai.chat", attributes) as span:
            try:
                completion = await self._client.chat.completions.parse(**request)
                usage = self._usage(completion, int((time.perf_counter() - started) * 1000))
                span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens or 0)
                span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens or 0)
                response_model = getattr(completion, "model", None)
                if isinstance(response_model, str):
                    span.set_attribute("gen_ai.response.model", response_model)
                reasons = [str(getattr(c, "finish_reason", "")) for c in getattr(completion, "choices", None) or []]
                span.set_attribute("gen_ai.response.finish_reasons", reasons)
                result = interpret(_first_message(completion))
            except Exception as exc:
                error = map_provider_error(exc)
                span.set_attribute("error.type", type(exc).__name__)
                log.warning(
                    "llm_call_failed",
                    step=step,
                    provider=self.provider_name,
                    model=self.model_name,
                    error_type=type(exc).__name__,
                    error_code=error.code if error else "unexpected",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    input_tokens=usage.input_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                    email_id=str(email.email_id),
                )
                if error is None or error is exc:
                    raise
                raise error from exc
        log.info(
            "llm_call_completed",
            step=step,
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=usage.latency_ms,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost_usd=usage.estimated_cost_usd,
            email_id=str(email.email_id),
        )
        return result, usage
