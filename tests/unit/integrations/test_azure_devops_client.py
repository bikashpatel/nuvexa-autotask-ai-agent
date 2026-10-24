from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from app.core.errors import IntegrationError, IntegrationNotConfigured
from app.domain.enums import FailureCategory
from app.integrations.azure_devops.base import CreateOutcomeStatus, WorkItemCreateRequest
from app.integrations.azure_devops.client import (
    AzureDevOpsRestClient,
    BearerTokenAuth,
    PatAuth,
    validate_name,
    wiql_literal,
)
from app.integrations.azure_devops.mapping import build_create_request, idempotency_tag
from app.integrations.http import RetryPolicy
from app.integrations.microsoft_graph.base import AZURE_DEVOPS_DELEGATED_SCOPES
from app.schemas.actions import AzureDevOpsWorkItemPayload, SourceEmailReference
from tests.unit.integrations.conftest import FAKE_PAT, ado_settings

ORG = "https://dev.azure.com/contoso-test"
PROJECT = f"{ORG}/NUVEXA-Demo"
WIQL = f"{PROJECT}/_apis/wit/wiql"
WORK_ITEMS = f"{PROJECT}/_apis/wit/workitems"
CREATE_BUG = rf"^{PROJECT}/_apis/wit/workitems/\$Bug\?"
KEY = "0123456789abcdef" + "f" * 48
TAG = idempotency_tag(KEY)


def payload(**overrides) -> AzureDevOpsWorkItemPayload:
    values: dict[str, Any] = {
        "project": "NUVEXA-Demo",
        "work_item_type": "Bug",
        "title": "Production login failures after deployment",
        "description": "Users cannot log in.",
        "priority": 1,
        "tags": ["email", "incident"],
        "source_email": SourceEmailReference(
            email_id=uuid.uuid4(),
            subject="URGENT: Production login failures after deployment",
            sender="jordan.ellis@example.com",
            received_at=datetime(2026, 10, 25, 9, 0, tzinfo=UTC),
        ),
    }
    values.update(overrides)
    return AzureDevOpsWorkItemPayload(**values)


def create_request(**overrides) -> WorkItemCreateRequest:
    return build_create_request(payload(**overrides), idempotency_key=KEY)


def client(settings, http_client, sleep, *, max_retries: int = 3) -> AzureDevOpsRestClient:
    return AzureDevOpsRestClient(
        settings,
        auth=PatAuth(settings.azure_devops_pat),
        http_client=http_client,
        sleep=sleep,
        retry_policy=RetryPolicy(max_retries=max_retries),
    )


def wiql_result(*ids: int) -> httpx.Response:
    return httpx.Response(200, json={"queryType": "flat", "workItems": [{"id": i, "url": "x"} for i in ids]})


def work_items(*ids: int, tags: str = f"email; {TAG}") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "count": len(ids),
            "value": [
                {
                    "id": i,
                    "rev": 1,
                    "fields": {
                        "System.Id": i,
                        "System.Title": "Production login failures after deployment",
                        "System.TeamProject": "NUVEXA-Demo",
                        "System.WorkItemType": "Bug",
                        "System.State": "New",
                        "System.Tags": tags,
                    },
                }
                for i in ids
            ],
        },
    )


def created(item_id: int = 4242) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": item_id,
            "rev": 1,
            "fields": {"System.State": "New", "System.TeamProject": "NUVEXA-Demo", "System.WorkItemType": "Bug"},
            "_links": {"html": {"href": f"{ORG}/web/wi.aspx?id={item_id}"}},
            "url": f"{ORG}/_apis/wit/workItems/{item_id}",
        },
    )


# ---------------------------------------------------------------------------------------------


