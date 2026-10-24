"""Entra OAuth facade: MSAL error mapping table, claim/scope parsing, app construction and failure paths."""

from __future__ import annotations

import base64
import json
from typing import Any

import msal
import pytest

from app.core.errors import IntegrationError
from app.domain.enums import FailureCategory
from app.integrations.microsoft_graph import oauth as oauth_module
from app.integrations.microsoft_graph.base import GRAPH_MAIL_SCOPES
from app.integrations.microsoft_graph.oauth import (
    AuthResult,
    EntraOAuthClient,
    _granted_scopes,
    _home_account_id,
    map_msal_error,
    new_token_cache,
)
from tests.conftest import make_settings
from tests.unit.integrations.test_entra_oauth import HOME, OID, SUCCESS, TENANT, FakeApp, FakeCache, make_client


@pytest.mark.parametrize(
    ("error", "category", "retryable", "label"),
    [
        ("invalid_grant", FailureCategory.AUTHORIZATION, False, "re-authentication is required (invalid_grant)"),
        ("interaction_required", FailureCategory.AUTHORIZATION, False, "(interaction_required)"),
        ("consent_required", FailureCategory.AUTHORIZATION, False, "(consent_required)"),
        ("login_required", FailureCategory.AUTHORIZATION, False, "(login_required)"),
        ("access_denied", FailureCategory.AUTHORIZATION, False, "(access_denied)"),
        ("invalid_scope", FailureCategory.AUTHORIZATION, False, "(invalid_scope)"),
        ("unauthorized_client", FailureCategory.AUTHORIZATION, False, "(unauthorized_client)"),
        ("invalid_request", FailureCategory.AUTHORIZATION, False, "(invalid_request)"),
        ("invalid_client", FailureCategory.CONFIGURATION, False, "misconfigured (invalid_client)"),
        ("unsupported_response_type", FailureCategory.CONFIGURATION, False, "(unsupported_response_type)"),
        ("invalid_resource", FailureCategory.CONFIGURATION, False, "(invalid_resource)"),
        ("temporarily_unavailable", FailureCategory.TRANSIENT, True, "temporarily unavailable"),
        ("server_error", FailureCategory.TRANSIENT, True, "(server_error)"),
        ("some_new_error", FailureCategory.AUTHORIZATION, False, "(some_new_error)"),
        ("Bad-Code!<script>", FailureCategory.AUTHORIZATION, False, "(unknown_error)"),  # never echoed
        (None, FailureCategory.AUTHORIZATION, False, "(unknown_error)"),
        (42, FailureCategory.AUTHORIZATION, False, "(unknown_error)"),
    ],
)
def test_msal_error_mapping_table(error: Any, category: FailureCategory, retryable: bool, label: str) -> None:
    mapped = map_msal_error(
        {"error": error, "error_description": "AADSTS secret detail"}, action="Token refresh failed"
    )
    assert mapped.category == category
    assert mapped.retryable is retryable
    assert mapped.message.startswith("Token refresh failed: ")
    assert label in mapped.message
    assert "secret detail" not in mapped.message
    assert "<script>" not in mapped.message


@pytest.mark.parametrize(
    ("codes", "suffix"),
    [
        ([50076], " (AADSTS50076)"),
        ([1, 2, 3, 4], " (AADSTS1, AADSTS2, AADSTS3)"),
        (["50076", None, 7000215], " (AADSTS7000215)"),
        ([], ""),
        (None, ""),
        # Regression: non-list values used to raise TypeError ("int object is not iterable").
        (50076, ""),
        ("50076", ""),
        ({"code": 50076}, ""),
    ],
)
def test_aadsts_codes_are_listed_safely(codes: Any, suffix: str) -> None:
    mapped = map_msal_error({"error": "invalid_grant", "error_codes": codes}, action="Sign-in failed")
    assert mapped.message == f"Sign-in failed: re-authentication is required (invalid_grant){suffix}"


def _client_info(payload: Any) -> str:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.mark.parametrize(
    ("client_info", "expected"),
    [
        (_client_info({"uid": "u1", "utid": "t1"}), "u1.t1"),
        (_client_info({"uid": "u1"}), f"{OID}.{TENANT}"),
        (_client_info({"uid": 1, "utid": "t1"}), f"{OID}.{TENANT}"),
        (_client_info(["u1", "t1"]), f"{OID}.{TENANT}"),
        (_client_info(b"\xff\xfe not utf-8"), f"{OID}.{TENANT}"),
        (_client_info(b"not json"), f"{OID}.{TENANT}"),
        ("!!!not-base64!!!", f"{OID}.{TENANT}"),
        ("", f"{OID}.{TENANT}"),
        (None, f"{OID}.{TENANT}"),
    ],
)
def test_home_account_id_falls_back_to_oid_and_tid(client_info: Any, expected: str) -> None:
    assert _home_account_id({"client_info": client_info}, OID, TENANT) == expected


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("Mail.Read  User.Read", ["Mail.Read", "User.Read"]),
        (["Mail.Read", 5, None, "openid"], ["Mail.Read", "openid"]),
        (None, []),
        (42, []),
    ],
)
def test_granted_scopes(scope: Any, expected: list[str]) -> None:
    assert _granted_scopes({"scope": scope}) == expected


