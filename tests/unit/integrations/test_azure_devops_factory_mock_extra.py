"""Azure DevOps factory refusals and the in-memory mock adapter's remaining behaviours."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.errors import IntegrationError, IntegrationNotConfigured
from app.domain.enums import FailureCategory
from app.integrations.azure_devops.base import CreateOutcomeStatus
from app.integrations.azure_devops.client import AzureDevOpsRestClient
from app.integrations.azure_devops.factory import build_azure_devops_client
from app.integrations.azure_devops.mapping import build_create_request
from app.integrations.azure_devops.mock import (
    MOCK_FAIL_MODES,
    MOCK_WORK_ITEM_TYPES,
    MockAzureDevOpsClient,
    mock_work_item_url,
)
from app.integrations.http import RetryPolicy
from app.schemas.actions import AzureDevOpsWorkItemPayload, SourceEmailReference
from tests.unit.integrations.conftest import SleepRecorder, ado_settings


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"azure_devops_organization": None}, "ORGANIZATION"),
        ({"azure_devops_organization": ""}, "ORGANIZATION"),
        ({"azure_devops_pat": None}, "PAT"),
        ({"azure_devops_pat": SecretStr("")}, "PAT"),
        ({"azure_devops_mode": "carrier-pigeon"}, "Unsupported AZURE_DEVOPS_MODE"),
    ],
)
def test_factory_refuses_incomplete_configuration(database_url, update: dict[str, Any], message: str) -> None:
    settings = ado_settings(database_url).model_copy(update=update)  # bypass the settings validator on purpose
    with pytest.raises(IntegrationNotConfigured, match=message):
        build_azure_devops_client(settings)


async def test_factory_passes_the_shared_http_client(database_url, http_client) -> None:
    ado = build_azure_devops_client(ado_settings(database_url), http_client=http_client)
    assert isinstance(ado, AzureDevOpsRestClient)
    assert ado._http() is http_client
    await ado.aclose()
    assert not http_client.is_closed


def request(key: str = "c" * 64, **overrides: Any):
    values: dict[str, Any] = {
        "project": "NUVEXA-Demo",
        "work_item_type": "Bug",
        "title": "Login failures",
        "priority": 2,
        "tags": ["Email"],
        "source_email": SourceEmailReference(email_id=uuid.uuid4(), subject="Subject"),
    }
    values.update(overrides)
    return build_create_request(AzureDevOpsWorkItemPayload(**values), idempotency_key=key)


async def test_mock_lookup_validates_tags_and_matches_case_insensitively() -> None:
    ado = MockAzureDevOpsClient()
    await ado.aclose()  # no-op, safe before use
    with pytest.raises(IntegrationError) as info:
        await ado.find_by_idempotency_tag("NUVEXA-Demo", "nuvexa-' OR 1=1")
    assert info.value.category == FailureCategory.VALIDATION
    created = await ado.create_work_item_idempotent(request())
    assert created.work_item is not None
    found = await ado.find_by_idempotency_tag("NUVEXA-Demo", request().idempotency_tag.upper().lower())
    assert found is not None
    assert found.id == created.work_item.id
    assert await ado.find_by_idempotency_tag("Customer-Portal", request().idempotency_tag) is None  # other project


async def test_mock_stores_are_isolated_per_organization_and_ids_increase() -> None:
    a, b = MockAzureDevOpsClient("org-a"), MockAzureDevOpsClient("org-b")
    first = await a.create_work_item_idempotent(request("1" * 64))
    second = await a.create_work_item_idempotent(request("2" * 64))
    other = await b.create_work_item_idempotent(request("1" * 64))
    assert [o.status for o in (first, second, other)] == [CreateOutcomeStatus.CREATED] * 3
    assert [o.work_item.id for o in (first, second, other) if o.work_item] == [1001, 1002, 1001]
    assert len(a.items()) == 2
    assert len(b.items()) == 1
    item = a.items()[0]
    assert item.ref().title == "Login failures"
    assert item.tags == ["Email", "nuvexa-1111111111111111"]
    assert item.state == "New"
    assert item.created_at.tzinfo is not None


async def test_mock_auth_error_mode_on_every_operation() -> None:
    ado = MockAzureDevOpsClient(fail_mode="auth_error")
    for call in (
        ado.list_projects(),
        ado.list_work_item_types("NUVEXA-Demo"),
        ado.validate_work_item(request()),
        ado.find_by_idempotency_tag("NUVEXA-Demo", request().idempotency_tag),
    ):
        with pytest.raises(IntegrationError) as info:
            await call
        assert info.value.category == FailureCategory.AUTHORIZATION
        assert info.value.status == 401
    attempt = await ado._attempt(request())  # the create attempt itself is also refused
    assert (attempt.kind, attempt.http_status, attempt.category) == ("permanent", 401, FailureCategory.AUTHORIZATION)
    assert ado.items() == []


async def test_transient_once_with_no_retries_fails_without_creating() -> None:
    sleep = SleepRecorder()
    ado = MockAzureDevOpsClient(fail_mode="transient_once", retry_policy=RetryPolicy(max_retries=0), sleep=sleep)
    outcome = await ado.create_work_item_idempotent(request())
    assert outcome.status is CreateOutcomeStatus.FAILED
    assert outcome.error_category is FailureCategory.TRANSIENT
    assert outcome.http_status == 503
    assert ado.items() == []
    assert sleep.calls == []
    retry = await MockAzureDevOpsClient(fail_mode="transient_once", sleep=sleep).create_work_item_idempotent(request())
    assert retry.status is CreateOutcomeStatus.CREATED
    assert len(sleep.calls) == 1


async def test_failure_modes_only_affect_the_first_attempt_of_an_instance() -> None:
    ado = MockAzureDevOpsClient(fail_mode="timeout_after_create_once")
    first = await ado.create_work_item_idempotent(request("a" * 64))
    second = await ado.create_work_item_idempotent(request("b" * 64))
    assert first.status is second.status is CreateOutcomeStatus.CREATED
    assert "recovered_after_ambiguous_error" not in second.response_summary
    assert ado.create_attempts == 2
    assert len(ado.items()) == 2


def test_mock_metadata() -> None:
    assert "none" in MOCK_FAIL_MODES
    assert len(MOCK_FAIL_MODES) == 6
    assert mock_work_item_url("Customer Portal/x", 5) == "mock://azure-devops/Customer%20Portal%2Fx/workitems/5"
    assert "Feature" in MOCK_WORK_ITEM_TYPES


async def test_mock_never_touches_the_network() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network call from the mock adapter")

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)):
        ado = MockAzureDevOpsClient()
        assert (await ado.create_work_item_idempotent(request())).status is CreateOutcomeStatus.CREATED
        assert (await ado.test_connection()).ok is True