@respx.mock
async def test_create_success_sends_json_patch_with_tag(pat_settings, http_client, sleep):
    wiql = respx.post(url__startswith=WIQL).mock(return_value=wiql_result())
    create = respx.post(url__regex=CREATE_BUG).mock(return_value=created())

    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())

    assert outcome.status is CreateOutcomeStatus.CREATED
    assert outcome.work_item is not None
    assert outcome.work_item.id == 4242
    assert outcome.work_item.url == f"{PROJECT}/_workitems/edit/4242"
    assert outcome.retry_count == 0
    assert outcome.http_status == 200
    assert outcome.response_summary == {"id": 4242, "rev": 1, "state": "New", "url": f"{PROJECT}/_workitems/edit/4242"}
    assert wiql.call_count == 1

    request = create.calls.last.request
    assert request.headers["Content-Type"] == "application/json-patch+json"
    assert request.url.params["api-version"] == "7.1"
    assert "validateOnly" not in request.url.params
    expected_auth = "Basic " + base64.b64encode(f":{FAKE_PAT}".encode()).decode()
    assert request.headers["Authorization"] == expected_auth
    ops = json.loads(request.content)
    assert all(op["op"] == "add" for op in ops)
    by_path = {op["path"]: op["value"] for op in ops}
    assert by_path["/fields/System.Title"] == "Production login failures after deployment"
    assert by_path["/fields/Microsoft.VSTS.Common.Priority"] == 1
    assert by_path["/fields/System.Tags"] == f"email; incident; {TAG}"
    assert "Created by NUVEXA" in by_path["/fields/System.Description"]

    query = json.loads(wiql.calls.last.request.content)["query"]
    assert f"[System.Tags] CONTAINS '{TAG}'" in query
    assert "[System.TeamProject] = 'NUVEXA-Demo'" in query


@respx.mock
async def test_existing_item_is_returned_without_post(pat_settings, http_client, sleep):
    respx.post(url__startswith=WIQL).mock(return_value=wiql_result(77))
    respx.get(url__startswith=WORK_ITEMS).mock(return_value=work_items(77))
    create = respx.post(url__regex=CREATE_BUG).mock(return_value=created())

    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())

    assert outcome.status is CreateOutcomeStatus.EXISTING
    assert outcome.work_item is not None
    assert outcome.work_item.id == 77
    assert not create.called


@respx.mock
async def test_sign_in_page_203_is_authorization_failure(pat_settings, http_client, sleep):
    respx.post(url__startswith=WIQL).mock(return_value=wiql_result())
    create = respx.post(url__regex=CREATE_BUG).mock(
        return_value=httpx.Response(203, html="<html><title>Sign In</title></html>")
    )
    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())
    assert outcome.status is CreateOutcomeStatus.FAILED
    assert outcome.error_category is FailureCategory.AUTHORIZATION
    assert create.call_count == 1
    assert FAKE_PAT not in (outcome.error_message or "")


@respx.mock
async def test_html_200_on_lookup_is_authorization_failure_and_nothing_is_created(pat_settings, http_client, sleep):
    respx.post(url__startswith=WIQL).mock(return_value=httpx.Response(200, html="<html>Sign in</html>"))
    create = respx.post(url__regex=CREATE_BUG).mock(return_value=created())
    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())
    assert outcome.status is CreateOutcomeStatus.FAILED
    assert outcome.error_category is FailureCategory.AUTHORIZATION
    assert not create.called


@respx.mock
async def test_400_is_permanent_validation_failure_without_retry(pat_settings, http_client, sleep):
    respx.post(url__startswith=WIQL).mock(return_value=wiql_result())
    create = respx.post(url__regex=CREATE_BUG).mock(
        return_value=httpx.Response(
            400,
            json={
                "message": "TF401320: Rule Error for field Area Path. Error code: Required, InvalidEmpty.",
                "typeKey": "RuleValidationException",
            },
        )
    )
    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())
    assert outcome.status is CreateOutcomeStatus.FAILED
    assert outcome.error_category is FailureCategory.VALIDATION
    assert "TF401320" in (outcome.error_message or "")
    assert outcome.http_status == 400
    assert create.call_count == 1
    assert sleep.calls == []


@respx.mock
@pytest.mark.parametrize(
    ("status", "category"),
    [(401, FailureCategory.AUTHORIZATION), (403, FailureCategory.PERMISSION), (404, FailureCategory.NOT_FOUND)],
)
async def test_auth_permission_not_found_are_never_retried(pat_settings, http_client, sleep, status, category):
    respx.post(url__startswith=WIQL).mock(return_value=wiql_result())
    create = respx.post(url__regex=CREATE_BUG).mock(return_value=httpx.Response(status, json={"message": "no"}))
    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())
    assert outcome.status is CreateOutcomeStatus.FAILED
    assert outcome.error_category is category
    assert create.call_count == 1


@respx.mock
async def test_503_is_retried_honouring_retry_after_then_succeeds(pat_settings, http_client, sleep):
    wiql = respx.post(url__startswith=WIQL).mock(return_value=wiql_result())
    create = respx.post(url__regex=CREATE_BUG).mock(
        side_effect=[httpx.Response(503, headers={"Retry-After": "2"}), created()]
    )
    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())
    assert outcome.status is CreateOutcomeStatus.CREATED
    assert outcome.retry_count == 1
    assert create.call_count == 2
    assert sleep.calls == [2.0]
    assert wiql.call_count == 1  # 503 = refused before processing; no extra lookup needed


