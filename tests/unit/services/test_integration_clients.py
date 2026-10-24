from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import IntegrationNotConfigured
from app.core.timeutil import utcnow
from app.domain.enums import ConnectionProvider, ConnectionStatus
from app.integrations.azure_devops.client import AzureDevOpsRestClient
from app.integrations.azure_devops.mock import MockAzureDevOpsClient
from app.integrations.microsoft_graph.token_store import MsalAccessTokenProvider
from app.services.integration_clients import find_active_account, make_ado_client_factory
from tests.conftest import make_settings
from tests.unit.services.conftest import Factory, World

ADO = ConnectionProvider.AZURE_DEVOPS


async def test_find_active_account_filters_owner_tenant_provider_and_status(
    session: AsyncSession, factory: Factory, world: World
) -> None:
    actor = world.actor(world.owner)
    assert await find_active_account(session, actor, ADO) is None

    await factory.account(world.owner, ADO, status=ConnectionStatus.NEEDS_REAUTH)
    await factory.account(world.owner, ADO, status=ConnectionStatus.DISCONNECTED)
    await factory.account(world.other, ADO)  # someone else's
    assert await find_active_account(session, actor, ADO) is None

    mine = await factory.account(world.owner, ADO)
    found = await find_active_account(session, actor, ADO)
    assert found is not None
    assert found.id == mine.id
    mailbox = await find_active_account(session, actor, ConnectionProvider.DEMO_MAILBOX)
    assert mailbox is not None
    assert mailbox.id == world.account.id
    assert await find_active_account(session, actor, ConnectionProvider.MICROSOFT_GRAPH) is None


async def test_find_active_account_is_tenant_scoped(session: AsyncSession, factory: Factory, world: World) -> None:
    await factory.account(world.owner, ADO)
    forged = world.actor(world.owner)
    other_tenant = await factory.tenant()
    cross = type(forged).build(
        user_id=forged.user_id,
        tenant_id=other_tenant.id,
        role=forged.role,
        email=forged.email,
        display_name=forged.display_name,
    )
    assert await find_active_account(session, cross, ADO) is None


async def test_find_active_account_prefers_most_recently_updated(
    session: AsyncSession, factory: Factory, world: World
) -> None:
    now = utcnow()
    await factory.account(world.owner, ADO, updated_at=now - timedelta(days=2))
    newest = await factory.account(world.owner, ADO, updated_at=now)
    await factory.account(world.owner, ADO, updated_at=now - timedelta(days=1))
    found = await find_active_account(session, world.actor(world.owner), ADO)
    assert found is not None
    assert found.id == newest.id


async def test_mock_mode_builds_mock_client_without_any_account(
    session: AsyncSession, settings: Settings, world: World
) -> None:
    client = await make_ado_client_factory(settings)(session, world.actor(world.viewer))
    try:
        assert isinstance(client, MockAzureDevOpsClient)
        assert client.mode == "mock"
    finally:
        await client.aclose()


async def test_pat_mode_builds_rest_client_with_service_identity(
    session: AsyncSession, database_url: str, world: World
) -> None:
    pat = make_settings(
        database_url,
        azure_devops_mode="pat",
        azure_devops_organization="contoso-test",
        azure_devops_pat="fake-pat-for-unit-tests-only",
    )
    client = await make_ado_client_factory(pat)(session, world.actor(world.owner))
    try:
        assert isinstance(client, AzureDevOpsRestClient)
        assert client.mode == "pat"
        assert client.organization == "contoso-test"
    finally:
        await client.aclose()


def _entra_settings(database_url: str) -> Settings:
    return make_settings(
        database_url,
        azure_devops_mode="entra_delegated",
        azure_devops_organization="contoso-test",
        entra_client_id="00000000-0000-0000-0000-000000000001",
        entra_client_secret="not-a-real-secret",
    )


async def test_entra_delegated_requires_the_actors_connected_account(
    session: AsyncSession, factory: Factory, database_url: str, world: World
) -> None:
    await factory.account(world.other, ADO)  # another user's connection must not be borrowed
    await factory.account(world.owner, ADO, status=ConnectionStatus.NEEDS_REAUTH)
    with pytest.raises(IntegrationNotConfigured) as info:
        await make_ado_client_factory(_entra_settings(database_url))(session, world.actor(world.owner))
    assert "Connect your Azure DevOps account" in info.value.message


async def test_entra_delegated_binds_the_actors_credentials(
    session: AsyncSession, factory: Factory, database_url: str, world: World
) -> None:
    approver_account = await factory.account(world.approver, ADO, external_account_id="uid.utid")
    client = await make_ado_client_factory(_entra_settings(database_url))(session, world.actor(world.approver))
    try:
        assert isinstance(client, AzureDevOpsRestClient)
        assert client.mode == "entra_delegated"
        provider = client._auth._provider  # type: ignore[union-attr]
        assert isinstance(provider, MsalAccessTokenProvider)
        assert provider._account.id == approver_account.id
        assert provider._account.user_id == world.approver.id
    finally:
        await client.aclose()


async def test_factory_does_not_authorize_by_itself(session: AsyncSession, settings: Settings, world: World) -> None:
    # Building a client is not an authorization decision: ExecutionService performs every check.
    client = await make_ado_client_factory(settings)(session, world.actor(world.viewer))
    assert isinstance(client, MockAzureDevOpsClient)
