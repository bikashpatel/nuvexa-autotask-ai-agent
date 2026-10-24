from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from app.core.errors import IntegrationError, IntegrationNotConfigured
from app.domain.enums import FailureCategory
from app.integrations.microsoft_graph.base import GRAPH_LOGIN_SCOPES, GRAPH_MAIL_SCOPES
from app.integrations.microsoft_graph.oauth import EntraOAuthClient, clean_scopes
from tests.conftest import make_settings
from tests.support.entra_tokens import msal_nonce_claim, sign_id_token, test_validator

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "00000000-0000-0000-0000-000000000001"
AUTHORITY_HOST = "https://login.microsoftonline.com"
OID = "22222222-2222-2222-2222-222222222222"
HOME = f"{OID}.{TENANT}"


def client_info(uid: str, utid: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"uid": uid, "utid": utid}).encode()).decode().rstrip("=")


class FakeCache:
    """Stand-in for msal.SerializableTokenCache (serialize/deserialize/has_state_changed)."""

    def __init__(self, state: str = "{}") -> None:
        self.state = state
        self.has_state_changed = False

    def serialize(self) -> str:
        self.has_state_changed = False
        return self.state

    def deserialize(self, state: str) -> None:
        self.state = state
        self.has_state_changed = False


class FakeApp:
    """Mimics the parts of msal.ConfidentialClientApplication used by EntraOAuthClient."""

    def __init__(self, cache: Any, script: dict[str, Any]) -> None:
        self.cache = cache
        self.script = script

    def initiate_auth_code_flow(self, scopes: list[str], **kwargs: Any) -> dict[str, Any]:
        self.script.setdefault("init_calls", []).append((scopes, kwargs))
        return {
            "state": "state-123",
            "redirect_uri": kwargs["redirect_uri"],
            "scope": scopes,
            "auth_uri": "https://login.example.invalid/authorize?state=state-123",
            "code_verifier": "verifier",
            "nonce": "nonce",
        }

    def acquire_token_by_auth_code_flow(
        self, auth_code_flow: dict[str, Any], auth_response: dict[str, str], **kwargs: Any
    ) -> dict[str, Any]:
        if auth_code_flow.get("state") != auth_response.get("state"):
            raise ValueError("state mismatch")
        if "raise" in self.script:
            raise self.script["raise"]
        self.cache.state = '{"RefreshToken": {"x": {"secret": "fake-refresh-token"}}}'
        self.cache.has_state_changed = True
        result: dict[str, Any] = self.script["token_result"]
        claims = result.get("id_token_claims")
        if isinstance(claims, dict) and "id_token" not in result:
            # Microsoft returns the raw, signed ID token; EntraOAuthClient verifies it before trusting claims.
            token = sign_id_token(
                claims,
                client_id=CLIENT_ID,
                authority_host=AUTHORITY_HOST,
                nonce=msal_nonce_claim(auth_code_flow.get("nonce")),
            )
            result = {**result, "id_token": token}
        return result

    def get_accounts(self, username: str | None = None) -> list[dict[str, Any]]:
        accounts: list[dict[str, Any]] = self.script.get("accounts", [])
        return accounts

    def acquire_token_silent_with_error(
        self, scopes: list[str], account: dict[str, Any] | None, **kwargs: Any
    ) -> dict[str, Any] | None:
        self.script.setdefault("silent_calls", []).append((scopes, account))
        silent: dict[str, Any] | None = self.script.get("silent_result")
        return silent


def make_client(database_url: str, script: dict[str, Any], **overrides: Any) -> EntraOAuthClient:
    settings = make_settings(
        database_url,
        entra_client_id=CLIENT_ID,
        entra_client_secret="not-a-real-secret",
        oauth_redirect_uri="http://localhost:5173/api/v1/auth/callback",
        **overrides,
    )
    return EntraOAuthClient(
        settings, app_factory=lambda cache: FakeApp(cache, script), id_token_validator=test_validator(settings)
    )