@respx.mock
async def test_429_exhausting_retries_is_failed_rate_limited(pat_settings, http_client, sleep):
    respx.post(url__startswith=WIQL).mock(return_value=wiql_result())
    create = respx.post(url__regex=CREATE_BUG).mock(return_value=httpx.Response(429, headers={"Retry-After": "1"}))
    outcome = await client(pat_settings, http_client, sleep, max_retries=2).create_work_item_idempotent(
        create_request()
    )
    assert outcome.status is CreateOutcomeStatus.FAILED
    assert outcome.error_category is FailureCategory.RATE_LIMITED
    assert outcome.retry_count == 2
    assert create.call_count == 3


@respx.mock
async def test_connect_error_is_retried_without_lookup(pat_settings, http_client, sleep):
    wiql = respx.post(url__startswith=WIQL).mock(return_value=wiql_result())
    create = respx.post(url__regex=CREATE_BUG).mock(side_effect=[httpx.ConnectError("refused"), created()])
    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())
    assert outcome.status is CreateOutcomeStatus.CREATED
    assert outcome.retry_count == 1
    assert create.call_count == 2
    assert wiql.call_count == 1


@respx.mock
async def test_read_timeout_then_lookup_finds_item_means_no_second_post(pat_settings, http_client, sleep):
    respx.post(url__startswith=WIQL).mock(side_effect=[wiql_result(), wiql_result(4242)])
    respx.get(url__startswith=WORK_ITEMS).mock(return_value=work_items(4242))
    create = respx.post(url__regex=CREATE_BUG).mock(side_effect=httpx.ReadTimeout("no response"))

    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())

    assert outcome.status is CreateOutcomeStatus.CREATED
    assert outcome.work_item is not None
    assert outcome.work_item.id == 4242
    assert outcome.response_summary["recovered_after_ambiguous_error"] is True
    assert create.call_count == 1


@respx.mock
async def test_read_timeout_then_lookup_absent_retries_and_creates(pat_settings, http_client, sleep):
    wiql = respx.post(url__startswith=WIQL).mock(side_effect=[wiql_result(), wiql_result()])
    create = respx.post(url__regex=CREATE_BUG).mock(side_effect=[httpx.ReadTimeout("no response"), created(9)])

    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())

    assert outcome.status is CreateOutcomeStatus.CREATED
    assert outcome.work_item is not None
    assert outcome.work_item.id == 9
    assert outcome.retry_count == 1
    assert create.call_count == 2
    assert wiql.call_count == 2


@respx.mock
async def test_read_timeout_then_lookup_failure_is_uncertain(pat_settings, http_client, sleep):
    responses = iter([wiql_result()])
    respx.post(url__startswith=WIQL).mock(side_effect=lambda request: next(responses, httpx.Response(500)))
    create = respx.post(url__regex=CREATE_BUG).mock(side_effect=httpx.ReadTimeout("no response"))

    outcome = await client(pat_settings, http_client, sleep, max_retries=2).create_work_item_idempotent(
        create_request()
    )

    assert outcome.status is CreateOutcomeStatus.UNCERTAIN
    assert outcome.error_category is FailureCategory.AMBIGUOUS
    assert outcome.work_item is None
    assert create.call_count == 1


@respx.mock
async def test_500_is_treated_as_ambiguous_and_checked_before_retry(pat_settings, http_client, sleep):
    wiql = respx.post(url__startswith=WIQL).mock(side_effect=[wiql_result(), wiql_result(31)])
    respx.get(url__startswith=WORK_ITEMS).mock(return_value=work_items(31))
    create = respx.post(url__regex=CREATE_BUG).mock(return_value=httpx.Response(500))
    outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(create_request())
    assert outcome.status is CreateOutcomeStatus.CREATED
    assert outcome.work_item is not None
    assert outcome.work_item.id == 31
    assert create.call_count == 1
    assert wiql.call_count == 2


@respx.mock
async def test_ambiguous_failures_exhausting_retries_are_failed_only_after_confirmed_absent(
    pat_settings, http_client, sleep
):
    wiql = respx.post(url__startswith=WIQL).mock(return_value=wiql_result())
    create = respx.post(url__regex=CREATE_BUG).mock(side_effect=httpx.ReadTimeout("slow"))
    outcome = await client(pat_settings, http_client, sleep, max_retries=1).create_work_item_idempotent(
        create_request()
    )
    assert outcome.status is CreateOutcomeStatus.FAILED
    assert outcome.error_category is FailureCategory.TIMEOUT
    assert create.call_count == 2
    assert wiql.call_count == 3  # pre-check + one lookup after each ambiguous attempt


