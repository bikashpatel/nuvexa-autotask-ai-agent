"""Azure DevOps REST client: error mapping, sign-in detection, paging, lookup parsing, request hygiene.

Properties (Hypothesis) run against an in-process `httpx.MockTransport` that records every request, so they
can prove that rejected names never reach a URL and that WIQL literals always round-trip.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import respx
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.errors import IntegrationError, IntegrationNotConfigured
from app.domain.enums import FailureCategory
from app.integrations.azure_devops import client as client_module
from app.integrations.azure_devops.base import CreateOutcomeStatus, WorkItemCreateRequest
from app.integrations.azure_devops.client import (
    AzureDevOpsRestClient,
    BearerTokenAuth,
    PatAuth,
    build_patch_document,
    local_request_issues,
    validate_name,
    wiql_literal,
)
from app.integrations.http import RetryPolicy
from tests.unit.integrations.conftest import FAKE_PAT, ado_settings
from tests.unit.integrations.test_azure_devops_client import (
    CREATE_BUG,
    ORG,
    PROJECT,
    TAG,
    WIQL,
    WORK_ITEMS,
    client,
    create_request,
    created,
    wiql_result,
    work_items,
)

DB = "sqlite+aiosqlite:///:memory:"
PROJECTS = f"{ORG}/_apis/projects"
TYPES = f"{PROJECT}/_apis/wit/workitemtypes"


# ---------------------------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"azure_devops_api_version": "latest"}, "API_VERSION"),
        ({"azure_devops_api_version": "7.1-preview.1"}, None),
        ({"azure_devops_base_url": "http://dev.azure.com"}, "https"),
        ({"azure_devops_organization": "-leading-dash"}, "ORGANIZATION"),
        ({"azure_devops_organization": "a" * 65}, "ORGANIZATION"),
    ],
)
def test_constructor_validates_settings(overrides: dict[str, Any], message: str | None) -> None:
    settings_ = ado_settings(DB, **overrides)
    if message is None:
        assert AzureDevOpsRestClient(settings_, auth=PatAuth(FAKE_PAT)).organization == "contoso-test"
        return
    with pytest.raises(IntegrationNotConfigured, match=message):
        AzureDevOpsRestClient(settings_, auth=PatAuth(FAKE_PAT))


def test_constructor_requires_an_organization() -> None:
    settings_ = ado_settings(DB).model_copy(update={"azure_devops_organization": None})
    with pytest.raises(IntegrationNotConfigured):
        AzureDevOpsRestClient(settings_, auth=PatAuth(FAKE_PAT))


async def test_owned_client_settings_and_injected_client_is_not_closed(pat_settings, http_client) -> None:
    owned = AzureDevOpsRestClient(pat_settings, auth=PatAuth(FAKE_PAT))
    inner = owned._http()
    assert inner is owned._http()
    assert inner.follow_redirects is False
    assert inner.timeout.read == pat_settings.azure_devops_timeout_seconds
    await owned.aclose()
    await owned.aclose()  # idempotent
    assert inner.is_closed

    shared = AzureDevOpsRestClient(pat_settings, auth=PatAuth(FAKE_PAT), http_client=http_client)
    await shared.aclose()
    assert not http_client.is_closed


def test_html_url_and_auth_reprs(pat_settings, token_provider) -> None:
    ado = AzureDevOpsRestClient(pat_settings, auth=PatAuth(FAKE_PAT))
    assert ado.html_url("Customer Portal", 7) == f"{ORG}/Customer%20Portal/_workitems/edit/7"
    assert repr(BearerTokenAuth(token_provider)) == "BearerTokenAuth(<redacted>)"
    assert repr(PatAuth(FAKE_PAT)) == "PatAuth(<redacted>)"


# ---------------------------------------------------------------------------------------------
# Error mapping for idempotent reads (every status class + sign-in detection)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "category", "retryable", "message"),
    [
        (
            httpx.Response(400, json={"message": "TF\x00 bad\n request"}),  # no code: the text is never echoed
            FailureCategory.VALIDATION,
            False,
            "Azure DevOps rejected the request (list work item types)",
        ),
        (httpx.Response(401, json={"message": "nope"}), FailureCategory.AUTHORIZATION, False, "authentication failed"),
        (
            httpx.Response(403, json={"message": "VS403"}),
            FailureCategory.PERMISSION,
            False,
            "list work item types: VS403",
        ),
        (
            httpx.Response(404, json={"message": 42}),
            FailureCategory.NOT_FOUND,
            False,
            "not found (list work item types)",
        ),
        (httpx.Response(409, json=["not", "a", "dict"]), FailureCategory.VALIDATION, False, "failed (HTTP 409)"),
        (httpx.Response(422, text="not json"), FailureCategory.VALIDATION, False, "failed (HTTP 422)"),
        (httpx.Response(500, json={"message": "x" * 900}), FailureCategory.TRANSIENT, True, "failed (HTTP 500)"),
        (httpx.Response(503), FailureCategory.TRANSIENT, True, "HTTP 503"),
        (httpx.Response(429), FailureCategory.RATE_LIMITED, True, "HTTP 429"),
        (
            httpx.Response(302, headers={"Location": "https://login.example"}),
            FailureCategory.AUTHORIZATION,
            False,
            "sign-in",
        ),
        (httpx.Response(203, json={"value": []}), FailureCategory.AUTHORIZATION, False, "sign-in"),
        (httpx.Response(200, html="<html>Sign in</html>"), FailureCategory.AUTHORIZATION, False, "sign-in"),
        (httpx.Response(100), FailureCategory.INTERNAL, False, "HTTP 100"),
    ],
)
@respx.mock
async def test_read_error_mapping(pat_settings, http_client, sleep, response, category, retryable, message) -> None:
    respx.get(url__startswith=TYPES).mock(return_value=response)
    with pytest.raises(IntegrationError) as info:
        await client(pat_settings, http_client, sleep, max_retries=1).list_work_item_types("NUVEXA-Demo")
    assert info.value.category == category
    assert info.value.retryable is retryable
    assert info.value.status == response.status_code
    assert message in info.value.message
    assert len(info.value.message) < 600  # provider messages are truncated
    assert FAKE_PAT not in info.value.message


def _echo(secret: str) -> str:
    return f"Request headers: Authorization: Basic {secret}; PAT={secret}"


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (
            {
                "message": f"TF401320: Rule Error for field Area Path. {_echo(FAKE_PAT)}",
                "typeKey": "RuleValidationException",
                "customProperties": {"FieldReferenceName": "System.AreaPath"},
            },
            ": TF401320, RuleValidationException, field System.AreaPath",
        ),
        ({"message": f"  VS403403: denied. {_echo(FAKE_PAT)}"}, ": VS403403"),
        ({"message": f"{_echo(FAKE_PAT)} TF401320", "typeKey": FAKE_PAT}, ""),  # code not leading, key not a type
        ({"message": "TF4013201234: too many digits", "typeKey": "Rule Validation Exception"}, ""),
        ({"message": "TF401320x: glued", "typeKey": "A" * 90 + "Exception"}, ""),
        ({"message": 42, "typeKey": ["RuleValidationException"], "customProperties": "System.Title"}, ""),
        ({"customProperties": {"FieldReferenceName": "System.Title; " + FAKE_PAT}}, ""),
        ({"customProperties": {"FieldReferenceName": "eyJhbGciOi." + "x" * 200 + ".sig"}}, ""),  # token-shaped
        (
            {"customProperties": {"FieldReferenceName": "Microsoft.VSTS.Common.Severity"}},
            ": field Microsoft.VSTS.Common.Severity",
        ),
    ],
)
@respx.mock
async def test_error_bodies_contribute_only_allow_listed_identifiers(
    pat_settings, http_client, sleep, body: dict[str, Any], detail: str
) -> None:
    # Regression (Prompt 5): free-form Azure DevOps error text (which can echo the PAT) is never propagated.
    respx.get(url__startswith=TYPES).mock(return_value=httpx.Response(400, json=body))
    with pytest.raises(IntegrationError) as info:
        await client(pat_settings, http_client, sleep).list_work_item_types("NUVEXA-Demo")
    assert info.value.message == "Azure DevOps rejected the request (list work item types)" + detail
    assert FAKE_PAT not in info.value.message
    assert "Request headers" not in info.value.message


@respx.mock
async def test_validation_issues_carry_codes_but_never_the_error_text(pat_settings, http_client, sleep) -> None:
    respx.post(url__regex=CREATE_BUG).mock(
        return_value=httpx.Response(
            400,
            json={
                "message": f"TF401320: Rule Error for field Severity. {_echo(FAKE_PAT)}",
                "typeKey": "RuleValidationException",
                "customProperties": {"FieldReferenceName": "Microsoft.VSTS.Common.Severity"},
            },
        )
    )
    result = await client(pat_settings, http_client, sleep).validate_work_item(create_request())
    [issue] = result.issues
    assert (issue.field, issue.message) == (
        "Microsoft.VSTS.Common.Severity",
        "Azure DevOps rejected the work item (TF401320, RuleValidationException)",
    )


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"string"'])
@respx.mock
async def test_unreadable_success_body_is_transient(pat_settings, http_client, sleep, body: bytes) -> None:
    respx.get(url__startswith=TYPES).mock(return_value=httpx.Response(200, content=body))
    with pytest.raises(IntegrationError) as info:
        await client(pat_settings, http_client, sleep).list_work_item_types("NUVEXA-Demo")
    assert info.value.category == FailureCategory.TRANSIENT
    assert info.value.retryable is True
    assert "unreadable" in info.value.message


@respx.mock
async def test_work_item_types_skip_malformed_entries(pat_settings, http_client, sleep) -> None:
    respx.get(url__startswith=TYPES).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"name": "Bug", "isDisabled": "yes"}, {"name": 5}, "junk", {"referenceName": "x"}, None]},
        )
    )
    types = await client(pat_settings, http_client, sleep).list_work_item_types("NUVEXA-Demo")
    assert [(t.name, t.is_disabled, t.reference_name, t.description) for t in types] == [("Bug", False, None, None)]


@respx.mock
async def test_work_item_types_without_value(pat_settings, http_client, sleep) -> None:
    respx.get(url__startswith=TYPES).mock(return_value=httpx.Response(200, json={"count": 0}))
    assert await client(pat_settings, http_client, sleep).list_work_item_types("NUVEXA-Demo") == []


# ---------------------------------------------------------------------------------------------
# Projects: paging and errors
# ---------------------------------------------------------------------------------------------


@respx.mock
async def test_projects_paging_with_body_token_repeat_guard_and_bad_items(pat_settings, http_client, sleep) -> None:
    route = respx.get(url__startswith=PROJECTS).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "value": [{"id": "p1", "name": "A"}, {"id": 1, "name": "bad id"}, {"name": "no id"}, "junk"],
                    "continuationToken": "tok en&x=1",
                },
            ),
            httpx.Response(
                200, headers={"x-ms-continuationtoken": "tok en&x=1"}, json={"value": [{"id": "p2", "name": "B"}]}
            ),
        ]
    )
    projects = await client(pat_settings, http_client, sleep).list_projects()
    assert [p.name for p in projects] == ["A", "B"]  # stopped: the second page repeated the token
    second = route.calls[1].request
    assert second.url.params["continuationToken"] == "tok en&x=1"
    assert b"tok%20en%26x%3D1" in second.url.raw_path
    assert second.url.params["$top"] == "100"


@respx.mock
async def test_projects_stop_after_the_page_limit(pat_settings, http_client, sleep, monkeypatch) -> None:
    monkeypatch.setattr(client_module, "_MAX_PROJECT_PAGES", 3)
    counter = iter(range(100))
    route = respx.get(url__startswith=PROJECTS).mock(
        side_effect=lambda request: httpx.Response(
            200, json={"value": [{"id": f"p{next(counter)}", "name": "P"}], "continuationToken": str(len(route.calls))}
        )
    )
    projects = await client(pat_settings, http_client, sleep).list_projects()
    assert route.call_count == 3
    assert len(projects) == 3


@respx.mock
async def test_projects_empty_body_and_errors(pat_settings, http_client, sleep) -> None:
    respx.get(url__startswith=PROJECTS).mock(
        side_effect=[httpx.Response(200, content=b"[]"), httpx.Response(401), httpx.Response(203)]
    )
    ado = client(pat_settings, http_client, sleep)
    assert await ado.list_projects() == []
    for category in (FailureCategory.AUTHORIZATION, FailureCategory.AUTHORIZATION):
        with pytest.raises(IntegrationError) as info:
            await ado.list_projects()
        assert info.value.category == category


@pytest.mark.parametrize(
    ("response", "ok", "message"),
    [
        (httpx.Response(403), False, "not permitted to list projects"),
        (httpx.Response(404), False, "organization was not found"),
        (httpx.Response(429), False, "throttling"),
        (httpx.Response(500), False, "could not be reached (transient)"),
        (httpx.Response(200, html="<html/>"), False, "Authentication failed"),
        (httpx.Response(200, json={"value": []}), True, "Connected to Azure DevOps organization 'contoso-test'."),
    ],
)
@respx.mock
async def test_connection_messages(pat_settings, http_client, sleep, response, ok, message) -> None:
    respx.get(url__startswith=PROJECTS).mock(return_value=response)
    check = await client(pat_settings, http_client, sleep, max_retries=0).test_connection()
    assert check.ok is ok
    assert message in check.message
    assert check.latency_ms is not None
    assert check.latency_ms >= 0


# ---------------------------------------------------------------------------------------------
# Tag lookup parsing
# ---------------------------------------------------------------------------------------------


@respx.mock
async def test_lookup_ignores_junk_ids_caps_and_skips_deleted_items(pat_settings, http_client, sleep) -> None:
    junk = [{"id": True}, {"id": "7"}, {"noid": 1}, "x", None]
    ids = [{"id": i} for i in range(30, 0, -1)]
    wiql = respx.post(url__startswith=WIQL).mock(return_value=httpx.Response(200, json={"workItems": junk + ids}))
    items = respx.get(url__startswith=WORK_ITEMS).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    None,  # deleted (errorPolicy=omit)
                    {"id": "3", "fields": {}},
                    {
                        "id": 2,
                        "rev": 4,
                        "fields": {"System.TeamProject": "NUVEXA-Demo", "System.Tags": f" {TAG.upper()} ; x"},
                    },
                    {"id": 1, "fields": {"System.Tags": "other"}},
                ]
            },
        )
    )
    found = await client(pat_settings, http_client, sleep).find_by_idempotency_tag("NUVEXA-Demo", TAG)
    assert found is not None
    assert (found.id, found.rev, found.project, found.title) == (2, 4, "NUVEXA-Demo", None)
    assert wiql.calls.last.request.url.params["$top"] == "20"
    params = items.calls.last.request.url.params
    assert params["ids"] == ",".join(str(i) for i in range(1, 21))
    assert params["errorPolicy"] == "omit"


@respx.mock
async def test_lookup_trusts_items_without_a_tags_field(pat_settings, http_client, sleep) -> None:
    respx.post(url__startswith=WIQL).mock(return_value=wiql_result(9))
    respx.get(url__startswith=WORK_ITEMS).mock(return_value=httpx.Response(200, json={"value": [{"id": 9}]}))
    found = await client(pat_settings, http_client, sleep).find_by_idempotency_tag("NUVEXA-Demo", TAG)
    assert found is not None
    assert (found.id, found.project, found.url) == (9, "NUVEXA-Demo", f"{PROJECT}/_workitems/edit/9")


@respx.mock
async def test_lookup_with_no_matching_detail_returns_none(pat_settings, http_client, sleep) -> None:
    respx.post(url__startswith=WIQL).mock(return_value=httpx.Response(200, json={"workItems": None}))
    assert await client(pat_settings, http_client, sleep).find_by_idempotency_tag("NUVEXA-Demo", TAG) is None
    respx.post(url__startswith=WIQL).mock(return_value=wiql_result(4))
    respx.get(url__startswith=WORK_ITEMS).mock(return_value=httpx.Response(200, json={"count": 0}))
    assert await client(pat_settings, http_client, sleep).find_by_idempotency_tag("NUVEXA-Demo", TAG) is None


# ---------------------------------------------------------------------------------------------
# validate_work_item
# ---------------------------------------------------------------------------------------------


async def test_validate_reports_local_issues_without_any_request(pat_settings, http_client, sleep) -> None:
    request = create_request()
    request.fields["System.Title"] = " "
    request.fields["Bad Field"] = "x"
    with respx.mock:  # nothing mocked
        result = await client(pat_settings, http_client, sleep).validate_work_item(request)
    assert result.valid is False
    assert {i.message for i in result.issues} == {"Title is required", "Invalid field reference name"}


@respx.mock
async def test_validate_400_without_details_and_other_errors(pat_settings, http_client, sleep) -> None:
    respx.post(url__regex=CREATE_BUG).mock(
        side_effect=[
            httpx.Response(400, content=b"oops"),
            httpx.Response(403, json={"message": "denied"}),
            httpx.Response(200, html="<html/>"),
        ]
    )
    ado = client(pat_settings, http_client, sleep)
    bad = await ado.validate_work_item(create_request())
    assert bad.valid is False
    assert (bad.issues[0].field, bad.issues[0].message) == (None, "Azure DevOps rejected the work item")
    with pytest.raises(IntegrationError) as forbidden:
        await ado.validate_work_item(create_request())
    assert forbidden.value.category == FailureCategory.PERMISSION
    assert forbidden.value.message == "The Azure DevOps identity is not permitted to validate work items"
    with pytest.raises(IntegrationError) as sign_in:
        await ado.validate_work_item(create_request())
    assert sign_in.value.category == FailureCategory.AUTHORIZATION


# ---------------------------------------------------------------------------------------------
# Create attempts
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("side_effect", "status", "category", "posts", "lookups"),
    [
        ([httpx.Response(502), created()], CreateOutcomeStatus.CREATED, None, 2, 2),  # ambiguous, absent, resent
        ([httpx.Response(504), httpx.Response(504)], CreateOutcomeStatus.FAILED, FailureCategory.TRANSIENT, 2, 3),
        ([httpx.Response(408), created()], CreateOutcomeStatus.CREATED, None, 2, 1),  # refused -> resent, no lookup
        (
            [httpx.Response(409, json={"message": "conflict"})],
            CreateOutcomeStatus.FAILED,
            FailureCategory.VALIDATION,
            1,
            1,
        ),
        ([httpx.Response(302)], CreateOutcomeStatus.FAILED, FailureCategory.AUTHORIZATION, 1, 1),
        ([httpx.Response(200, html="<html/>")], CreateOutcomeStatus.FAILED, FailureCategory.AUTHORIZATION, 1, 1),
        ([httpx.Response(200, json={"rev": 1}), created()], CreateOutcomeStatus.CREATED, None, 2, 2),  # no id
        ([httpx.Response(200, content=b"garbage"), created()], CreateOutcomeStatus.CREATED, None, 2, 2),
        ([httpx.RemoteProtocolError("dropped"), created()], CreateOutcomeStatus.CREATED, None, 2, 2),
        ([httpx.PoolTimeout("busy"), created()], CreateOutcomeStatus.CREATED, None, 2, 1),
        ([httpx.WriteTimeout("slow")] * 2, CreateOutcomeStatus.FAILED, FailureCategory.TIMEOUT, 2, 3),
    ],
)
@respx.mock
async def test_create_attempt_classification(
    pat_settings, http_client, sleep, side_effect, status, category, posts, lookups
) -> None:
    wiql = respx.post(url__startswith=WIQL).mock(return_value=wiql_result())
    create = respx.post(url__regex=CREATE_BUG).mock(side_effect=side_effect)
    outcome = await client(pat_settings, http_client, sleep, max_retries=1).create_work_item_idempotent(
        create_request()
    )
    assert outcome.status is status
    assert outcome.error_category is category
    assert create.call_count == posts
    assert wiql.call_count == lookups
    if status is CreateOutcomeStatus.CREATED:
        assert outcome.work_item is not None
        assert outcome.work_item.id == 4242
        assert outcome.retry_count == 1


@respx.mock
async def test_created_without_id_found_by_lookup_is_not_resent(pat_settings, http_client, sleep) -> None:
    respx.post(url__startswith=WIQL).mock(side_effect=[wiql_result(), wiql_result(77)])
    respx.get(url__startswith=WORK_ITEMS).mock(return_value=work_items(77))
    create = respx.post(url__regex=CREATE_BUG).mock(return_value=httpx.Response(200, json={}))
    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())
    assert outcome.status is CreateOutcomeStatus.CREATED
    assert outcome.work_item is not None
    assert outcome.work_item.id == 77
    assert outcome.response_summary["recovered_after_ambiguous_error"] is True
    assert create.call_count == 1


class FlakyTokens:
    """Token provider that succeeds for the duplicate check, then fails as scripted."""

    def __init__(self, *errors: Exception | None) -> None:
        self.script = list(errors)

    async def get_access_token(self, scopes: tuple[str, ...]) -> str:
        error = self.script.pop(0) if self.script else None
        if error is not None:
            raise error
        return "fake-token"


@pytest.mark.parametrize(
    ("token_error", "status", "category", "posts"),
    [
        (
            IntegrationError("re-auth", category=FailureCategory.AUTHORIZATION),
            CreateOutcomeStatus.FAILED,
            FailureCategory.AUTHORIZATION,
            0,
        ),
        (
            IntegrationError("blip", category=FailureCategory.TRANSIENT, retryable=True),
            CreateOutcomeStatus.CREATED,
            None,
            1,
        ),
    ],
)
@respx.mock
async def test_token_failure_before_sending_is_never_ambiguous(
    database_url, http_client, sleep, token_error, status, category, posts
) -> None:
    settings_ = ado_settings(database_url, azure_devops_mode="entra_delegated", azure_devops_pat=None)
    tokens = FlakyTokens(None, token_error)  # lookup ok, first attempt fails, later calls ok
    auth = BearerTokenAuth(tokens, cache_seconds=0)
    ado = AzureDevOpsRestClient(settings_, auth=auth, http_client=http_client, sleep=sleep)
    wiql = respx.post(url__startswith=WIQL).mock(return_value=wiql_result())
    create = respx.post(url__regex=CREATE_BUG).mock(return_value=created())
    outcome = await ado.create_work_item_idempotent(create_request())
    assert outcome.status is status
    assert outcome.error_category is category
    assert create.call_count == posts
    assert wiql.call_count == 1  # nothing was sent, so no reconcile lookup is needed


@respx.mock
async def test_create_span_attributes(pat_settings, http_client, sleep, monkeypatch) -> None:
    recorded: list[dict[str, Any]] = []

    class Span:
        def __init__(self, attributes: dict[str, Any]) -> None:
            self.attributes = attributes

        def set_attribute(self, key: str, value: Any) -> None:
            self.attributes[key] = value

    @contextmanager
    def fake_span(name: str, attributes: dict[str, Any] | None = None):
        attrs = {"name": name, **(attributes or {})}
        recorded.append(attrs)
        yield Span(attrs)

    monkeypatch.setattr(client_module, "start_span", fake_span)
    respx.post(url__startswith=WIQL).mock(return_value=wiql_result())
    respx.post(url__regex=CREATE_BUG).mock(
        side_effect=[httpx.Response(503), httpx.Response(400, json={"message": "bad"})]
    )
    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())
    assert outcome.status is CreateOutcomeStatus.FAILED
    assert recorded == [
        {
            "name": "azure_devops.create_work_item",
            "ado.mode": "pat",
            "ado.project": "NUVEXA-Demo",
            "ado.work_item_type": "Bug",
            "ado.outcome": "failed",
            "ado.retry_count": 1,
            "http.response.status_code": 400,
            "error.type": "validation",
        }
    ]
    assert not any(FAKE_PAT in str(v) for v in recorded[0].values())


# ---------------------------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------------------------


def test_patch_document_merges_existing_tags_case_insensitively() -> None:
    request = create_request()
    request.fields["System.Tags"] = "Email; ; urgent"
    ops = build_patch_document(request)
    assert [op["path"] for op in ops].count("/fields/System.Tags") == 1
    tags = next(op["value"] for op in ops if op["path"] == "/fields/System.Tags")
    assert tags == f"Email; urgent; incident; {TAG}"


def test_local_request_issues_cover_every_rule() -> None:
    request = WorkItemCreateRequest(
        project="a/b",
        work_item_type="..",
        fields={"System.Title": None, "Custom.List": ["x"], "Custom.Ok": None, "Custom.Bool": True, "bad ref": 1},
        tags=["ok", "semi;colon", "ctrl\x07"],
        idempotency_tag="nuvexa-XYZ",
    )
    messages = [i.message for i in local_request_issues(request)]
    assert messages == [
        "Invalid Azure DevOps project name",
        "Invalid Azure DevOps work item type name",
        "Invalid idempotency tag",
        "Title is required",
        "Field values must be strings or numbers",
        "Invalid field reference name",
        "Tags must not contain ';' or control characters",
        "Tags must not contain ';' or control characters",
    ]
    assert local_request_issues(create_request()) == []


# ---------------------------------------------------------------------------------------------
# Properties: URL segments and WIQL literals
# ---------------------------------------------------------------------------------------------

names = st.one_of(
    st.text(max_size=30),
    st.lists(
        st.sampled_from(["a", "B", " ", "/", "\\", "?", "#", "..", ".", "%2F", "'", "é", "\x00", "\n", "$"]), max_size=8
    ).map("".join),
)


def _run_with_transport(handler: Callable[[httpx.Request], httpx.Response], coro_factory) -> Any:
    async def main() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ado = AzureDevOpsRestClient(
                ado_settings(DB), auth=PatAuth(FAKE_PAT), http_client=http, retry_policy=RetryPolicy(max_retries=0)
            )
            return await coro_factory(ado)

    return asyncio.run(main())


@settings(max_examples=150, deadline=None)
@given(project=names)
def test_rejected_names_never_reach_a_url_and_accepted_names_stay_one_segment(project: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"workItems": []})

    try:
        validate_name(project)
    except IntegrationError:
        accepted = False
    else:
        accepted = True

    request = create_request().model_copy(update={"project": project})
    outcome = _run_with_transport(handler, lambda ado: ado.create_work_item_idempotent(request))
    if not accepted:
        assert seen == []
        assert outcome.status is CreateOutcomeStatus.FAILED
        assert outcome.error_category is FailureCategory.VALIDATION
        return
    assert seen  # the duplicate check ran
    for sent in seen:
        assert sent.url.host == "dev.azure.com"
        segments = sent.url.raw_path.split(b"?")[0].decode("ascii").split("/")
        assert segments[1] == "contoso-test"
        assert unquote(segments[2]) == project  # exactly one segment, decoded back to the name
        assert segments[3] == "_apis"
        assert set(sent.url.params) <= {"api-version", "$top", "ids", "fields", "errorPolicy"}


def _unescape_wiql(literal: str) -> str:
    assert literal.startswith("'")
    assert literal.endswith("'")
    inner = literal[1:-1]
    assert "'" not in inner.replace("''", "")  # every quote inside is doubled
    return inner.replace("''", "'")


@settings(max_examples=200, deadline=None)
@given(st.text())
def test_wiql_literal_round_trips(value: str) -> None:
    assert _unescape_wiql(wiql_literal(value)) == value


def _is_valid(name: str) -> bool:
    try:
        validate_name(name)
    except IntegrationError:
        return False
    return True


@settings(max_examples=100, deadline=None)
@given(project=names.filter(_is_valid), tags=st.just(TAG))
def test_wiql_query_embeds_the_project_as_a_single_literal(project: str, tags: str) -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(json.loads(request.content)["query"])
        return httpx.Response(200, json={"workItems": []})

    assert _run_with_transport(handler, lambda ado: ado.find_by_idempotency_tag(project, tags)) is None
    (query,) = queries
    prefix = "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = "
    suffix = f" AND [System.Tags] CONTAINS '{TAG}' ORDER BY [System.Id] ASC"
    assert query.startswith(prefix)
    assert query.endswith(suffix)
    assert _unescape_wiql(query[len(prefix) : -len(suffix)]) == project