@pytest.mark.parametrize(
    ("email", "username", "expected"),
    [
        ("Morgan@Example.com", "other@example.com", "morgan@example.com"),
        (None, "Morgan@Example.com", "morgan@example.com"),
        (None, "morgan", None),  # not e-mail-like
        (None, None, None),
    ],
)
def test_login_email(email: str | None, username: str | None, expected: str | None) -> None:
    result = AuthResult(oid=OID, tid=TENANT, home_account_id=HOME, email=email, preferred_username=username)
    assert result.login_email == expected


def test_configured_flag(database_url) -> None:
    assert make_client(database_url, {}).configured is True
    assert EntraOAuthClient(make_settings(database_url)).configured is False


def test_real_msal_application_is_built_from_settings(database_url, monkeypatch) -> None:
    built: list[dict[str, Any]] = []

    class FakeConfidentialClient:
        def __init__(self, client_id: str, **kwargs: Any) -> None:
            built.append({"client_id": client_id, **kwargs})

    monkeypatch.setattr(msal, "ConfidentialClientApplication", FakeConfidentialClient)
    settings = make_settings(
        database_url,
        entra_client_id="00000000-0000-0000-0000-000000000001",
        entra_client_secret="not-a-real-secret",
        entra_tenant_id=TENANT,
        entra_authority_host="https://login.example.invalid/",
        graph_timeout_seconds=9.0,
    )
    cache = new_token_cache()
    EntraOAuthClient(settings)._build_app(cache)
    (kwargs,) = built
    assert kwargs["client_id"] == "00000000-0000-0000-0000-000000000001"
    assert kwargs["client_credential"] == "not-a-real-secret"
    assert kwargs["authority"] == f"https://login.example.invalid/{TENANT}"
    assert kwargs["token_cache"] is cache
    assert kwargs["http_cache"] is oauth_module._MSAL_HTTP_CACHE
    assert kwargs["timeout"] == 9.0

    no_secret = settings.model_copy(update={"entra_client_secret": None})
    EntraOAuthClient(no_secret)._build_app(None)
    assert built[1]["client_credential"] is None


class RaisingApp(FakeApp):
    def __init__(self, cache: Any, script: dict[str, Any]) -> None:
        super().__init__(cache, script)

    def initiate_auth_code_flow(self, scopes: list[str], **kwargs: Any) -> dict[str, Any]:
        if "init_raise" in self.script:
            raise self.script["init_raise"]
        result: dict[str, Any] = self.script["flow"]
        return result

    def get_accounts(self, username: str | None = None) -> list[dict[str, Any]]:
        if "accounts_raise" in self.script:
            raise self.script["accounts_raise"]
        return super().get_accounts(username)


def raising_client(database_url: str, script: dict[str, Any]) -> EntraOAuthClient:
    settings = make_settings(
        database_url, entra_client_id="00000000-0000-0000-0000-000000000001", entra_client_secret="not-a-real-secret"
    )
    return EntraOAuthClient(settings, app_factory=lambda cache: RaisingApp(cache, script))


@pytest.mark.parametrize("error", [ValueError("bad scope"), OSError("dns"), RuntimeError("discovery")])
async def test_begin_flow_errors_are_transient(database_url, error: Exception) -> None:
    with pytest.raises(IntegrationError) as info:
        await raising_client(database_url, {"init_raise": error}).begin_flow(GRAPH_MAIL_SCOPES)
    assert info.value.category == FailureCategory.TRANSIENT
    assert info.value.retryable is True


@pytest.mark.parametrize(
    "flow",
    [
        None,
        ["not", "a", "dict"],
        {"state": "s"},  # no auth_uri
        {"auth_uri": 7, "state": "s"},
        {"auth_uri": "https://login.example.invalid/authorize", "state": ""},
    ],
)
async def test_invalid_flows_are_rejected(database_url, flow: Any) -> None:
    with pytest.raises(IntegrationError) as info:
        await raising_client(database_url, {"flow": flow}).begin_flow(GRAPH_MAIL_SCOPES)
    assert info.value.category == FailureCategory.INTERNAL


@pytest.mark.parametrize("prompt", [None, "login", "none", "consent", "select_account", "create"])
async def test_allowed_prompts(database_url, prompt: str | None) -> None:
    script: dict[str, Any] = {}
    await make_client(database_url, script).begin_flow(GRAPH_MAIL_SCOPES, prompt=prompt)
    assert script["init_calls"][0][1]["prompt"] == prompt


class _NetworkDown(FakeApp):
    def acquire_token_by_auth_code_flow(self, auth_code_flow, auth_response, **kwargs):
        self.cache.state = "partially-written"
        raise OSError("connection reset")


class _OddResult(FakeApp):
    def acquire_token_by_auth_code_flow(self, auth_code_flow, auth_response, **kwargs):
        self.cache.state = "partially-written"
        return ["not", "a", "dict"]


