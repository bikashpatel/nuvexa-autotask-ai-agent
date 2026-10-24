"""Test doubles for integration tests (no network): a fake Microsoft Entra ID and a scripted LLM agent.

These are fakes of *third-party* boundaries only. The application's own classes (EntraOAuthClient,
CredentialStore, MsalAccessTokenProvider, GraphMailboxProvider, AzureDevOpsRestClient, …) run for real.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import msal
import pytest

from app.agents.base import ClassificationResult, ExtractionResult, PlanningResult
from app.agents.heuristic import HeuristicAnalysisAgent
from app.core.config import Settings
from app.integrations.microsoft_graph.oauth import EntraOAuthClient
from app.schemas.ai import ClassificationOutput, ExtractionOutput, PlanningContext
from app.schemas.email import EmailContext
from app.schemas.settings import TaxonomyConfig
from tests.support.entra_tokens import msal_nonce_claim, sign_id_token, test_validator

FAKE_TENANT_ID = "11111111-2222-3333-4444-555555555555"
FAKE_OBJECT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FAKE_AUTHORIZE_HOST = "https://login.fake-entra.test"
_RESERVED = ("openid", "profile", "offline_access")


def _b64url(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


@dataclass
class FakeIdentity:
    oid: str = FAKE_OBJECT_ID
    tid: str = FAKE_TENANT_ID
    email: str = "casey.entra@contoso.example"
    name: str = "Casey Entra"

    def claims(self) -> dict[str, Any]:
        return {
            "oid": self.oid,
            "tid": self.tid,
            "preferred_username": self.email,
            "email": self.email,
            "name": self.name,
        }

    @property
    def home_account_id(self) -> str:
        return f"{self.oid}.{self.tid}"


@dataclass
class FakeEntra:
    """Stands in for Microsoft Entra ID behind `msal.ConfidentialClientApplication`.

    Behaves like MSAL where the application relies on it: the auth-code flow dict carries `state`,
    redemption rejects a mismatching `state` with ValueError, redeemed tokens land in the caller's
    `SerializableTokenCache`, and silent acquisition reads that cache.
    """

    identity: FakeIdentity = field(default_factory=FakeIdentity)
    access_token: str = "fake-access-token-0001"
    redeem_error: str | None = None
    silent_error: str | None = None
    refresh_on_silent: bool = False
    flows_started: list[dict[str, Any]] = field(default_factory=list)
    redeemed: list[dict[str, Any]] = field(default_factory=list)
    silent_calls: list[dict[str, Any]] = field(default_factory=list)
    # Audience/issuer host the fake signs ID tokens for (taken from the settings in `oauth_client`).
    client_id: str = ""
    authority_host: str = "https://login.microsoftonline.com"

    def app_factory(self, cache: msal.SerializableTokenCache | None) -> _FakeMsalApp:
        return _FakeMsalApp(self, cache)

    def oauth_client(self, settings: Settings) -> EntraOAuthClient:
        self.client_id = settings.entra_client_id or ""
        self.authority_host = settings.entra_authority_host
        return EntraOAuthClient(settings, app_factory=self.app_factory, id_token_validator=test_validator(settings))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeEntra:
        """Inject the fake MSAL app wherever the application constructs `EntraOAuthClient`."""
        build: Callable[[Settings], EntraOAuthClient] = self.oauth_client
        for target in (
            "app.services.auth_service.EntraOAuthClient",
            "app.integrations.microsoft_graph.factory.EntraOAuthClient",
            "app.services.integration_clients.EntraOAuthClient",
        ):
            monkeypatch.setattr(target, build)
        return self


class _FakeMsalApp:
    def __init__(self, entra: FakeEntra, cache: msal.SerializableTokenCache | None) -> None:
        self._entra = entra
        self._cache = cache

    def initiate_auth_code_flow(self, scopes: list[str], **kwargs: Any) -> dict[str, Any]:
        for reserved in _RESERVED:  # real MSAL raises ValueError for these
            if reserved in scopes:
                raise ValueError(f"{reserved} is reserved")
        state = secrets.token_urlsafe(16)
        full_scopes = [*scopes, *_RESERVED]
        query = urlencode({"client_id": "fake-client-id", "state": state, "scope": " ".join(full_scopes)})
        flow = {
            "state": state,
            "redirect_uri": kwargs.get("redirect_uri"),
            "scope": full_scopes,
            "auth_uri": f"{FAKE_AUTHORIZE_HOST}/organizations/oauth2/v2.0/authorize?{query}",
            "code_verifier": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(8),
            "claims_challenge": None,
        }
        self._entra.flows_started.append(
            {"scopes": list(scopes), "login_hint": kwargs.get("login_hint"), "state": state, **kwargs}
        )
        return flow

    def acquire_token_by_auth_code_flow(
        self, auth_code_flow: dict[str, Any], auth_response: dict[str, str], **kwargs: Any
    ) -> dict[str, Any]:
        if auth_response.get("state") != auth_code_flow.get("state"):
            raise ValueError("state mismatch")  # what MSAL does
        if "error" in auth_response:
            return {"error": auth_response["error"]}
        if self._entra.redeem_error:
            return {
                "error": self._entra.redeem_error,
                "error_codes": [70008],
                "error_description": "AADSTS70008: secret description that must not leak",
            }
        ident = self._entra.identity
        scopes = [s for s in auth_code_flow.get("scope", []) if s not in _RESERVED]
        response = {
            "access_token": self._entra.access_token,
            "refresh_token": "fake-refresh-token-0001",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": " ".join(scopes),
            "client_info": _b64url({"uid": ident.oid, "utid": ident.tid}),
            "id_token": sign_id_token(
                ident.claims(),
                client_id=self._entra.client_id,
                authority_host=self._entra.authority_host,
                nonce=msal_nonce_claim(auth_code_flow.get("nonce")),
            ),
            "id_token_claims": ident.claims(),
        }
        self._entra.redeemed.append({"code": auth_response.get("code"), "scopes": scopes})
        if self._cache is not None:
            self._cache.add(
                {
                    "client_id": "fake-client-id",
                    "scope": scopes,
                    "token_endpoint": f"https://login.microsoftonline.com/{ident.tid}/oauth2/v2.0/token",
                    "response": dict(response),
                    "grant_type": "authorization_code",
                }
            )
        return response

    def get_accounts(self, username: str | None = None) -> list[dict[str, Any]]:
        if self._cache is None:
            return []
        return list(self._cache.search(msal.TokenCache.CredentialType.ACCOUNT))

    def acquire_token_silent_with_error(
        self, scopes: list[str], account: dict[str, Any] | None, **kwargs: Any
    ) -> dict[str, Any] | None:
        self._entra.silent_calls.append({"scopes": list(scopes), "account": (account or {}).get("home_account_id")})
        if self._entra.silent_error:
            return {"error": self._entra.silent_error, "error_codes": [50173]}
        if self._cache is None or account is None:
            return None
        tokens = list(
            self._cache.search(
                msal.TokenCache.CredentialType.ACCESS_TOKEN, query={"home_account_id": account["home_account_id"]}
            )
        )
        if not tokens:
            return None
        if self._entra.refresh_on_silent:  # simulate a refresh-token redemption that rotates the cache
            ident = self._entra.identity
            self._cache.add(
                {
                    "client_id": "fake-client-id",
                    "scope": list(scopes),
                    "token_endpoint": f"https://login.microsoftonline.com/{ident.tid}/oauth2/v2.0/token",
                    "response": {
                        "access_token": f"{self._entra.access_token}-refreshed",
                        "expires_in": 3600,
                        "client_info": _b64url({"uid": ident.oid, "utid": ident.tid}),
                        "id_token_claims": ident.claims(),
                    },
                    "grant_type": "refresh_token",
                }
            )
            return {"access_token": f"{self._entra.access_token}-refreshed", "token_type": "Bearer"}
        return {"access_token": tokens[0]["secret"], "token_type": "Bearer", "expires_in": 3600}


class ScriptedAgent:
    """The deterministic heuristic agent with scripted failures per step (e.g. a flaky LLM provider).

    `failures["classify"] = [AgentProviderError(...), None]` raises on the first classify call only.
    """

    def __init__(self, failures: dict[str, list[BaseException | None]] | None = None) -> None:
        self.inner = HeuristicAnalysisAgent()
        self.provider_name = self.inner.provider_name
        self.model_name = self.inner.model_name
        self.prompt_version = self.inner.prompt_version
        self.failures = failures or {}
        self.calls: dict[str, int] = {"classify": 0, "extract": 0, "plan": 0}

    def _step(self, name: str) -> None:
        self.calls[name] += 1
        script = self.failures.get(name)
        if script:
            failure = script.pop(0)
            if failure is not None:
                raise failure

    async def classify(self, email: EmailContext, taxonomy: TaxonomyConfig) -> ClassificationResult:
        self._step("classify")
        return await self.inner.classify(email, taxonomy)

    async def extract(self, email: EmailContext, classification: ClassificationOutput) -> ExtractionResult:
        self._step("extract")
        return await self.inner.extract(email, classification)

    async def plan(
        self,
        email: EmailContext,
        classification: ClassificationOutput,
        extraction: ExtractionOutput,
        context: PlanningContext,
    ) -> PlanningResult:
        self._step("plan")
        return await self.inner.plan(email, classification, extraction, context)
