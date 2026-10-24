"""Fixtures for integration adapter tests. No network: every HTTP call is served by respx."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.integrations.azure_devops.mock import MockAzureDevOpsClient
from tests.conftest import make_settings

FAKE_PAT = "fake-pat-for-unit-tests-only"
FAKE_ACCESS_TOKEN = "fake-access-token-for-unit-tests"


class SleepRecorder:
    """Drop-in for asyncio.sleep that records requested delays and returns immediately."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class StaticTokenProvider:
    def __init__(self, token: str = FAKE_ACCESS_TOKEN) -> None:
        self.token = token
        self.calls: list[tuple[str, ...]] = []

    async def get_access_token(self, scopes: tuple[str, ...]) -> str:
        self.calls.append(tuple(scopes))
        return self.token


@pytest.fixture
def sleep() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def token_provider() -> StaticTokenProvider:
    return StaticTokenProvider()


@pytest.fixture
def graph_settings(database_url: str) -> Settings:
    return make_settings(
        database_url,
        mail_mode="graph",
        entra_client_id="00000000-0000-0000-0000-000000000001",
        entra_client_secret="not-a-real-secret",
        entra_tenant_id="organizations",
    )


def ado_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "azure_devops_mode": "pat",
        "azure_devops_organization": "contoso-test",
        "azure_devops_pat": FAKE_PAT,
        "azure_devops_max_retries": 3,
    }
    values.update(overrides)
    return make_settings(database_url, **values)


@pytest.fixture
def pat_settings(database_url: str) -> Settings:
    return ado_settings(database_url)


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture(autouse=True)
def _reset_mock_ado() -> Iterator[None]:
    MockAzureDevOpsClient.reset()
    yield
    MockAzureDevOpsClient.reset()
