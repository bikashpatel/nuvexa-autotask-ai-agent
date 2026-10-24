from __future__ import annotations

import uuid

import pytest

from app.core.errors import IntegrationNotConfigured
from app.db.models import ConnectedAccount
from app.domain.enums import ConnectionProvider, ConnectionStatus
from app.integrations.azure_devops.client import AzureDevOpsRestClient
from app.integrations.azure_devops.factory import build_azure_devops_client
from app.integrations.azure_devops.mock import MockAzureDevOpsClient
from app.integrations.microsoft_graph.demo_mailbox import DemoMailboxProvider
from app.integrations.microsoft_graph.factory import build_mailbox_provider
from app.integrations.microsoft_graph.mail_client import GraphMailboxProvider
from tests.conftest import make_settings
from tests.unit.integrations.conftest import ado_settings


def account(provider: ConnectionProvider, status: ConnectionStatus = ConnectionStatus.ACTIVE) -> ConnectedAccount:
    return ConnectedAccount(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        provider=provider,
        external_account_id="x",
        status=status,
    )


async def test_mailbox_factory_selects_provider(session, settings, graph_settings):
    demo = build_mailbox_provider(account(ConnectionProvider.DEMO_MAILBOX), session=session, settings=settings)
    assert isinstance(demo, DemoMailboxProvider)
    graph = build_mailbox_provider(
        account(ConnectionProvider.MICROSOFT_GRAPH), session=session, settings=graph_settings
    )
    assert isinstance(graph, GraphMailboxProvider)


async def test_mailbox_factory_refusals(session, settings, database_url):
    with pytest.raises(IntegrationNotConfigured):  # MAIL_MODE=demo
        build_mailbox_provider(account(ConnectionProvider.MICROSOFT_GRAPH), session=session, settings=settings)
    no_entra = make_settings(database_url, mail_mode="graph")
    with pytest.raises(IntegrationNotConfigured):
        build_mailbox_provider(account(ConnectionProvider.MICROSOFT_GRAPH), session=session, settings=no_entra)
    no_demo = make_settings(database_url, demo_mode=False)
    with pytest.raises(IntegrationNotConfigured):
        build_mailbox_provider(account(ConnectionProvider.DEMO_MAILBOX), session=session, settings=no_demo)
    with pytest.raises(IntegrationNotConfigured):
        build_mailbox_provider(
            account(ConnectionProvider.DEMO_MAILBOX, ConnectionStatus.DISCONNECTED), session=session, settings=settings
        )
    with pytest.raises(IntegrationNotConfigured):
        build_mailbox_provider(account(ConnectionProvider.AZURE_DEVOPS), session=session, settings=settings)


async def test_azure_devops_factory_modes(settings, database_url, token_provider):
    assert isinstance(build_azure_devops_client(settings), MockAzureDevOpsClient)

    pat = build_azure_devops_client(ado_settings(database_url))
    assert isinstance(pat, AzureDevOpsRestClient)
    assert pat.mode == "pat"
    assert pat.organization == "contoso-test"
    await pat.aclose()

    delegated_settings = ado_settings(database_url, azure_devops_mode="entra_delegated", azure_devops_pat=None)
    with pytest.raises(IntegrationNotConfigured):
        build_azure_devops_client(delegated_settings)
    delegated = build_azure_devops_client(delegated_settings, token_provider=token_provider)
    assert delegated.mode == "entra_delegated"
    await delegated.aclose()
