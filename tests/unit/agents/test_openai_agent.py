"""OpenAIAnalysisAgent against a fake `client.chat.completions.parse` (no network, no real keys)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.agents import openai_agent
from app.agents.base import AgentError, AgentOutputError, AgentProviderError, EmailAnalysisAgent
from app.agents.factory import build_analysis_agent
from app.agents.heuristic import HeuristicAnalysisAgent
from app.agents.openai_agent import OpenAIAnalysisAgent
from app.core.config import LLMProvider
from app.domain.enums import ActionType, EmailCategory, FailureCategory, Priority, Urgency
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


class Unprocessable:
    """A queued response whose body the SDK fails to process (`.parse()` of the raw response raises)."""

    def __init__(self, error: BaseException) -> None:
        self.error = error


class FakeRawResponse:
    def __init__(self, response: Any) -> None:
        self.response = response

    def parse(self) -> Any:
        if isinstance(self.response, Unprocessable):
            raise self.response.error
        return self.response


class FakeCompletions:
    """`client.chat.completions` as the agent uses it: `with_raw_response.parse/create` (HTTP), then `.parse()`."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.methods: list[str] = []
        self.with_raw_response = SimpleNamespace(parse=self._raw("parse"), create=self._raw("create"))

    def _raw(self, method: str) -> Any:
        async def send(**kwargs: Any) -> FakeRawResponse:
            self.calls.append(kwargs)
            self.methods.append(method)
            response = self.responses.pop(0)
            if isinstance(response, BaseException):  # raised by the HTTP exchange (status, connection, ...)
                raise response
            return FakeRawResponse(response)

        return send


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


# ---------------------------------------------------------------------------------------------
# Exception mapping (every SDK error class, status and code path)
# ---------------------------------------------------------------------------------------------


def _json_error() -> json.JSONDecodeError:
    try:
        json.loads("{not json")
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("error", "expected", "message_part"),
    [
        (openai.APITimeoutError(request=REQUEST), AgentProviderError, "timed out"),
        (openai.APIConnectionError(request=REQUEST), AgentProviderError, "connect"),
        (_status_error(openai.RateLimitError, 429), AgentProviderError, "rate limit"),
        (_status_error(openai.InternalServerError, 500), AgentProviderError, "HTTP 500"),
        (_status_error(openai.APIStatusError, 502), AgentProviderError, "HTTP 502"),
        (_status_error(openai.APIStatusError, 408), AgentProviderError, "HTTP 408"),
        (_status_error(openai.AuthenticationError, 401), AgentError, "credentials (HTTP 401)"),
        (_status_error(openai.PermissionDeniedError, 403), AgentError, "credentials (HTTP 403)"),
        (_status_error(openai.BadRequestError, 400, code="content_filter"), AgentOutputError, "content filter"),
        (_status_error(openai.BadRequestError, 400, code="content_policy_violation"), AgentOutputError, "filter"),
        (_status_error(openai.BadRequestError, 400, code="Invalid Value!"), AgentError, "HTTP 400, invalidvalue"),
        (_status_error(openai.BadRequestError, 400), AgentError, "rejected the request (HTTP 400)"),
        (_status_error(openai.NotFoundError, 404, code="model_not_found"), AgentError, "404, model_not_found"),
        (_status_error(openai.ConflictError, 409), AgentError, "HTTP 409"),
        (_status_error(openai.UnprocessableEntityError, 422), AgentError, "HTTP 422"),
        (openai.LengthFinishReasonError(completion=completion()), AgentOutputError, "truncated"),  # type: ignore[arg-type]
        (openai.ContentFilterFinishReasonError(), AgentOutputError, "content filter"),
        (openai.APIResponseValidationError(httpx2.Response(200, request=REQUEST), None), AgentError, "Validation"),
        (openai.OpenAIError("sdk misconfigured"), AgentError, "(OpenAIError)"),
        (_json_error(), AgentOutputError, "schema"),
    ],
)
def test_map_provider_error_table(error: Exception, expected: type[AgentError], message_part: str) -> None:
    mapped = openai_agent.map_provider_error(error)
    assert type(mapped) is expected
    assert mapped is not None
    assert message_part in mapped.message
    assert mapped.retryable is (expected is AgentProviderError)
    assert "provider message" not in mapped.message  # provider body text is never echoed


def test_map_provider_error_passthrough_and_unrelated() -> None:
    own = AgentOutputError("already mapped")
    assert openai_agent.map_provider_error(own) is own
    assert openai_agent.map_provider_error(RuntimeError("bug")) is None
    assert openai_agent.map_provider_error(KeyError("x")) is None


