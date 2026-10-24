from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.errors import IntegrationError
from app.domain.enums import FailureCategory
from app.integrations.azure_devops.base import CreateOutcomeStatus
from app.integrations.azure_devops.mapping import build_create_request
from app.integrations.azure_devops.mock import MOCK_PROJECTS, MockAzureDevOpsClient
from app.schemas.actions import AzureDevOpsWorkItemPayload, SourceEmailReference


def request(key: str = "a" * 64, **overrides):
    values: dict[str, Any] = {
        "project": "NUVEXA-Demo",
        "work_item_type": "Bug",
        "title": "Login failures",
        "priority": 1,
        "source_email": SourceEmailReference(email_id=uuid.uuid4(), subject="URGENT"),
    }
    values.update(overrides)
    return build_create_request(AzureDevOpsWorkItemPayload(**values), idempotency_key=key)


async def test_create_is_idempotent_and_urls_are_marked_mock():
    ado = MockAzureDevOpsClient()
    first = await ado.create_work_item_idempotent(request())
    assert first.status is CreateOutcomeStatus.CREATED
    assert first.work_item is not None
    assert first.work_item.id == 1001
    assert first.work_item.url == "mock://azure-devops/NUVEXA-Demo/workitems/1001"
    assert first.response_summary["mock"] is True

    again = await MockAzureDevOpsClient().create_work_item_idempotent(request())  # new instance, same store
    assert again.status is CreateOutcomeStatus.EXISTING
    assert again.work_item is not None
    assert again.work_item.id == 1001

    other = await ado.create_work_item_idempotent(request(key="b" * 64, project="Customer-Portal"))
    assert other.work_item is not None
    assert other.work_item.id == 1002
    assert len(ado.items()) == 2
    stored = ado.items("NUVEXA-Demo")[0]
    assert "nuvexa-aaaaaaaaaaaaaaaa" in stored.tags
    assert stored.fields["System.Title"] == "Login failures"


async def test_transient_once_retries_then_succeeds():
    ado = MockAzureDevOpsClient(fail_mode="transient_once")
    outcome = await ado.create_work_item_idempotent(request())
    assert outcome.status is CreateOutcomeStatus.CREATED
    assert outcome.retry_count == 1
    assert ado.create_attempts == 2
    assert len(ado.items()) == 1


async def test_timeout_after_create_is_recovered_without_duplicate():
    ado = MockAzureDevOpsClient(fail_mode="timeout_after_create_once")
    outcome = await ado.create_work_item_idempotent(request())
    assert outcome.status is CreateOutcomeStatus.CREATED
    assert outcome.response_summary["recovered_after_ambiguous_error"] is True
    assert ado.create_attempts == 1
    assert len(ado.items()) == 1


async def test_lookup_unavailable_after_timeout_is_uncertain_then_reconcilable():
    ado = MockAzureDevOpsClient(fail_mode="lookup_unavailable_after_timeout")
    outcome = await ado.create_work_item_idempotent(request())
    assert outcome.status is CreateOutcomeStatus.UNCERTAIN
    assert outcome.error_category is FailureCategory.AMBIGUOUS
    assert len(ado.items()) == 1  # it was in fact created
    reconciled = await MockAzureDevOpsClient().find_by_idempotency_tag("NUVEXA-Demo", request().idempotency_tag)
    assert reconciled is not None
    assert reconciled.id == outcome_id(ado)


def outcome_id(ado: MockAzureDevOpsClient) -> int:
    return ado.items()[0].id


async def test_permanent_validation_creates_nothing():
    ado = MockAzureDevOpsClient(fail_mode="permanent_validation")
    outcome = await ado.create_work_item_idempotent(request())
    assert outcome.status is CreateOutcomeStatus.FAILED
    assert outcome.error_category is FailureCategory.VALIDATION
    assert outcome.retry_count == 0
    assert ado.items() == []


async def test_auth_error_mode():
    ado = MockAzureDevOpsClient(fail_mode="auth_error")
    outcome = await ado.create_work_item_idempotent(request())
    assert outcome.status is CreateOutcomeStatus.FAILED
    assert outcome.error_category is FailureCategory.AUTHORIZATION
    check = await ado.test_connection()
    assert check.ok is False
    with pytest.raises(IntegrationError):
        await ado.list_projects()
    assert ado.items() == []


async def test_validate_checks_project_type_and_title():
    ado = MockAzureDevOpsClient()
    assert (await ado.validate_work_item(request())).valid is True
    bad = request().model_copy(update={"project": "Unknown", "work_item_type": "Spaceship"})
    bad.fields["System.Title"] = "  "
    result = await ado.validate_work_item(bad)
    assert result.valid is False
    assert {i.field for i in result.issues} >= {"System.TeamProject", "System.WorkItemType", "System.Title"}
    failed = await ado.create_work_item_idempotent(bad)
    assert failed.status is CreateOutcomeStatus.FAILED
    assert ado.items() == []


async def test_metadata_and_reset():
    ado = MockAzureDevOpsClient()
    assert ado.mode == "mock"
    assert ado.organization == "nuvexa-demo-org"
    assert [p.name for p in await ado.list_projects()] == list(MOCK_PROJECTS)
    assert "User Story" in [t.name for t in await ado.list_work_item_types("Customer-Portal")]
    with pytest.raises(IntegrationError):
        await ado.list_work_item_types("Nope")
    assert (await ado.test_connection()).ok is True
    await ado.create_work_item_idempotent(request())
    MockAzureDevOpsClient.reset()
    assert ado.items() == []
    with pytest.raises(ValueError, match="fail_mode"):
        MockAzureDevOpsClient(fail_mode="explode")  # type: ignore[arg-type]


async def test_engine_treats_unexpected_attempt_errors_as_ambiguous():
    from app.integrations.azure_devops.base import WorkItemRef
    from app.integrations.azure_devops.idempotency import run_idempotent_create
    from app.integrations.http import RetryPolicy, no_sleep

    lookups: list[int] = []
    ref = WorkItemRef(id=5, url="mock://azure-devops/NUVEXA-Demo/workitems/5")

    async def lookup() -> WorkItemRef | None:
        lookups.append(1)
        return None if len(lookups) == 1 else ref

    async def attempt():
        raise RuntimeError("bug after the request was sent")

    outcome = await run_idempotent_create(lookup=lookup, attempt=attempt, policy=RetryPolicy(), sleep=no_sleep)
    assert outcome.status is CreateOutcomeStatus.CREATED
    assert outcome.work_item == ref
    assert len(lookups) == 2

    async def broken_lookup() -> WorkItemRef | None:
        if lookups:
            lookups.clear()
            return None
        raise KeyError("boom")

    lookups.append(1)
    outcome = await run_idempotent_create(lookup=broken_lookup, attempt=attempt, policy=RetryPolicy(), sleep=no_sleep)
    assert outcome.status is CreateOutcomeStatus.UNCERTAIN