SUCCESS = {
    "access_token": "fake-access-token",
    "refresh_token": "fake-refresh-token",
    "client_info": client_info(OID, TENANT),
    "scope": "Mail.Read User.Read openid profile email",
    "id_token_claims": {
        "oid": OID,
        "tid": TENANT,
        "preferred_username": "Morgan.Member@Example.com",
        "name": "Morgan Member",
        "nonce": "hashed",
    },
}


async def test_begin_flow_passes_redirect_uri_and_strips_reserved_scopes(database_url):
    script: dict[str, Any] = {}
    oauth = make_client(database_url, script)
    flow = await oauth.begin_flow([*GRAPH_LOGIN_SCOPES, "openid", "offline_access", "User.Read"], login_hint="m@x.com")
    assert flow["state"] == "state-123"
    assert flow["auth_uri"].startswith("https://")
    scopes, kwargs = script["init_calls"][0]
    assert scopes == ["User.Read"]
    assert kwargs["redirect_uri"] == "http://localhost:5173/api/v1/auth/callback"
    assert kwargs["login_hint"] == "m@x.com"
    with pytest.raises(ValueError, match="prompt"):
        await oauth.begin_flow(GRAPH_MAIL_SCOPES, prompt="evil")


async def test_complete_flow_returns_identity_without_tokens(database_url):
    oauth = make_client(database_url, {"token_result": dict(SUCCESS)})
    flow = await oauth.begin_flow(GRAPH_MAIL_SCOPES)
    cache = FakeCache()
    result = await oauth.complete_flow(flow, {"state": flow["state"], "code": "auth-code"}, cache)
    assert result.oid == OID
    assert result.tid == TENANT
    assert result.home_account_id == HOME
    assert result.preferred_username == "Morgan.Member@Example.com"
    assert result.login_email == "morgan.member@example.com"
    assert result.name == "Morgan Member"
    assert "Mail.Read" in result.granted_scopes
    dumped = result.model_dump_json()
    assert "fake-access-token" not in dumped
    assert "fake-refresh-token" not in dumped
    assert cache.has_state_changed is True


async def test_state_mismatch_is_authorization_error_and_cache_untouched(database_url):
    oauth = make_client(database_url, {"token_result": dict(SUCCESS)})
    flow = await oauth.begin_flow(GRAPH_MAIL_SCOPES)
    cache = FakeCache("{}")
    with pytest.raises(IntegrationError) as info:
        await oauth.complete_flow(flow, {"state": "forged", "code": "auth-code"}, cache)
    assert info.value.category == FailureCategory.AUTHORIZATION
    assert cache.state == "{}"


async def test_nonce_failure_maps_to_authorization(database_url):
    oauth = make_client(database_url, {"raise": RuntimeError("nonce mismatch")})
    flow = await oauth.begin_flow(GRAPH_MAIL_SCOPES)
    with pytest.raises(IntegrationError) as info:
        await oauth.complete_flow(flow, {"state": flow["state"], "code": "c"}, FakeCache())
    assert info.value.category == FailureCategory.AUTHORIZATION


async def test_tenant_restriction_rejects_and_rolls_back_cache(database_url):
    oauth = make_client(
        database_url,
        {"token_result": dict(SUCCESS)},
        entra_allowed_tenant_ids="33333333-3333-3333-3333-333333333333",
    )
    flow = await oauth.begin_flow(GRAPH_MAIL_SCOPES)
    cache = FakeCache("{}")
    with pytest.raises(IntegrationError) as info:
        await oauth.complete_flow(flow, {"state": flow["state"], "code": "c"}, cache)
    assert info.value.category == FailureCategory.PERMISSION
    assert cache.state == "{}"
    assert "fake-refresh-token" not in cache.state


async def test_tenant_restriction_allows_listed_tenant(database_url):
    oauth = make_client(database_url, {"token_result": dict(SUCCESS)}, entra_allowed_tenant_ids=TENANT.upper())
    flow = await oauth.begin_flow(GRAPH_MAIL_SCOPES)
    result = await oauth.complete_flow(flow, {"state": flow["state"], "code": "c"}, FakeCache())
    assert result.tid == TENANT