async def test_unrelated_exceptions_are_re_raised_unchanged(make_ctx) -> None:
    agent, _ = make_agent(RuntimeError("unexpected bug"))
    with pytest.raises(RuntimeError, match="unexpected bug"):
        await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())


async def test_agent_errors_from_interpretation_keep_their_identity(make_ctx, spans) -> None:
    agent, _ = make_agent(SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=None)], usage=None))
    with pytest.raises(AgentOutputError, match="no message"):
        await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())
    assert spans[0]["error.type"] == "AgentOutputError"


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(usage=None),  # no `choices` attribute at all
        SimpleNamespace(choices=None, usage=None),
        SimpleNamespace(choices=[SimpleNamespace(message=None)], usage=None),  # no finish_reason, no message
    ],
)
async def test_missing_choices_or_message_are_output_errors(make_ctx, response) -> None:
    agent, _ = make_agent(response)
    with pytest.raises(AgentOutputError):
        await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())


async def test_parsed_dict_and_foreign_models_are_revalidated(make_ctx) -> None:
    as_dict = CLASSIFICATION.model_dump(mode="json")
    agent, _ = make_agent(completion(parsed=as_dict))
    assert (await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())).output == CLASSIFICATION

    class Lookalike(BaseModel):  # not a ClassificationOutput: dumped and re-validated
        model_config = ConfigDict(extra="allow")

    agent, _ = make_agent(completion(parsed=Lookalike(**as_dict)))
    result = (await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())).output
    assert type(result) is ClassificationOutput
    assert result == CLASSIFICATION

    for bad in ({"category": "incident"}, EXTRACTION):  # incomplete dict, wrong model type
        agent, _ = make_agent(completion(parsed=bad))
        with pytest.raises(AgentOutputError, match="schema"):
            await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())


# ---------------------------------------------------------------------------------------------
# Tool-call parsing edge cases
# ---------------------------------------------------------------------------------------------


async def test_tool_call_shapes(make_ctx, planning_context) -> None:
    no_action = {"reason": "FYI only", "rationale": "Informational.", "expected_impact": "Closed."}
    calls = [
        SimpleNamespace(id="a", type="custom", function=SimpleNamespace(name="propose_no_action", arguments="{}")),
        SimpleNamespace(id="b", type="function", function=None),
        # SDK-parsed arguments are used when the raw JSON string is absent
        SimpleNamespace(
            id="c",
            type="function",
            function=SimpleNamespace(name="propose_email_reply", arguments=None, parsed_arguments={"subject": "x"}),
        ),
        SimpleNamespace(id="d", type="function", function=SimpleNamespace(arguments="{}")),  # no name
        SimpleNamespace(id="e", function=SimpleNamespace(name="propose_no_action", arguments=json.dumps(no_action))),
    ]
    agent, _ = make_agent(completion(tool_calls=calls))
    result = await agent.plan(make_ctx(SPEC_BODY), CLASSIFICATION, EXTRACTION, planning_context())
    assert [c.tool_name for c in result.tool_calls] == ["propose_no_action"]
    assert result.tool_calls[0].arguments == no_action
    reasons = [(r["tool_name"], r["reason"].split(" (")[0]) for r in result.rejected_calls]
    assert reasons == [
        ("non_function_tool", "unknown or unavailable tool"),
        ("non_function_tool", "unknown or unavailable tool"),
        ("propose_email_reply", "invalid arguments"),
        ("(empty)", "unknown or unavailable tool"),
    ]


async def test_plan_with_only_valid_calls_has_no_rejections(make_ctx, planning_context) -> None:
    agent, fake = make_agent(completion(tool_calls=[tool_call("propose_azure_devops_work_item", WORK_ITEM_ARGS)]))
    result = await agent.plan(make_ctx(SPEC_BODY), CLASSIFICATION, EXTRACTION, planning_context())
    assert [c.tool_name for c in result.tool_calls] == ["propose_azure_devops_work_item"]
    assert result.rejected_calls == []
    offered = {t["function"]["name"] for t in fake.calls[0]["tools"]}
    assert offered == {
        "propose_azure_devops_work_item",
        "propose_email_reply",
        "propose_follow_up_task",
        "propose_no_action",
    }
    schema = fake.calls[0]["tools"][0]["function"]["parameters"]
    assert schema["additionalProperties"] is False
    assert result.usage.input_tokens == 1000


