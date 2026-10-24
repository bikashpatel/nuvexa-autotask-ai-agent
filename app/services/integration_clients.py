"""Builds integration clients for an actor (the only place credentials are bound to a user)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AzureDevOpsMode, Settings
from app.core.errors import IntegrationNotConfigured
from app.core.security import SecretBox
from app.db.models import ConnectedAccount
from app.domain.enums import ConnectionProvider, ConnectionStatus
from app.integrations.azure_devops.base import AzureDevOpsClient
from app.integrations.azure_devops.factory import build_azure_devops_client
from app.integrations.microsoft_graph.oauth import EntraOAuthClient
from app.integrations.microsoft_graph.token_store import CredentialStore, MsalAccessTokenProvider
from app.services.authz import Actor

AdoClientFactory = Callable[[AsyncSession, Actor], Awaitable[AzureDevOpsClient]]


async def find_active_account(
    session: AsyncSession, actor: Actor, provider: ConnectionProvider
) -> ConnectedAccount | None:
    stmt = (
        select(ConnectedAccount)
        .where(
            ConnectedAccount.tenant_id == actor.tenant_id,
            ConnectedAccount.user_id == actor.user_id,
            ConnectedAccount.provider == provider,
            ConnectedAccount.status == ConnectionStatus.ACTIVE,
        )
        .order_by(ConnectedAccount.updated_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def make_ado_client_factory(settings: Settings) -> AdoClientFactory:
    async def factory(session: AsyncSession, actor: Actor) -> AzureDevOpsClient:
        if settings.azure_devops_mode is not AzureDevOpsMode.ENTRA_DELEGATED:
            return build_azure_devops_client(settings)
        account = await find_active_account(session, actor, ConnectionProvider.AZURE_DEVOPS)
        if account is None:
            raise IntegrationNotConfigured("Connect your Azure DevOps account before executing work-item actions.")
        provider = MsalAccessTokenProvider(
            account, CredentialStore(session, SecretBox.from_settings(settings)), EntraOAuthClient(settings)
        )
        return build_azure_devops_client(settings, token_provider=provider)

    return factory
