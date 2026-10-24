"""OpenAIAnalysisAgent against a fake `client.chat.completions.parse` (no network, no real keys)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from pydantic import ValidationError

from app.agents import openai_agent
from app.agents.base import AgentError, AgentOutputError, AgentProviderError, EmailAnalysisAgent
from app.agents.factory import build_analysis_agent
from app.agents.heuristic import HeuristicAnalysisAgent
from app.agents.openai_agent import OpenAIAnalysisAgent
from app.core.config import LLMProvider
from app.domain.enums import ActionType, EmailCategory, Priority, Urgency
from app.schemas.ai import ClassificationOutput, ExtractionOutput
from app.schemas.settings import TaxonomyConfig
from tests.conftest import make_settings
from tests.unit.agents.conftest import SPEC_BODY

DB = "sqlite+aiosqlite:///:memory:"
REQUEST = httpx2.Request("POST", "https://api.example.invalid/v1/chat/completions")

CLASSIFICATION = ClassificationOutput(
    category=EmailCategory.INCIDENT,
    rationale="Production users cannot log in.",
    urgency=Urgency.IMMEDIATE,
    priority=Priority.HIGH,
    action_recommended=True,
    needs_more_info=False,
    missing_information=[],
    extraction_warnings=[],
    confidence=0.9,
)
EXTRACTION = ExtractionOutput(
    summary="Investigate production login failures after deployment",
    description="Production users are unable to log in.",
    requester_name="Dana Reyes",
    requester_email="dana.reyes@example.com",
    organization=None,
    product_or_system="customer portal",
    environment="production",
    impact="The issue affects our customer portal.",
    deadline=None,
    suggested_owner=None,
    suggested_team=None,
    references=[],
    suggested_work_item_type="Bug",
    proposed_fields=[],
    evidence=[],
)
WORK_ITEM_ARGS = {
    "title": "Investigate production login failures",
    "description": "Users cannot log in.",
    "work_item_type": "Bug",
    "priority": "high",
    "tags": ["nuvexa"],
    "rationale": "Production incident.",
    "expected_impact": "Tracked fix.",
}


def completion(
    *,
    parsed: Any = None,
    tool_calls: list[Any] | None = None,
    refusal: str | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 1000,
    completion_tokens: int = 500,
) -> Any:
    message = SimpleNamespace(parsed=parsed, tool_calls=tool_calls, refusal=refusal, content=None)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=prompt_tokens + completion_tokens
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=message)], usage=usage, model="gpt-test-2026"
    )


def tool_call(name: str, arguments: Any) -> Any:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(id="call_1", type="function", function=SimpleNamespace(name=name, arguments=raw))


class FakeCompletions:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClient:
    def __init__(self, *responses: Any) -> None:
        self.completions = FakeCompletions(list(responses))
        self.chat = SimpleNamespace(completions=self.completions)


def make_agent(*responses: Any, **settings_overrides: Any) -> tuple[OpenAIAnalysisAgent, FakeCompletions]:
    values: dict[str, Any] = {
        "llm_provider": LLMProvider.OPENAI,
        "openai_api_key": "sk-test-not-a-real-key",
        "llm_model": "gpt-test",
    }
    values.update(settings_overrides)
    client = FakeClient(*responses)
    agent = OpenAIAnalysisAgent(make_settings(DB, **values), client=client)  # type: ignore[arg-type]
    return agent, client.completions


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    class FakeSpan:
        def __init__(self, attributes: dict[str, Any]) -> None:
            self.attributes = attributes

        def set_attribute(self, key: str, value: Any) -> None:
            self.attributes[key] = value

    @contextmanager
    def fake_start_span(name: str, attributes: dict[str, Any] | None = None):
        attrs = {"name": name, **(attributes or {})}
        recorded.append(attrs)
        yield FakeSpan(attrs)

    monkeypatch.setattr(openai_agent, "start_span", fake_start_span)
    return recorded


async def test_classify_uses_structured_output(make_ctx, spans) -> None:
    agent, fake = make_agent(completion(parsed=CLASSIFICATION))
    ctx = make_ctx(SPEC_BODY, subject="Production login failure")
    result = await agent.classify(ctx, TaxonomyConfig())

    assert result.output == CLASSIFICATION
    request = fake.calls[0]
    assert request["model"] == "gpt-test"
    assert request["response_format"] is ClassificationOutput
    assert request["max_completion_tokens"] > 0
    assert request["timeout"] == 60.0
    assert "temperature" not in request
    system, user = request["messages"]
    assert system["role"] == "system"
    assert user["role"] == "user"
    assert "Production users are unable" not in system["content"]
    assert "Production users are unable" in user["content"]
    assert "<<<EMAIL_DATA_" in user["content"]

    assert result.usage.input_tokens == 1000
    assert result.usage.output_tokens == 500
    assert result.usage.total_tokens == 1500
    assert result.usage.estimated_cost_usd is None  # prices not configured
    span = spans[0]
    assert span["name"] == "gen_ai.chat"
    assert span["gen_ai.operation.name"] == "chat"
    assert span["gen_ai.provider.name"] == "openai"
    assert span["gen_ai.request.model"] == "gpt-test"
    assert span["gen_ai.usage.input_tokens"] == 1000
    assert span["gen_ai.usage.output_tokens"] == 500
    assert span["nuvexa.prompt_version"] == agent.prompt_version
    assert not any("Production users" in str(v) for v in span.values())


async def test_cost_and_temperature_from_settings(make_ctx) -> None:
    agent, fake = make_agent(
        completion(parsed=EXTRACTION, prompt_tokens=1000, completion_tokens=500),
        llm_input_cost_per_1m_tokens=2.5,
        llm_output_cost_per_1m_tokens=10.0,
        llm_temperature=0.0,
    )
    result = await agent.extract(make_ctx(SPEC_BODY), CLASSIFICATION)
    assert result.output == EXTRACTION
    assert result.usage.estimated_cost_usd == pytest.approx(0.0075)
    assert fake.calls[0]["temperature"] == 0.0
    assert fake.calls[0]["response_format"] is ExtractionOutput


@pytest.mark.parametrize(
    "response",
    [
        completion(parsed=None, refusal="I can't help with that."),
        completion(parsed=CLASSIFICATION, finish_reason="length"),
        completion(parsed=CLASSIFICATION, finish_reason="content_filter"),
        completion(parsed=None),
        SimpleNamespace(choices=[], usage=None, model="x"),
    ],
)
async def test_unusable_output_raises_output_error(make_ctx, response) -> None:
    agent, _ = make_agent(response)
    with pytest.raises(AgentOutputError) as exc:
        await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())
    assert exc.value.retryable is False
    assert "Production users" not in str(exc.value)


def _status_error(cls: type[openai.APIStatusError], status: int, code: str | None = None) -> openai.APIStatusError:
    body = {"code": code, "message": "provider message"} if code else None
    return cls("provider error", response=httpx2.Response(status, request=REQUEST), body=body)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (openai.APITimeoutError(request=REQUEST), AgentProviderError),
        (openai.APIConnectionError(request=REQUEST), AgentProviderError),
        (_status_error(openai.RateLimitError, 429), AgentProviderError),
        (_status_error(openai.InternalServerError, 503), AgentProviderError),
        (_status_error(openai.AuthenticationError, 401), AgentError),
        (_status_error(openai.PermissionDeniedError, 403), AgentError),
        (_status_error(openai.BadRequestError, 400), AgentError),
        (_status_error(openai.BadRequestError, 400, code="content_filter"), AgentOutputError),
        (openai.LengthFinishReasonError(completion=completion()), AgentOutputError),  # type: ignore[arg-type]
        (openai.ContentFilterFinishReasonError(), AgentOutputError),
    ],
)
async def test_provider_errors_are_mapped(make_ctx, error, expected) -> None:
    agent, _ = make_agent(error)
    with pytest.raises(AgentError) as exc:
        await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())
    assert type(exc.value) is expected
    assert exc.value.retryable is (expected is AgentProviderError)
    assert "Production users" not in exc.value.message


async def test_schema_invalid_content_maps_to_output_error(make_ctx) -> None:
    try:
        ClassificationOutput.model_validate_json("{}")
    except ValidationError as validation_error:
        error = validation_error
    agent, _ = make_agent(error)
    with pytest.raises(AgentOutputError):
        await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())


async def test_plan_offers_only_allowed_tools_and_validates_calls(make_ctx, planning_context, spans) -> None:
    calls = [
        tool_call("propose_azure_devops_work_item", WORK_ITEM_ARGS),
        tool_call("delete_everything", {"confirm": True}),
        tool_call("propose_email_reply", {"subject": "Re: hi", "body": "SECRET-BODY-TEXT"}),  # missing fields
        tool_call("propose_azure_devops_work_item", WORK_ITEM_ARGS),  # duplicate
        tool_call("propose_follow_up_task", "{not json"),  # not offered
    ]
    agent, fake = make_agent(completion(tool_calls=calls))
    context = planning_context(
        allowed_action_types=[ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM, ActionType.PROPOSE_EMAIL_REPLY]
    )
    result = await agent.plan(make_ctx(SPEC_BODY), CLASSIFICATION, EXTRACTION, context)

    request = fake.calls[0]
    offered = [t["function"]["name"] for t in request["tools"]]
    assert offered == ["propose_azure_devops_work_item", "propose_email_reply"]
    assert all(t["function"]["strict"] is True for t in request["tools"])
    assert request["tool_choice"] == "required"
    assert "response_format" not in request

    assert [c.tool_name for c in result.tool_calls] == ["propose_azure_devops_work_item"]
    assert result.tool_calls[0].action_type is ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM
    assert result.tool_calls[0].arguments["priority"] == "high"
    reasons = {(r["tool_name"], r["reason"].split(" (")[0]) for r in result.rejected_calls}
    assert reasons == {
        ("delete_everything", "unknown or unavailable tool"),
        ("propose_email_reply", "invalid arguments"),
        ("propose_azure_devops_work_item", "duplicate proposal"),
        ("propose_follow_up_task", "unknown or unavailable tool"),
    }
    assert not any("SECRET-BODY-TEXT" in r["reason"] for r in result.rejected_calls)
    assert spans[0]["nuvexa.agent.step"] == "plan"


async def test_plan_without_tool_calls_is_an_output_error(make_ctx, planning_context) -> None:
    agent, _ = make_agent(completion(tool_calls=None))
    with pytest.raises(AgentOutputError):
        await agent.plan(make_ctx(SPEC_BODY), CLASSIFICATION, EXTRACTION, planning_context())


async def test_plan_skips_the_call_when_no_tool_is_allowed(make_ctx, planning_context) -> None:
    agent, fake = make_agent()
    context = planning_context(
        allowed_action_types=[ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM], azure_devops_available=False
    )
    result = await agent.plan(make_ctx(SPEC_BODY), CLASSIFICATION, EXTRACTION, context)
    assert result.tool_calls == []
    assert result.rejected_calls == []
    assert fake.calls == []


async def test_plan_rejects_completed_actions(make_ctx, planning_context) -> None:
    agent, _ = make_agent(completion(tool_calls=[tool_call("propose_azure_devops_work_item", WORK_ITEM_ARGS)]))
    context = planning_context(already_completed_actions=["create_azure_devops_work_item: Bug: login failures"])
    result = await agent.plan(make_ctx(SPEC_BODY), CLASSIFICATION, EXTRACTION, context)
    assert result.tool_calls == []
    assert result.rejected_calls[0]["reason"] == "action already completed for this email"


def test_factory_builds_each_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(build_analysis_agent(make_settings(DB)), HeuristicAnalysisAgent)

    agent: EmailAnalysisAgent = build_analysis_agent(
        make_settings(DB, llm_provider=LLMProvider.OPENAI, openai_api_key="sk-test", llm_model="gpt-test")
    )
    assert isinstance(agent, OpenAIAnalysisAgent)
    assert (agent.provider_name, agent.model_name) == ("openai", "gpt-test")
    assert isinstance(agent._client, openai.AsyncOpenAI)

    azure_values: dict[str, Any] = {
        "llm_provider": LLMProvider.AZURE_OPENAI,
        "azure_openai_endpoint": "https://example-resource.openai.azure.com",
        "azure_openai_api_version": "2024-10-21",
        "azure_openai_deployment": "analysis-deployment",
    }
    azure = OpenAIAnalysisAgent(make_settings(DB, azure_openai_api_key="test-key", **azure_values))
    assert isinstance(azure._client, openai.AsyncAzureOpenAI)
    assert azure.model_name == "analysis-deployment"

    created: list[tuple[str, ...]] = []

    class FakeCredential:
        async def close(self) -> None:
            created.append(("closed",))

    def fake_provider(credential: Any, *scopes: str) -> Any:
        created.append(scopes)

        async def token() -> str:
            return "token"

        return token

    import azure.identity.aio as identity

    monkeypatch.setattr(identity, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr(identity, "get_bearer_token_provider", fake_provider)
    entra = OpenAIAnalysisAgent(make_settings(DB, azure_openai_use_entra_id=True, **azure_values))
    assert isinstance(entra._client, openai.AsyncAzureOpenAI)
    assert created == [("https://cognitiveservices.azure.com/.default",)]


async def test_aclose_closes_client_and_credential() -> None:
    closed: list[str] = []

    class ClosingClient(FakeClient):
        async def close(self) -> None:
            closed.append("client")

    agent = OpenAIAnalysisAgent(
        make_settings(DB, llm_provider=LLMProvider.OPENAI, openai_api_key="sk-test", llm_model="gpt-test"),
        client=ClosingClient(),  # type: ignore[arg-type]
    )
    await agent.aclose()
    assert closed == ["client"]
