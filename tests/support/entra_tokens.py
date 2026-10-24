"""Signed Microsoft Entra ID tokens for tests (throwaway RSA key + matching JWKS; no network)."""

from __future__ import annotations

import hashlib
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.core.config import Settings
from app.integrations.microsoft_graph.id_token import IdTokenValidator

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "nuvexa-test-key"


def jwks() -> dict[str, Any]:
    jwk = RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True)
    return {"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]}


async def static_jwks(tenant_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    return jwks()


def sign_id_token(claims: dict[str, Any], *, client_id: str, authority_host: str, nonce: str | None = None) -> str:
    """Sign `claims` (must include tid/oid) as a valid ID token for `client_id`, adding standard claims."""
    now = int(time.time())
    payload = {
        "iss": f"{authority_host.rstrip('/')}/{claims.get('tid', 'missing')}/v2.0",
        "aud": client_id,
        "sub": claims.get("sub", claims.get("oid", "test-subject")),
        "iat": now - 5,
        "nbf": now - 5,
        "exp": now + 3600,
        **claims,
        **({"nonce": nonce} if nonce else {}),
    }
    return jwt.encode(payload, _KEY, algorithm="RS256", headers={"kid": KID})


def msal_nonce_claim(raw_flow_nonce: str | None) -> str | None:
    """What Microsoft puts in the `nonce` claim for an MSAL flow: the SHA-256 hex digest of the raw nonce."""
    return hashlib.sha256(raw_flow_nonce.encode("ascii")).hexdigest() if raw_flow_nonce else None


def test_validator(settings: Settings) -> IdTokenValidator:
    return IdTokenValidator(
        client_id=settings.entra_client_id or "",
        authority_host=settings.entra_authority_host,
        allowed_tenant_ids=settings.entra_allowed_tenant_ids,
        jwks_fetcher=static_jwks,
    )


test_validator.__test__ = False  # type: ignore[attr-defined] — helper, not a pytest test
