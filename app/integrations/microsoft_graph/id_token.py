"""Validation of Microsoft Entra ID tokens before any claim is trusted.

MSAL checks `aud` and `nonce` on the tokens it redeems, but only logs expiry problems and cannot pin the
issuer for multi-tenant authorities. This validator verifies the RS256 signature against the tenant's
published signing keys (JWKS) and enforces issuer, audience, lifetime, tenant allow-list and nonce.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from app.core.errors import IntegrationError
from app.domain.enums import FailureCategory
from app.observability.logging import get_logger

log = get_logger(__name__)

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_REQUIRED_CLAIMS = ("iss", "aud", "exp", "iat", "sub", "tid", "oid")
_ALGORITHMS = ["RS256"]


class IdTokenError(IntegrationError):
    """The ID token cannot be trusted (signature, issuer, audience, lifetime, tenant or nonce)."""

    def __init__(self, message: str, *, category: FailureCategory = FailureCategory.AUTHORIZATION) -> None:
        super().__init__(message, category=category, retryable=False)


class JwksFetcher(Protocol):
    def __call__(self, tenant_id: str, *, force_refresh: bool = False) -> Awaitable[dict[str, Any]]: ...


def is_tenant_guid(value: str) -> bool:
    return bool(_GUID_RE.match(value))


class HttpJwksFetcher:
    """Fetches `{authority}/{tenant}/discovery/v2.0/keys` with a TTL cache (keys rotate rarely)."""

    def __init__(
        self,
        authority_host: str,
        *,
        ttl_seconds: float = 3600,
        timeout_seconds: float = 10,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        cache: dict[str, tuple[float, dict[str, Any]]] | None = None,
    ) -> None:
        self._authority = authority_host.rstrip("/")
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._clock = clock
        self._client = http_client  # None → a short-lived client per fetch (no event-loop affinity)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {} if cache is None else cache

    async def __call__(self, tenant_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
        if not is_tenant_guid(tenant_id):  # the tenant id ends up in the URL: allow GUIDs only
            raise IdTokenError("The ID token names an invalid tenant.")
        cache_key = f"{self._authority}/{tenant_id}"
        cached = self._cache.get(cache_key)
        if cached and not force_refresh and self._clock() - cached[0] < self._ttl:
            return cached[1]
        url = f"{self._authority}/{tenant_id}/discovery/v2.0/keys"
        try:
            if self._client is not None:
                response = await self._client.get(url)
            else:
                async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                    response = await client.get(url)
        except httpx.HTTPError as exc:
            raise IdTokenError("Microsoft signing keys could not be retrieved.") from exc
        if response.status_code != 200:
            raise IdTokenError("Microsoft signing keys could not be retrieved.")
        try:
            document = response.json()
        except ValueError as exc:
            raise IdTokenError("Microsoft signing keys response is malformed.") from exc
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise IdTokenError("Microsoft signing keys response is malformed.")
        self._cache[cache_key] = (self._clock(), document)
        return document

    async def aclose(self) -> None:
        """Kept for API symmetry; per-fetch clients are closed immediately and injected ones by their owner."""


class IdTokenValidator:
    def __init__(
        self,
        *,
        client_id: str,
        authority_host: str,
        allowed_tenant_ids: Sequence[str],
        jwks_fetcher: JwksFetcher,
        clock: Callable[[], float] = time.time,
        leeway_seconds: int = 120,
    ) -> None:
        self._client_id = client_id
        self._authority = authority_host.rstrip("/")
        self._allowed = {t.lower() for t in allowed_tenant_ids}
        self._jwks = jwks_fetcher
        self._clock = clock
        self._leeway = leeway_seconds

    async def validate(self, id_token: str, *, nonce: str | None) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
            unverified = jwt.decode(id_token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise IdTokenError("The ID token is malformed.") from exc
        if header.get("alg") not in _ALGORITHMS or not isinstance(header.get("kid"), str):
            raise IdTokenError("The ID token uses an unsupported signing algorithm.")
        tenant_id = unverified.get("tid")
        if not isinstance(tenant_id, str) or not is_tenant_guid(tenant_id):
            raise IdTokenError("The ID token names an invalid tenant.")
        if self._allowed and tenant_id.lower() not in self._allowed:
            raise IdTokenError("This organization is not allowed to sign in.", category=FailureCategory.PERMISSION)

        key = await self._signing_key(tenant_id, header["kid"])
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key=key,
                algorithms=_ALGORITHMS,
                audience=self._client_id,
                issuer=f"{self._authority}/{tenant_id}/v2.0",
                options={
                    "require": list(_REQUIRED_CLAIMS),
                    # Lifetime is checked below against the injected clock (one source of truth for time).
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                },
            )
            now = self._clock()
            if claims["exp"] + self._leeway < now:
                raise jwt.ExpiredSignatureError("expired")
            if "nbf" in claims and claims["nbf"] - self._leeway > now:
                raise jwt.ImmatureSignatureError("not yet valid")
            if claims["iat"] - self._leeway > now:
                raise jwt.ImmatureSignatureError("issued in the future")
        except jwt.PyJWTError as exc:
            log.warning("id_token_rejected", reason=type(exc).__name__)
            raise IdTokenError("The ID token failed validation.") from exc
        if nonce is not None and claims.get("nonce") != nonce:
            raise IdTokenError("The ID token nonce does not match the sign-in request.")
        return claims

    async def _signing_key(self, tenant_id: str, kid: str) -> Any:
        for force in (False, True):  # unknown kid → refresh once (key rotation), then give up
            document = await self._jwks(tenant_id, force_refresh=force)
            for jwk in document.get("keys", []):
                if isinstance(jwk, dict) and jwk.get("kid") == kid and jwk.get("kty") == "RSA":
                    try:
                        return RSAAlgorithm.from_jwk(jwk)
                    except (ValueError, KeyError, TypeError) as exc:
                        raise IdTokenError("Microsoft signing key is malformed.") from exc
        raise IdTokenError("The ID token was signed with an unknown key.")