# ---------------------------------------------------------------------------------------------
# Usage, cost and request options
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usage", "prices", "expected"),
    [
        (SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=None), (None, None), (10, 5, 15, None)),
        (None, (1.0, 2.0), (None, None, None, None)),
        (
            SimpleNamespace(prompt_tokens=10, completion_tokens=None, total_tokens=None),
            (1.0, 2.0),
            (10, None, None, None),
        ),
        (
            SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=0, total_tokens=7),
            (3.0, None),
            (1_000_000, 0, 7, None),
        ),
        (
            SimpleNamespace(prompt_tokens=1234, completion_tokens=567, total_tokens=1801),
            (0.15, 0.6),
            (1234, 567, 1801, 0.000525),
        ),
    ],
)
async def test_usage_and_cost_computation(make_ctx, usage, prices, expected) -> None:
    response = completion(parsed=CLASSIFICATION)
    response.usage = usage
    agent, _ = make_agent(response, llm_input_cost_per_1m_tokens=prices[0], llm_output_cost_per_1m_tokens=prices[1])
    result = await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())
    got = result.usage
    assert (got.input_tokens, got.output_tokens, got.total_tokens, got.estimated_cost_usd) == expected
    assert (got.provider, got.model, got.attempts) == ("openai", "gpt-test", 1)
    assert got.latency_ms >= 0


async def test_default_token_budgets_per_step(make_ctx, planning_context, spans) -> None:
    calls = [tool_call("propose_azure_devops_work_item", WORK_ITEM_ARGS)]
    agent, fake = make_agent(
        completion(parsed=CLASSIFICATION), completion(parsed=EXTRACTION), completion(tool_calls=calls)
    )
    ctx = make_ctx(SPEC_BODY)
    await agent.classify(ctx, TaxonomyConfig())
    await agent.extract(ctx, CLASSIFICATION)
    await agent.plan(ctx, CLASSIFICATION, EXTRACTION, planning_context())
    budgets = [c["max_completion_tokens"] for c in fake.calls]
    assert budgets == [openai_agent.MAX_COMPLETION_TOKENS[s] for s in ("classify", "extract", "plan")]
    assert [s["gen_ai.request.max_tokens"] for s in spans] == budgets
    assert [s["nuvexa.agent.step"] for s in spans] == ["classify", "extract", "plan"]
    assert all(s["gen_ai.response.model"] == "gpt-test-2026" for s in spans)
    assert all(s["gen_ai.response.finish_reasons"] == ["stop"] for s in spans)


async def test_settings_override_token_budget_and_temperature(make_ctx, spans) -> None:
    response = completion(parsed=EXTRACTION)
    response.model = None  # some gateways omit the response model
    agent, fake = make_agent(response, llm_max_completion_tokens=20_000, llm_temperature=0.3, llm_timeout_seconds=7.5)
    await agent.extract(make_ctx(SPEC_BODY), CLASSIFICATION)
    request = fake.calls[0]
    assert request["max_completion_tokens"] == 20_000
    assert request["temperature"] == 0.3
    assert request["timeout"] == 7.5
    assert spans[0]["gen_ai.request.max_tokens"] == 20_000
    assert "gen_ai.response.model" not in spans[0]


async def test_extract_user_message_carries_only_enumerated_classification(make_ctx) -> None:
    classification = CLASSIFICATION.model_copy(update={"rationale": "RATIONALE-MARKER from the model"})
    agent, fake = make_agent(completion(parsed=EXTRACTION))
    await agent.extract(make_ctx(SPEC_BODY), classification)
    system, user = fake.calls[0]["messages"]
    assert "RATIONALE-MARKER" not in user["content"]
    assert "category=incident, urgency=immediate, priority=high, needs_more_info=false" in user["content"]
    assert "verbatim" in system["content"]


# ---------------------------------------------------------------------------------------------
# Client construction (no network: clients are only constructed) and aclose
# ---------------------------------------------------------------------------------------------

AZURE_VALUES: dict[str, Any] = {
    "llm_provider": LLMProvider.AZURE_OPENAI,
    "azure_openai_endpoint": "https://example-resource.openai.azure.com",
    "azure_openai_api_version": "2024-10-21",
    "azure_openai_deployment": "analysis-deployment",
}


def test_openai_client_uses_configured_options() -> None:
    settings = make_settings(
        DB,
        llm_provider=LLMProvider.OPENAI,
        openai_api_key="sk-test-not-a-real-key",
        llm_model="gpt-test",
        openai_base_url="https://proxy.example.invalid/v1",
        llm_max_retries=4,
        llm_timeout_seconds=12.5,
    )
    client = OpenAIAnalysisAgent(settings)._client
    assert isinstance(client, openai.AsyncOpenAI)
    assert str(client.base_url) == "https://proxy.example.invalid/v1/"
    assert (client.max_retries, client.timeout) == (4, 12.5)
    assert client.api_key == "sk-test-not-a-real-key"


