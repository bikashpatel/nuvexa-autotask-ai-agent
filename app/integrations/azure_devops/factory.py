"""Builds the configured `AzureDevOpsClient` (mock / PAT / Entra delegated)."""

from __future__ import annotations

import httpx

from app.core.config import AzureDevOpsMode, Settings
from app.core.errors import IntegrationNotConfigured
from app.integrations.azure_devops.base import AzureDevOpsClient
from app.integrations.azure_devops.client import AzureDevOpsRestClient, BearerTokenAuth, PatAuth
from app.integrations.azure_devops.mock import MockAzureDevOpsClient
from app.integrations.microsoft_graph.base import AccessTokenProvider


def build_azure_devops_client(
    settings: Settings,
    *,
    token_provider: AccessTokenProvider | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> AzureDevOpsClient:
    """`AZURE_DEVOPS_MODE=entra_delegated` requires `token_provider` (the approving user's Microsoft account,
    e.g. `MsalAccessTokenProvider`). The caller closes the client with `aclose()`."""
    mode = settings.azure_devops_mode
    if mode is AzureDevOpsMode.MOCK:
        return MockAzureDevOpsClient()
    if not settings.azure_devops_organization:
        raise IntegrationNotConfigured("AZURE_DEVOPS_ORGANIZATION is not configured")
    if mode is AzureDevOpsMode.PAT:
        if settings.azure_devops_pat is None or not settings.azure_devops_pat.get_secret_value():
            raise IntegrationNotConfigured("AZURE_DEVOPS_PAT is not configured")
        return AzureDevOpsRestClient(settings, auth=PatAuth(settings.azure_devops_pat), http_client=http_client)
    if mode is AzureDevOpsMode.ENTRA_DELEGATED:
        if token_provider is None:
            raise IntegrationNotConfigured("Azure DevOps (Entra delegated) needs a connected Microsoft account")
        return AzureDevOpsRestClient(settings, auth=BearerTokenAuth(token_provider), http_client=http_client)
    raise IntegrationNotConfigured(f"Unsupported AZURE_DEVOPS_MODE: {mode}")