@respx.mock
async def test_lookup_requires_exact_tag_match(pat_settings, http_client, sleep):
    respx.post(url__startswith=WIQL).mock(return_value=wiql_result(5))
    respx.get(url__startswith=WORK_ITEMS).mock(return_value=work_items(5, tags="nuvexa-0123456789abcdef0"))
    found = await client(pat_settings, http_client, sleep).find_by_idempotency_tag("NUVEXA-Demo", TAG)
    assert found is None


@respx.mock
async def test_lookup_reads_ids_title_and_rev(pat_settings, http_client, sleep):
    respx.post(url__startswith=WIQL).mock(return_value=wiql_result(12, 7))
    items = respx.get(url__startswith=WORK_ITEMS).mock(return_value=work_items(12, 7))
    found = await client(pat_settings, http_client, sleep).find_by_idempotency_tag("NUVEXA-Demo", TAG)
    assert found is not None
    assert (found.id, found.rev, found.title) == (7, 1, "Production login failures after deployment")
    params = items.calls.last.request.url.params
    assert params["ids"] == "7,12"
    assert "System.Tags" in params["fields"].split(",")


@respx.mock
async def test_wiql_escapes_single_quotes_and_path_is_encoded(pat_settings, http_client, sleep):
    route = respx.post(url__startswith=f"{ORG}/O%27Brien%20Labs/_apis/wit/wiql").mock(return_value=wiql_result())
    found = await client(pat_settings, http_client, sleep).find_by_idempotency_tag("O'Brien Labs", TAG)
    assert found is None
    query = json.loads(route.calls.last.request.content)["query"]
    assert "[System.TeamProject] = 'O''Brien Labs'" in query
    assert wiql_literal("a'b''c") == "'a''b''''c'"


@pytest.mark.parametrize("name", ["Demo/../other", "a?b=c", "a#b", "line\nbreak", "tab\there", "..", "", " padded"])
async def test_unsafe_project_names_are_rejected_before_any_request(pat_settings, http_client, sleep, name):
    with respx.mock:  # nothing mocked → any request would fail
        outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(
            create_request().model_copy(update={"project": name})
        )
        assert outcome.status is CreateOutcomeStatus.FAILED
        assert outcome.error_category is FailureCategory.VALIDATION
        with pytest.raises(IntegrationError):
            await client(pat_settings, http_client, sleep).find_by_idempotency_tag(name, TAG)
    with pytest.raises(IntegrationError):
        validate_name(name)


async def test_unsafe_type_names_and_bad_tags_are_rejected(pat_settings, http_client, sleep):
    with respx.mock:
        ado = client(pat_settings, http_client, sleep)
        outcome = await ado.create_work_item_idempotent(create_request().model_copy(update={"work_item_type": "Bug/x"}))
        assert outcome.status is CreateOutcomeStatus.FAILED
        bad_tag = create_request().model_copy(update={"idempotency_tag": "nuvexa-' OR 1=1"})
        assert (await ado.create_work_item_idempotent(bad_tag)).status is CreateOutcomeStatus.FAILED
        with pytest.raises(IntegrationError):
            await ado.find_by_idempotency_tag("NUVEXA-Demo", "anything")
        injected_field = create_request()
        injected_field.fields["System.Title/../relations"] = "x"
        assert (await ado.create_work_item_idempotent(injected_field)).status is CreateOutcomeStatus.FAILED


async def test_space_in_type_is_encoded_with_dollar_prefix(pat_settings, http_client, sleep):
    with respx.mock:
        respx.post(url__startswith=f"{PROJECT}/_apis/wit/wiql").mock(return_value=wiql_result())
        route = respx.post(url__startswith=f"{PROJECT}/_apis/wit/workitems/$User%20Story?").mock(return_value=created())
        outcome = await client(pat_settings, http_client, sleep).create_work_item_idempotent(
            create_request(work_item_type="User Story")
        )
    assert outcome.status is CreateOutcomeStatus.CREATED
    assert route.call_count == 1


@respx.mock
async def test_validate_work_item_uses_validate_only(pat_settings, http_client, sleep):
    route = respx.post(url__regex=CREATE_BUG).mock(
        side_effect=[
            httpx.Response(200, json={"id": 0, "rev": 0, "fields": {}}),
            httpx.Response(
                400,
                json={
                    "message": "TF401320: Rule Error for field Severity.",
                    "customProperties": {"FieldReferenceName": "Microsoft.VSTS.Common.Severity"},
                },
            ),
        ]
    )
    ado = client(pat_settings, http_client, sleep)
    ok = await ado.validate_work_item(create_request())
    assert ok.valid is True
    bad = await ado.validate_work_item(create_request())
    assert bad.valid is False
    assert bad.issues[0].field == "Microsoft.VSTS.Common.Severity"
    assert all(call.request.url.params["validateOnly"] == "true" for call in route.calls)