def test_azure_api_key_client() -> None:
    agent = OpenAIAnalysisAgent(make_settings(DB, azure_openai_api_key="test-key", llm_max_retries=1, **AZURE_VALUES))
    client = agent._client
    assert isinstance(client, openai.AsyncAzureOpenAI)
    assert client.api_key == "test-key"
    assert client._api_version == "2024-10-21"
    assert client._azure_ad_token_provider is None
    assert client.max_retries == 1
    assert (agent.provider_name, agent.model_name, agent._semconv_provider) == (
        "azure_openai",
        "analysis-deployment",
        "azure.ai.openai",
    )
    assert agent._credential is None


async def test_azure_entra_client_uses_bearer_provider_and_closes_credential(monkeypatch) -> None:
    import azure.identity.aio as identity

    events: list[Any] = []

    class FakeCredential:
        async def close(self) -> None:
            events.append("credential-closed")

    async def token() -> str:
        return "entra-token"

    def fake_provider(credential: Any, *scopes: str) -> Any:
        events.append((type(credential).__name__, scopes))
        return token

    monkeypatch.setattr(identity, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr(identity, "get_bearer_token_provider", fake_provider)
    agent = OpenAIAnalysisAgent(make_settings(DB, azure_openai_use_entra_id=True, **AZURE_VALUES))
    assert isinstance(agent._client, openai.AsyncAzureOpenAI)
    assert agent._client._azure_ad_token_provider is token
    assert isinstance(agent._credential, FakeCredential)
    assert events == [("FakeCredential", (openai_agent.AZURE_COGNITIVE_SERVICES_SCOPE,))]

    closed: list[str] = []

    async def fake_close() -> None:
        closed.append("client")

    monkeypatch.setattr(agent._client, "close", fake_close)
    await agent.aclose()
    assert closed == ["client"]
    assert events[-1] == "credential-closed"


@pytest.mark.parametrize(
    ("base", "update", "message"),
    [
        ({"llm_provider": LLMProvider.HEURISTIC}, {}, "requires LLM_PROVIDER"),
        (
            {"llm_provider": LLMProvider.OPENAI, "openai_api_key": "sk-x", "llm_model": "m"},
            {"openai_api_key": None},
            "OPENAI_API_KEY",
        ),
        ({**AZURE_VALUES, "azure_openai_api_key": "k"}, {"azure_openai_endpoint": None}, "endpoint and API version"),
        ({**AZURE_VALUES, "azure_openai_api_key": "k"}, {"azure_openai_api_version": None}, "endpoint and API version"),
        ({**AZURE_VALUES, "azure_openai_api_key": "k"}, {"azure_openai_api_key": None}, "AZURE_OPENAI_API_KEY"),
    ],
)
def test_construction_refuses_incomplete_configuration(base, update, message) -> None:
    from app.core.errors import ConfigurationError

    settings = make_settings(DB, **base).model_copy(update=update)  # bypasses the settings validator on purpose
    with pytest.raises(ConfigurationError, match=message):
        OpenAIAnalysisAgent(settings)


async def test_aclose_tolerates_clients_without_close() -> None:
    settings = make_settings(DB, llm_provider=LLMProvider.OPENAI, openai_api_key="sk-test", llm_model="gpt-test")
    agent = OpenAIAnalysisAgent(settings, client=FakeClient())  # type: ignore[arg-type]
    await agent.aclose()  # FakeClient has no close(): nothing to do

    holder = SimpleNamespace(close="not callable", chat=None)
    agent = OpenAIAnalysisAgent(settings, client=holder)  # type: ignore[arg-type]
    await agent.aclose()


# ---------------------------------------------------------------------------------------------
# Prompt 5: SDK method per step, unprocessable responses, failure categories
# ---------------------------------------------------------------------------------------------


async def test_structured_steps_use_parse_and_the_plan_step_uses_create(make_ctx, planning_context) -> None:
    tools = [tool_call("propose_azure_devops_work_item", WORK_ITEM_ARGS)]
    agent, fake = make_agent(
        completion(parsed=CLASSIFICATION), completion(parsed=EXTRACTION), completion(tool_calls=tools)
    )
    ctx = make_ctx(SPEC_BODY)
    await agent.classify(ctx, TaxonomyConfig())
    await agent.extract(ctx, CLASSIFICATION)
    await agent.plan(ctx, CLASSIFICATION, EXTRACTION, planning_context())
    assert fake.methods == ["parse", "parse", "create"]
    assert "response_format" not in fake.calls[2]


async def test_one_malformed_tool_call_is_rejected_alone(make_ctx, planning_context) -> None:
    calls = [tool_call("propose_azure_devops_work_item", WORK_ITEM_ARGS), tool_call("propose_email_reply", '{"sub')]
    agent, _ = make_agent(completion(tool_calls=calls))
    result = await agent.plan(make_ctx(SPEC_BODY), CLASSIFICATION, EXTRACTION, planning_context())
    assert [c.tool_name for c in result.tool_calls] == ["propose_azure_devops_work_item"]
    assert [(r["tool_name"], r["reason"].split(" (")[0]) for r in result.rejected_calls] == [
        ("propose_email_reply", "invalid arguments")
    ]


@pytest.mark.parametrize(
    "response",
    [
        Unprocessable(AttributeError("'str' object has no attribute 'choices'")),  # parse() of an HTML page
        Unprocessable(KeyError("choices")),
        "<html><body>Please sign in to the corporate proxy</body></html>",  # create() returns non-JSON as text
        b"\x00binary",
    ],
)
async def test_a_response_that_is_not_a_chat_completion_is_a_provider_error(make_ctx, spans, response) -> None:
    agent, _ = make_agent(response)
    with pytest.raises(AgentProviderError) as exc:
        await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())
    assert exc.value.message == "The model provider returned a response that is not a valid chat completion."
    assert (exc.value.retryable, exc.value.category) == (True, FailureCategory.LLM_PROVIDER)
    assert "proxy" not in exc.value.message
    assert spans[0]["error.type"] == "AgentProviderError"