@pytest.mark.parametrize(
    ("error", "category", "retryable"),
    [
        ("invalid_grant", FailureCategory.AUTHORIZATION, False),
        ("interaction_required", FailureCategory.AUTHORIZATION, False),
        ("consent_required", FailureCategory.AUTHORIZATION, False),
        ("access_denied", FailureCategory.AUTHORIZATION, False),
        ("invalid_client", FailureCategory.CONFIGURATION, False),
        ("temporarily_unavailable", FailureCategory.TRANSIENT, True),
    ],
)
async def test_msal_errors_are_mapped(database_url, error, category, retryable):
    result = {"error": error, "error_description": "AADSTS65001: secret detail", "error_codes": [65001]}
    oauth = make_client(database_url, {"token_result": result})
    flow = await oauth.begin_flow(GRAPH_MAIL_SCOPES)
    with pytest.raises(IntegrationError) as info:
        await oauth.complete_flow(flow, {"state": flow["state"], "code": "c"}, FakeCache())
    assert info.value.category == category
    assert info.value.retryable is retryable
    assert "secret detail" not in info.value.message
    assert "AADSTS65001" in info.value.message


async def test_missing_oid_claim_is_rejected(database_url):
    token_result = {**SUCCESS, "id_token_claims": {"tid": TENANT}}
    oauth = make_client(database_url, {"token_result": token_result})
    flow = await oauth.begin_flow(GRAPH_MAIL_SCOPES)
    with pytest.raises(IntegrationError) as info:
        await oauth.complete_flow(flow, {"state": flow["state"], "code": "c"}, FakeCache())
    assert info.value.category == FailureCategory.AUTHORIZATION


async def test_acquire_token_silent(database_url):
    account = {"home_account_id": HOME, "username": "morgan@example.com"}
    script: dict[str, Any] = {"accounts": [account], "silent_result": {"access_token": "fresh-token"}}
    oauth = make_client(database_url, script)
    token = await oauth.acquire_token_silent(GRAPH_MAIL_SCOPES, HOME, FakeCache())
    assert token == "fresh-token"
    assert script["silent_calls"] == [(["Mail.Read"], account)]


@pytest.mark.parametrize(
    "script",
    [
        {"accounts": []},  # account not in cache
        {"accounts": [{"home_account_id": HOME}], "silent_result": None},  # no refresh token
        {"accounts": [{"home_account_id": HOME}], "silent_result": {"error": "invalid_grant"}},
        {"accounts": [{"home_account_id": HOME}], "silent_result": {"token_type": "Bearer"}},
    ],
)
async def test_acquire_token_silent_requires_reauth(database_url, script):
    oauth = make_client(database_url, script)
    with pytest.raises(IntegrationError) as info:
        await oauth.acquire_token_silent(GRAPH_MAIL_SCOPES, HOME, FakeCache())
    assert info.value.category == FailureCategory.AUTHORIZATION


async def test_not_configured(database_url):
    oauth = EntraOAuthClient(make_settings(database_url))
    with pytest.raises(IntegrationNotConfigured):
        await oauth.begin_flow(GRAPH_MAIL_SCOPES)
    with pytest.raises(IntegrationNotConfigured):
        await oauth.acquire_token_silent(GRAPH_MAIL_SCOPES, HOME, FakeCache())


def test_authority_and_scope_cleaning(database_url):
    oauth = make_client(database_url, {}, entra_tenant_id=TENANT)
    assert oauth.authority == f"https://login.microsoftonline.com/{TENANT}"
    assert clean_scopes(["profile", " Mail.Read ", "Mail.Read", "", "email"]) == ["Mail.Read", "email"]
    bad = make_client(database_url, {}, entra_tenant_id="evil.com/../x")
    with pytest.raises(IntegrationNotConfigured):
        _ = bad.authority
