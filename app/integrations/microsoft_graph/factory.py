"""Builds the `MailboxProvider` for a connected account."""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import MailMode, Settings
from app.core.errors import IntegrationNotConfigured
from app.core.security import SecretBox
from app.db.models import ConnectedAccount
from app.domain.enums import ConnectionProvider, ConnectionStatus
from app.integrations.microsoft_graph.base import MailboxProvider
from app.integrations.microsoft_graph.demo_mailbox import DemoMailboxProvider
from app.integrations.microsoft_graph.mail_client import GraphMailboxProvider
from app.integrations.microsoft_graph.oauth import EntraOAuthClient
from app.integrations.microsoft_graph.token_store import CredentialStore, MsalAccessTokenProvider


def build_mailbox_provider(
    account: ConnectedAccount,
    *,
    session: AsyncSession,
    settings: Settings,
    http_client: httpx.AsyncClient | None = None,
) -> MailboxProvider:
    """Demo accounts → `DemoMailboxProvider` (DEMO_MODE only); Microsoft accounts → Graph delta client.

    Token refreshes are written through `session` (flushed, committed by the caller).
    """
    if account.status is ConnectionStatus.DISCONNECTED:
        raise IntegrationNotConfigured("The mailbox connection has been disconnected")
    if account.provider is ConnectionProvider.DEMO_MAILBOX:
        if not settings.demo_mode:
            raise IntegrationNotConfigured("The demo mailbox is only available when DEMO_MODE=true")
        return DemoMailboxProvider()
    if account.provider is ConnectionProvider.MICROSOFT_GRAPH:
        if settings.mail_mode is not MailMode.GRAPH:
            raise IntegrationNotConfigured("Microsoft Graph mail sync is disabled (MAIL_MODE=demo)")
        if not settings.entra_configured:
            raise IntegrationNotConfigured("Microsoft Entra ID is not configured")
        store = CredentialStore(session, SecretBox.from_settings(settings))
        token_provider = MsalAccessTokenProvider(account, store, EntraOAuthClient(settings))
        return GraphMailboxProvider(token_provider, settings, http_client=http_client)
    raise IntegrationNotConfigured(f"Unsupported mailbox provider: {account.provider}")