def _validation_error() -> ValidationError:
    try:
        ClassificationOutput.model_validate_json("{}")
    except ValidationError as exc:
        return exc
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    "error",
    [
        openai.LengthFinishReasonError(completion=completion()),  # type: ignore[arg-type]
        openai.ContentFilterFinishReasonError(),
        _validation_error(),
        _json_error(),
    ],
)
async def test_sdk_errors_while_processing_the_body_keep_their_mapping(make_ctx, error) -> None:
    agent, _ = make_agent(Unprocessable(error))
    with pytest.raises(AgentOutputError):
        await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())


async def test_errors_building_the_request_are_not_mapped(make_ctx) -> None:
    agent, _ = make_agent(TypeError("unexpected keyword argument"))  # a programming error, before any response
    with pytest.raises(TypeError):
        await agent.classify(make_ctx(SPEC_BODY), TaxonomyConfig())


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (_status_error(openai.AuthenticationError, 401), FailureCategory.AUTHORIZATION),
        (_status_error(openai.PermissionDeniedError, 403), FailureCategory.AUTHORIZATION),
        (_status_error(openai.BadRequestError, 400), FailureCategory.CONFIGURATION),
        (_status_error(openai.NotFoundError, 404, code="DeploymentNotFound"), FailureCategory.CONFIGURATION),
        (_status_error(openai.UnprocessableEntityError, 422), FailureCategory.CONFIGURATION),
        (_status_error(openai.BadRequestError, 400, code="content_filter"), FailureCategory.LLM_OUTPUT),
        (_status_error(openai.InternalServerError, 500), FailureCategory.LLM_PROVIDER),
        (_status_error(openai.RateLimitError, 429), FailureCategory.LLM_PROVIDER),
        (openai.APITimeoutError(request=REQUEST), FailureCategory.LLM_PROVIDER),
        (openai.APIResponseValidationError(httpx2.Response(200, request=REQUEST), None), FailureCategory.LLM_PROVIDER),
        (openai.OpenAIError("The api_key client option must be set"), FailureCategory.CONFIGURATION),
        (openai.ContentFilterFinishReasonError(), FailureCategory.LLM_OUTPUT),
        (_json_error(), FailureCategory.LLM_OUTPUT),
    ],
)
def test_map_provider_error_assigns_a_failure_category(error: Exception, category: FailureCategory) -> None:
    mapped = openai_agent.map_provider_error(error)
    assert mapped is not None
    assert mapped.category is category


def test_agent_error_category_defaults_and_overrides() -> None:
    assert AgentError("x").category is FailureCategory.LLM_PROVIDER
    assert AgentError("x", category=FailureCategory.CONFIGURATION).category is FailureCategory.CONFIGURATION
    assert AgentError("x").category is FailureCategory.LLM_PROVIDER  # the override is per instance
    assert AgentOutputError("x").category is FailureCategory.LLM_OUTPUT
    assert AgentProviderError("x").category is FailureCategory.LLM_PROVIDER