@pytest.mark.parametrize(
    ("app_cls", "category", "retryable"),
    [(_NetworkDown, FailureCategory.TRANSIENT, True), (_OddResult, FailureCategory.INTERNAL, False)],
)
async def test_complete_flow_failures_restore_the_cache(database_url, app_cls, category, retryable) -> None:
    settings = make_settings(
        database_url, entra_client_id="00000000-0000-0000-0000-000000000001", entra_client_secret="not-a-real-secret"
    )
    oauth = EntraOAuthClient(settings, app_factory=lambda cache: app_cls(cache, {}))
    cache = FakeCache('{"before": true}')
    with pytest.raises(IntegrationError) as info:
        await oauth.complete_flow({"state": "s"}, {"state": "s", "code": "c"}, cache)
    assert info.value.category == category
    assert info.value.retryable is retryable
    assert cache.state == '{"before": true}'


async def test_complete_flow_error_result_restores_the_cache(database_url) -> None:
    oauth = make_client(database_url, {"token_result": {"error": "invalid_grant", "error_codes": 70008}})
    flow = await oauth.begin_flow(GRAPH_MAIL_SCOPES)
    cache = FakeCache("{}")
    with pytest.raises(IntegrationError) as info:
        await oauth.complete_flow(flow, {"state": flow["state"], "code": "c"}, cache)
    assert info.value.category == FailureCategory.AUTHORIZATION
    assert cache.state == "{}"  # restored even though FakeApp wrote tokens before returning the error


@pytest.mark.parametrize(
    "claims",
    [None, "not-a-mapping", {"oid": OID}, {"oid": "  ", "tid": TENANT}, {"oid": OID, "tid": 5}],
)
async def test_missing_or_blank_identity_claims_are_rejected(database_url, claims: Any) -> None:
    oauth = make_client(database_url, {"token_result": {**SUCCESS, "id_token_claims": claims}})
    flow = await oauth.begin_flow(GRAPH_MAIL_SCOPES)
    with pytest.raises(IntegrationError) as info:
        await oauth.complete_flow(flow, {"state": flow["state"], "code": "c"}, FakeCache())
    assert info.value.category == FailureCategory.AUTHORIZATION


async def test_identity_claims_are_trimmed_and_scopes_parsed(database_url) -> None:
    claims = {"oid": f" {OID} ", "tid": TENANT, "email": " Morgan@Example.com ", "name": "", "preferred_username": 7}
    result_payload = {**SUCCESS, "id_token_claims": claims, "client_info": None, "scope": ["Mail.Read"]}
    oauth = make_client(database_url, {"token_result": result_payload})
    flow = await oauth.begin_flow(GRAPH_MAIL_SCOPES)
    result = await oauth.complete_flow(flow, {"state": flow["state"], "code": "c"}, FakeCache())
    assert (result.oid, result.home_account_id) == (OID, f"{OID}.{TENANT}")
    assert (result.email, result.name, result.preferred_username) == ("Morgan@Example.com", None, None)
    assert result.granted_scopes == ["Mail.Read"]
    assert result.login_email == "morgan@example.com"


@pytest.mark.parametrize("error", [OSError("dns"), RuntimeError("msal"), ValueError("bad cache")])
async def test_silent_acquisition_exceptions_are_transient(database_url, error: Exception) -> None:
    oauth = raising_client(database_url, {"accounts_raise": error})
    with pytest.raises(IntegrationError) as info:
        await oauth.acquire_token_silent(GRAPH_MAIL_SCOPES, HOME, FakeCache())
    assert info.value.category == FailureCategory.TRANSIENT
    assert info.value.retryable is True


@pytest.mark.parametrize(
    ("silent", "category"),
    [
        ({"error": "temporarily_unavailable"}, FailureCategory.TRANSIENT),
        ({"error": "invalid_client"}, FailureCategory.CONFIGURATION),
        ({"access_token": ""}, FailureCategory.AUTHORIZATION),
        ({"access_token": 123}, FailureCategory.AUTHORIZATION),
    ],
)
async def test_silent_acquisition_results(database_url, silent: dict[str, Any], category: FailureCategory) -> None:
    script = {"accounts": [{"home_account_id": "someone.else"}, {"home_account_id": HOME}], "silent_result": silent}
    oauth = make_client(database_url, script)
    with pytest.raises(IntegrationError) as info:
        await oauth.acquire_token_silent(["openid", *GRAPH_MAIL_SCOPES, "offline_access"], HOME, FakeCache())
    assert info.value.category == category
    assert script["silent_calls"] == [(["Mail.Read"], {"home_account_id": HOME})]  # reserved scopes stripped


def test_new_token_cache() -> None:
    empty = new_token_cache()
    assert isinstance(empty, msal.SerializableTokenCache)
    assert empty.has_state_changed is False
    assert json.loads(empty.serialize() or "{}") == {}
    serialized = json.dumps({"AccessToken": {}, "RefreshToken": {}})
    loaded = new_token_cache(serialized)
    assert loaded.has_state_changed is False
    assert new_token_cache("").has_state_changed is False
