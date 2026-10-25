"""Microsoft Entra ID token validation: signature (JWKS), issuer, audience, lifetime, tenant and nonce.

MSAL validates audience and nonce but only *logs* expiry problems and cannot pin the issuer for
multi-tenant authorities, so the application validates ID tokens itself before trusting any claim.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import httpx
import jwt
import msal
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.core.errors import IntegrationError
from app.domain.enums import FailureCategory
from app.integrations.microsoft_graph.id_token import HttpJwksFetcher, IdTokenError, IdTokenValidator
from app.integrations.microsoft_graph.oauth import EntraOAuthClient
from tests.conftest import TEST_FERNET_KEY, make_settings

AUTHORITY = "https://login.microsoftonline.com"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_TENANT = "99999999-8888-7777-6666-555555555555"
NOW = 1_800_000_000


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = _key()
ROTATED_KEY = _key()
ATTACKER_KEY = _key()


def jwks(*keys: tuple[str, rsa.RSAPrivateKey]) -> dict[str, Any]:
    entries = []
    for kid, key in keys:
        jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        entries.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
    return {"keys": entries}


def claims(**overrides: Any) -> dict[str, Any]:
    base = {
        "iss": f"{AUTHORITY}/{TENANT}/v2.0",
        "aud": CLIENT_ID,
        "tid": TENANT,
        "oid": str(uuid.uuid4()),
        "sub": "subject-1",
        "iat": NOW - 60,
        "nbf": NOW - 60,
        "exp": NOW + 3600,
        "nonce": "nonce-1",
        "preferred_username": "dana@contoso.example",
        "name": "Dana",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def sign(payload: dict[str, Any], key: rsa.RSAPrivateKey = SIGNING_KEY, kid: str = "k1", alg: str = "RS256") -> str:
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid})


class FakeJwks:
    def __init__(self, *documents: dict[str, Any]) -> None:
        self.documents = list(documents)
        self.calls: list[tuple[str, bool]] = []

    async def __call__(self, tenant_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
        self.calls.append((tenant_id, force_refresh))
        return self.documents[min(len(self.calls) - 1, len(self.documents) - 1)]


def validator(fetcher: FakeJwks | None = None, *, allowed: list[str] | None = None) -> IdTokenValidator:
    return IdTokenValidator(
        client_id=CLIENT_ID,
        authority_host=AUTHORITY,
        allowed_tenant_ids=allowed if allowed is not None else [TENANT],
        jwks_fetcher=fetcher or FakeJwks(jwks(("k1", SIGNING_KEY))),
        clock=lambda: NOW,
        leeway_seconds=120,
    )


# --------------------------------------------------------------------------------------- positive


async def test_valid_token_returns_verified_claims() -> None:
    result = await validator().validate(sign(claims()), nonce="nonce-1")
    assert result["tid"] == TENANT
    assert result["aud"] == CLIENT_ID


async def test_expiry_within_clock_skew_leeway_is_accepted() -> None:
    await validator().validate(sign(claims(exp=NOW - 60)), nonce="nonce-1")


# --------------------------------------------------------------------------------------- negative


@pytest.mark.parametrize(
    ("description", "token"),
    [
        ("signed by an unknown key", lambda: sign(claims(), key=ATTACKER_KEY)),
        ("wrong audience", lambda: sign(claims(aud="another-client"))),
        ("issuer from another host", lambda: sign(claims(iss=f"https://evil.example/{TENANT}/v2.0"))),
        ("issuer for another tenant than tid", lambda: sign(claims(iss=f"{AUTHORITY}/{OTHER_TENANT}/v2.0"))),
        ("v1 issuer", lambda: sign(claims(iss=f"https://sts.windows.net/{TENANT}/"))),
        ("expired beyond leeway", lambda: sign(claims(exp=NOW - 600))),
        ("not yet valid", lambda: sign(claims(nbf=NOW + 600))),
        ("issued in the future", lambda: sign(claims(iat=NOW + 600))),
        ("missing tid", lambda: sign(claims(tid=None))),
        ("missing oid", lambda: sign(claims(oid=None))),
        ("missing exp", lambda: sign(claims(exp=None))),
        ("tid is not a GUID", lambda: sign(claims(tid="../../evil", iss=f"{AUTHORITY}/../../evil/v2.0"))),
    ],
)
async def test_invalid_tokens_are_rejected(description: str, token: Any) -> None:
    with pytest.raises(IdTokenError):
        await validator().validate(token(), nonce="nonce-1")


async def test_claims_edited_after_signing_are_rejected_even_for_an_allowed_tenant() -> None:
    # The validator reads `tid` before verifying, to pick the tenant's keys and issuer
    # (security-exceptions.toml SEC-EX-003). Editing `tid`/`iss` to another allowed tenant that uses the
    # same signing key must still fail: only the verified decode's claims are ever returned.
    header, _, signature = sign(claims()).split(".")
    forged_claims = claims(tid=OTHER_TENANT, iss=f"{AUTHORITY}/{OTHER_TENANT}/v2.0", oid="victim")
    forged_payload = jwt.utils.base64url_encode(json.dumps(forged_claims).encode()).decode()
    forged = f"{header}.{forged_payload}.{signature}"
    fetcher = FakeJwks(jwks(("k1", SIGNING_KEY)))
    with pytest.raises(IdTokenError):
        await validator(fetcher, allowed=[TENANT, OTHER_TENANT]).validate(forged, nonce="nonce-1")
    assert fetcher.calls == [(OTHER_TENANT, False)]  # the unverified tid only chose which keys to try


async def test_wrong_tenant_is_rejected_when_tenants_are_restricted() -> None:
    other = sign(claims(tid=OTHER_TENANT, iss=f"{AUTHORITY}/{OTHER_TENANT}/v2.0"))
    with pytest.raises(IdTokenError):
        await validator().validate(other, nonce="nonce-1")


async def test_nonce_mismatch_is_rejected() -> None:
    with pytest.raises(IdTokenError):
        await validator().validate(sign(claims()), nonce="different-nonce")


@pytest.mark.parametrize("alg", ["HS256", "none"])
async def test_algorithm_confusion_is_rejected(alg: str) -> None:
    payload = claims()
    if alg == "none":
        token = jwt.encode(payload, None, algorithm="none", headers={"kid": "k1"})
    else:  # RS/HS confusion: an HMAC-"signed" token must never be accepted, whatever the secret
        token = jwt.encode(
            payload, "attacker-chosen-secret-of-sufficient-length", algorithm="HS256", headers={"kid": "k1"}
        )
    with pytest.raises(IdTokenError):
        await validator().validate(token, nonce="nonce-1")


async def test_malformed_token_is_rejected() -> None:
    with pytest.raises(IdTokenError):
        await validator().validate("not-a-jwt", nonce=None)


# --------------------------------------------------------------------------------------- key rotation


async def test_unknown_kid_triggers_one_forced_jwks_refresh() -> None:
    fetcher = FakeJwks(jwks(("k1", SIGNING_KEY)), jwks(("k1", SIGNING_KEY), ("k2", ROTATED_KEY)))
    result = await validator(fetcher).validate(sign(claims(), key=ROTATED_KEY, kid="k2"), nonce="nonce-1")
    assert result["tid"] == TENANT
    assert fetcher.calls == [(TENANT, False), (TENANT, True)]


async def test_unknown_kid_after_refresh_is_rejected() -> None:
    fetcher = FakeJwks(jwks(("k1", SIGNING_KEY)))
    with pytest.raises(IdTokenError):
        await validator(fetcher).validate(sign(claims(), key=ROTATED_KEY, kid="k9"), nonce="nonce-1")
    assert len(fetcher.calls) == 2


# --------------------------------------------------------------------------------------- JWKS fetcher


@respx.mock
async def test_http_jwks_fetcher_caches_and_force_refreshes() -> None:
    route = respx.get(f"{AUTHORITY}/{TENANT}/discovery/v2.0/keys").mock(
        return_value=httpx.Response(200, json=jwks(("k1", SIGNING_KEY)))
    )
    fetcher = HttpJwksFetcher(AUTHORITY, ttl_seconds=3600)
    first = await fetcher(TENANT)
    await fetcher(TENANT)
    assert route.call_count == 1
    await fetcher(TENANT, force_refresh=True)
    assert route.call_count == 2
    assert first["keys"][0]["kid"] == "k1"
    await fetcher.aclose()


@respx.mock
@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, json={"no": "keys"})])
async def test_http_jwks_fetcher_errors_are_reported(response: httpx.Response) -> None:
    respx.get(f"{AUTHORITY}/{TENANT}/discovery/v2.0/keys").mock(return_value=response)
    fetcher = HttpJwksFetcher(AUTHORITY)
    with pytest.raises(IdTokenError):
        await fetcher(TENANT)
    await fetcher.aclose()


async def test_http_jwks_fetcher_refuses_non_guid_tenants() -> None:
    fetcher = HttpJwksFetcher(AUTHORITY)
    with pytest.raises(IdTokenError):
        await fetcher("evil.example/../../x")
    await fetcher.aclose()


# --------------------------------------------------------------------------------------- wiring


class FakeMsalApp:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

    def acquire_token_by_auth_code_flow(self, flow: dict[str, Any], response: dict[str, str], **_: Any) -> Any:
        return self.result


def entra_client(result: dict[str, Any]) -> EntraOAuthClient:
    settings = make_settings(
        "sqlite+aiosqlite://",
        entra_client_id=CLIENT_ID,
        entra_client_secret="test-secret",
        entra_tenant_id=TENANT,
        entra_allowed_tenant_ids=TENANT,
        token_encryption_keys=TEST_FERNET_KEY,
    )
    return EntraOAuthClient(
        settings,
        app_factory=lambda _cache: FakeMsalApp(result),
        id_token_validator=validator(),  # type: ignore[arg-type]
    )


RAW_FLOW_NONCE = "raw-flow-nonce"
HASHED_NONCE = hashlib.sha256(RAW_FLOW_NONCE.encode("ascii")).hexdigest()  # what MSAL sends / Microsoft echoes


async def test_sign_in_uses_verified_claims_not_msal_decoded_ones() -> None:
    token = sign(claims(nonce=HASHED_NONCE))
    # MSAL's decoded claims are attacker-influenced in this scenario; only the verified token counts.
    forged = {**claims(), "tid": OTHER_TENANT, "oid": "forged"}
    client = entra_client({"id_token": token, "id_token_claims": forged, "client_info": "", "scope": "User.Read"})
    flow = {"state": "s", "nonce": RAW_FLOW_NONCE}
    result = await client.complete_flow(flow, {"state": "s"}, msal.SerializableTokenCache())
    assert result.tid == TENANT
    assert result.oid != "forged"


async def test_sign_in_rejects_a_token_whose_nonce_is_not_the_flows_hashed_nonce() -> None:
    client = entra_client({"id_token": sign(claims(nonce=RAW_FLOW_NONCE)), "scope": "User.Read"})
    with pytest.raises(IntegrationError):
        await client.complete_flow(
            {"state": "s", "nonce": "a-different-flow"}, {"state": "s"}, msal.SerializableTokenCache()
        )


async def test_sign_in_with_an_unverifiable_token_fails_and_restores_the_cache() -> None:
    token = sign(claims(), key=ATTACKER_KEY)
    client = entra_client({"id_token": token, "id_token_claims": claims(), "scope": "User.Read"})
    cache = msal.SerializableTokenCache()
    before = cache.serialize()
    with pytest.raises(IntegrationError):
        await client.complete_flow({"state": "s", "nonce": "nonce-1"}, {"state": "s"}, cache)
    assert cache.serialize() == before


async def test_sign_in_without_a_raw_id_token_fails() -> None:
    client = entra_client({"id_token_claims": claims(), "scope": "User.Read"})
    with pytest.raises(IntegrationError):
        await client.complete_flow({"state": "s", "nonce": "nonce-1"}, {"state": "s"}, msal.SerializableTokenCache())


# --------------------------------------------------------------------------------------- mutation-hardening
# Pinned down after mutation testing showed these behaviours were not asserted (docs/TESTING_STRATEGY.md).


async def test_rs256_token_without_a_key_id_is_rejected_cleanly() -> None:
    token = jwt.encode(claims(), SIGNING_KEY, algorithm="RS256")  # no "kid" header
    with pytest.raises(IdTokenError):
        await validator().validate(token, nonce="nonce-1")


async def test_lifetime_is_judged_by_the_injected_clock_only() -> None:
    past = 1_600_000_000  # long before the wall clock: PyJWT's own exp check would reject this token
    old_validator = IdTokenValidator(
        client_id=CLIENT_ID,
        authority_host=AUTHORITY,
        allowed_tenant_ids=[TENANT],
        jwks_fetcher=FakeJwks(jwks(("k1", SIGNING_KEY))),
        clock=lambda: past,
    )
    token = sign(claims(iat=past - 60, nbf=past - 60, exp=past + 3600))
    assert (await old_validator.validate(token, nonce="nonce-1"))["tid"] == TENANT


@pytest.mark.parametrize(
    "overrides",
    [{"exp": NOW - 120}, {"nbf": NOW + 120}, {"iat": NOW + 120}],
    ids=["exp-exactly-at-leeway", "nbf-exactly-at-leeway", "iat-exactly-at-leeway"],
)
async def test_leeway_boundaries_are_inclusive(overrides: dict[str, int]) -> None:
    await validator().validate(sign(claims(**overrides)), nonce="nonce-1")


async def test_a_jwks_document_without_keys_is_rejected() -> None:
    with pytest.raises(IdTokenError):
        await validator(FakeJwks({})).validate(sign(claims()), nonce="nonce-1")


def test_id_token_errors_are_authorization_failures_and_never_retried() -> None:
    error = IdTokenError("x")
    assert error.retryable is False
    assert error.category == FailureCategory.AUTHORIZATION


@respx.mock
async def test_jwks_fetcher_never_follows_redirects() -> None:
    respx.get(f"{AUTHORITY}/{TENANT}/discovery/v2.0/keys").mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.example/keys"})
    )
    evil = respx.get("https://evil.example/keys").mock(
        return_value=httpx.Response(200, json=jwks(("k1", ATTACKER_KEY)))
    )
    with pytest.raises(IdTokenError):
        await HttpJwksFetcher(AUTHORITY)(TENANT)
    assert evil.call_count == 0


@respx.mock
async def test_jwks_cache_is_per_tenant() -> None:
    first = respx.get(f"{AUTHORITY}/{TENANT}/discovery/v2.0/keys").mock(
        return_value=httpx.Response(200, json=jwks(("k1", SIGNING_KEY)))
    )
    second = respx.get(f"{AUTHORITY}/{OTHER_TENANT}/discovery/v2.0/keys").mock(
        return_value=httpx.Response(200, json=jwks(("k2", ROTATED_KEY)))
    )
    fetcher = HttpJwksFetcher(AUTHORITY)
    assert (await fetcher(TENANT))["keys"][0]["kid"] == "k1"
    assert (await fetcher(OTHER_TENANT))["keys"][0]["kid"] == "k2"
    assert (first.call_count, second.call_count) == (1, 1)


@respx.mock
async def test_authority_with_a_trailing_slash_is_normalized() -> None:
    route = respx.get(f"{AUTHORITY}/{TENANT}/discovery/v2.0/keys").mock(
        return_value=httpx.Response(200, json=jwks(("k1", SIGNING_KEY)))
    )
    await HttpJwksFetcher(AUTHORITY + "/")(TENANT)
    assert route.call_count == 1
    slashy = IdTokenValidator(
        client_id=CLIENT_ID,
        authority_host=AUTHORITY + "/",
        allowed_tenant_ids=[TENANT],
        jwks_fetcher=FakeJwks(jwks(("k1", SIGNING_KEY))),
        clock=lambda: NOW,
    )
    await slashy.validate(sign(claims()), nonce="nonce-1")


@respx.mock
async def test_jwks_cache_expires_exactly_at_the_ttl() -> None:
    route = respx.get(f"{AUTHORITY}/{TENANT}/discovery/v2.0/keys").mock(
        return_value=httpx.Response(200, json=jwks(("k1", SIGNING_KEY)))
    )
    now = [1000.0]
    fetcher = HttpJwksFetcher(AUTHORITY, ttl_seconds=60, clock=lambda: now[0])
    await fetcher(TENANT)
    now[0] += 59.9
    await fetcher(TENANT)
    assert route.call_count == 1
    now[0] = 1060.0  # exactly one TTL later
    await fetcher(TENANT)
    assert route.call_count == 2


async def test_jwks_fetches_use_the_configured_timeout_and_never_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, Any]] = []
    real_client = httpx.AsyncClient

    def recording_client(**kwargs: Any) -> httpx.AsyncClient:
        created.append(kwargs)
        return real_client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=jwks(("k1", SIGNING_KEY))))
        )

    monkeypatch.setattr("app.integrations.microsoft_graph.id_token.httpx.AsyncClient", recording_client)
    await HttpJwksFetcher(AUTHORITY, timeout_seconds=7)(TENANT)
    assert created == [{"timeout": 7, "follow_redirects": False}]