@respx.mock
async def test_list_projects_follows_continuation_tokens(pat_settings, http_client, sleep):
    route = respx.get(url__startswith=f"{ORG}/_apis/projects").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"x-ms-continuationtoken": "2"},
                json={"count": 2, "value": [{"id": "p1", "name": "Alpha"}, {"id": "p2", "name": "Beta Two"}]},
            ),
            httpx.Response(200, json={"count": 1, "value": [{"id": "p3", "name": "Gamma", "state": "wellFormed"}]}),
        ]
    )
    projects = await client(pat_settings, http_client, sleep).list_projects()
    assert [p.name for p in projects] == ["Alpha", "Beta Two", "Gamma"]
    assert projects[1].url == f"{ORG}/Beta%20Two"
    assert "continuationToken" not in route.calls[0].request.url.params
    assert route.calls[1].request.url.params["continuationToken"] == "2"


@respx.mock
async def test_list_work_item_types(pat_settings, http_client, sleep):
    respx.get(url__startswith=f"{PROJECT}/_apis/wit/workitemtypes").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 2,
                "value": [
                    {"name": "Bug", "referenceName": "Microsoft.VSTS.WorkItemTypes.Bug", "isDisabled": False},
                    {"name": "Old", "isDisabled": True, "description": "legacy\x00type"},
                ],
            },
        )
    )
    types = await client(pat_settings, http_client, sleep).list_work_item_types("NUVEXA-Demo")
    assert [(t.name, t.is_disabled) for t in types] == [("Bug", False), ("Old", True)]
    assert types[1].description == "legacy type"


@respx.mock
async def test_test_connection_reports_without_secrets(pat_settings, http_client, sleep):
    respx.get(url__startswith=f"{ORG}/_apis/projects").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"count": 0, "value": []})]
    )
    ado = client(pat_settings, http_client, sleep)
    failed = await ado.test_connection()
    assert failed.ok is False
    assert failed.mode == "pat"
    assert FAKE_PAT not in failed.message
    assert "Authentication failed" in failed.message
    ok = await ado.test_connection()
    assert ok.ok is True
    assert ok.organization == "contoso-test"


def test_client_requires_valid_configuration(database_url):
    with pytest.raises(IntegrationNotConfigured):
        AzureDevOpsRestClient(ado_settings(database_url, azure_devops_organization="bad/org"), auth=PatAuth(FAKE_PAT))
    with pytest.raises(IntegrationNotConfigured):
        PatAuth("")
    assert FAKE_PAT not in repr(PatAuth(FAKE_PAT))


async def test_bearer_auth_uses_delegated_scopes_and_memoises(token_provider):
    now = [100.0]
    auth = BearerTokenAuth(token_provider, cache_seconds=60, clock=lambda: now[0])
    assert await auth.authorization() == f"Bearer {token_provider.token}"
    await auth.authorization()
    assert token_provider.calls == [AZURE_DEVOPS_DELEGATED_SCOPES]
    now[0] += 61
    await auth.authorization()
    assert len(token_provider.calls) == 2
    assert token_provider.token not in repr(auth)


@respx.mock
async def test_entra_delegated_mode_sends_bearer_token(database_url, http_client, sleep, token_provider):
    settings = ado_settings(database_url, azure_devops_mode="entra_delegated", azure_devops_pat=None)
    respx.post(url__startswith=WIQL).mock(return_value=wiql_result())
    create = respx.post(url__regex=CREATE_BUG).mock(return_value=created())
    ado = AzureDevOpsRestClient(settings, auth=BearerTokenAuth(token_provider), http_client=http_client, sleep=sleep)
    assert ado.mode == "entra_delegated"
    outcome = await ado.create_work_item_idempotent(create_request())
    assert outcome.status is CreateOutcomeStatus.CREATED
    assert create.calls.last.request.headers["Authorization"] == f"Bearer {token_provider.token}"


async def test_owned_http_client_is_closed():
    settings_client = None
    async with AzureDevOpsRestClient(ado_settings("sqlite+aiosqlite:///:memory:"), auth=PatAuth(FAKE_PAT)) as ado:
        settings_client = ado._http()
    assert settings_client.is_closed
