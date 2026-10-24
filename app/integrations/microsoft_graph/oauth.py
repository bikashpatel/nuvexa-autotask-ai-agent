"""Microsoft Entra ID auth-code flow (PKCE, state, nonce) via MSAL `ConfidentialClientApplication`.

MSAL is synchronous and may perform network I/O (discovery, token redemption), so every call runs in a
worker thread. A fresh application object is built per call around the caller's token cache (the pattern
used by MSAL web-app samples); discovery responses are shared through a process-wide `http_cache`.
Tokens never leave this module except as the return value of `acquire_token_silent`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

import msal
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.core.errors import IntegrationError, IntegrationNotConfigured
from app.domain.enums import FailureCategory
from app.observability.logging import get_logger

log = get_logger(__name__)

# MSAL adds these itself and raises ValueError if they are passed explicitly.
_RESERVED_SCOPES = frozenset({"openid", "profile", "offline_access"})
_ALLOWED_PROMPTS = frozenset({"login", "none", "consent", "select_account", "create"})
_TENANT_RE = re.compile(r"^[A-Za-z0-9.-]{1,64}$")
_ERROR_CODE_RE = re.compile(r"^[a-z_]{1,64}$")
_REAUTH_ERRORS = frozenset(
    {
        "invalid_grant",
        "interaction_required",
        "consent_required",
        "login_required",
        "access_denied",
        "invalid_scope",
        "unauthorized_client",
        "invalid_request",
    }
)
_CONFIG_ERRORS = frozenset({"invalid_client", "unsupported_response_type", "invalid_resource"})
_TRANSIENT_ERRORS = frozenset({"temporarily_unavailable", "server_error"})

# Discovery/metadata responses only (no tokens, no PII) — safe to share across application instances.
_MSAL_HTTP_CACHE: dict[str, Any] = {}


class MsalApplication(Protocol):
    """The subset of `msal.ConfidentialClientApplication` this module uses (lets tests inject a fake)."""

    def initiate_auth_code_flow(self, scopes: list[str], **kwargs: Any) -> dict[str, Any]: ...

    def acquire_token_by_auth_code_flow(
        self, auth_code_flow: dict[str, Any], auth_response: dict[str, str], **kwargs: Any
    ) -> dict[str, Any]: ...

    def get_accounts(self, username: str | None = None) -> list[dict[str, Any]]: ...

    def acquire_token_silent_with_error(
        self, scopes: list[str], account: dict[str, Any] | None, **kwargs: Any
    ) -> dict[str, Any] | None: ...


type MsalAppFactory = Callable[[msal.SerializableTokenCache | None], MsalApplication]


class AuthResult(BaseModel):
    """Identity established by a completed auth-code flow. Contains no tokens."""

    model_config = ConfigDict(frozen=True)

    oid: str  # Entra object id of the user (in `tid`)
    tid: str  # Entra tenant id the token was issued for
    home_account_id: str  # MSAL cache key ("<uid>.<utid>"); store as ConnectedAccount.external_account_id
    preferred_username: str | None = None
    email: str | None = None
    name: str | None = None
    granted_scopes: list[str] = Field(default_factory=list)

    @property
    def login_email(self) -> str | None:
        """Best e-mail-like identifier, lower-cased (`email` claim, else `preferred_username`)."""
        value = self.email or self.preferred_username
        return value.lower() if value and "@" in value else None


def clean_scopes(scopes: Iterable[str]) -> list[str]:
    """De-duplicate and drop MSAL-reserved OIDC scopes (MSAL always requests them)."""
    return list(dict.fromkeys(s.strip() for s in scopes if s.strip() and s.strip() not in _RESERVED_SCOPES))


def _claim(claims: Mapping[str, Any], key: str) -> str | None:
    value = claims.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _home_account_id(result: Mapping[str, Any], oid: str, tid: str) -> str:
    raw = result.get("client_info")
    if isinstance(raw, str) and raw:
        try:
            info = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except ValueError, TypeError:
            info = None
        if isinstance(info, dict) and isinstance(info.get("uid"), str) and isinstance(info.get("utid"), str):
            return f"{info['uid']}.{info['utid']}"
    return f"{oid}.{tid}"


def _granted_scopes(result: Mapping[str, Any]) -> list[str]:
    scope = result.get("scope")
    if isinstance(scope, str):
        return scope.split()
    if isinstance(scope, list):
        return [s for s in scope if isinstance(s, str)]
    return []


def map_msal_error(result: Mapping[str, Any], *, action: str) -> IntegrationError:
    """Turn an MSAL error dict into an `IntegrationError` without leaking descriptions or tokens."""
    error = result.get("error")
    code = error if isinstance(error, str) and _ERROR_CODE_RE.match(error) else "unknown_error"
    aadsts = [c for c in result.get("error_codes") or [] if isinstance(c, int)][:3]
    detail = f" (AADSTS{', AADSTS'.join(str(c) for c in aadsts)})" if aadsts else ""
    if code in _TRANSIENT_ERRORS:
        return IntegrationError(
            f"{action}: Microsoft Entra ID is temporarily unavailable ({code}){detail}",
            category=FailureCategory.TRANSIENT,
            retryable=True,
        )
    if code in _CONFIG_ERRORS:
        return IntegrationError(
            f"{action}: the application registration is misconfigured ({code}){detail}",
            category=FailureCategory.CONFIGURATION,
        )
    return IntegrationError(
        f"{action}: re-authentication is required ({code}){detail}", category=FailureCategory.AUTHORIZATION
    )


class EntraOAuthClient:
    """Async facade over MSAL for sign-in, mailbox consent and silent token acquisition."""

    def __init__(self, settings: Settings, *, app_factory: MsalAppFactory | None = None) -> None:
        self._settings = settings
        self._app_factory = app_factory

    # ------------------------------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return self._settings.entra_configured

    @property
    def authority(self) -> str:
        tenant = self._settings.entra_tenant_id
        if not _TENANT_RE.match(tenant):
            raise IntegrationNotConfigured("ENTRA_TENANT_ID is invalid")
        return f"{self._settings.entra_authority_host.rstrip('/')}/{tenant}"

    def _ensure_configured(self) -> None:
        if not self._settings.entra_configured:
            raise IntegrationNotConfigured(
                "Microsoft Entra ID is not configured (ENTRA_CLIENT_ID / ENTRA_CLIENT_SECRET)"
            )

    def _build_app(self, cache: msal.SerializableTokenCache | None) -> MsalApplication:
        if self._app_factory is not None:
            return self._app_factory(cache)
        secret = self._settings.entra_client_secret
        app: MsalApplication = msal.ConfidentialClientApplication(
            self._settings.entra_client_id,
            client_credential=secret.get_secret_value() if secret else None,
            authority=self.authority,
            token_cache=cache,
            http_cache=_MSAL_HTTP_CACHE,
            timeout=self._settings.graph_timeout_seconds,
        )
        return app

    # ------------------------------------------------------------------------------------------
    async def begin_flow(
        self, scopes: Iterable[str], *, login_hint: str | None = None, prompt: str | None = None
    ) -> dict[str, Any]:
        """Start an auth-code flow. Persist the returned dict server-side (it holds the PKCE verifier) and
        redirect the browser to `flow["auth_uri"]`. `flow["state"]` identifies the pending flow."""
        self._ensure_configured()
        if prompt is not None and prompt not in _ALLOWED_PROMPTS:
            raise ValueError(f"Unsupported prompt value: {prompt!r}")
        requested = clean_scopes(scopes)
        redirect_uri = self._settings.oauth_redirect_uri

        def _run() -> dict[str, Any]:
            app = self._build_app(None)
            return app.initiate_auth_code_flow(
                requested, redirect_uri=redirect_uri, login_hint=login_hint, prompt=prompt
            )

        try:
            flow = await asyncio.to_thread(_run)
        except (ValueError, OSError, RuntimeError) as exc:
            raise IntegrationError(
                "Could not start the Microsoft sign-in flow",
                category=FailureCategory.TRANSIENT,
                retryable=True,
            ) from exc
        if not isinstance(flow, dict) or not isinstance(flow.get("auth_uri"), str) or not flow.get("state"):
            raise IntegrationError("MSAL returned an invalid auth-code flow", category=FailureCategory.INTERNAL)
        return flow

    async def complete_flow(
        self, flow: dict[str, Any], auth_response: dict[str, str], cache: msal.SerializableTokenCache
    ) -> AuthResult:
        """Redeem the authorization code. MSAL validates `state`, PKCE and the id-token `nonce`.

        On success the tokens are in `cache` (an `msal.SerializableTokenCache`); persist it encrypted.
        On any failure the cache is restored to its previous state.
        """
        self._ensure_configured()
        snapshot: str = cache.serialize()

        def _run() -> dict[str, Any]:
            app = self._build_app(cache)
            return app.acquire_token_by_auth_code_flow(dict(flow), dict(auth_response))

        try:
            try:
                result = await asyncio.to_thread(_run)
            except ValueError as exc:  # state mismatch / malformed flow (possible CSRF)
                raise IntegrationError(
                    "The sign-in response does not match the pending sign-in request",
                    category=FailureCategory.AUTHORIZATION,
                ) from exc
            except RuntimeError as exc:  # id-token nonce / auth_time validation failures
                raise IntegrationError(
                    "The sign-in response failed id-token validation", category=FailureCategory.AUTHORIZATION
                ) from exc
            except OSError as exc:
                raise IntegrationError(
                    "Microsoft Entra ID could not be reached", category=FailureCategory.TRANSIENT, retryable=True
                ) from exc
            if not isinstance(result, dict):
                raise IntegrationError("MSAL returned an invalid token response", category=FailureCategory.INTERNAL)
            if "error" in result:
                raise map_msal_error(result, action="Sign-in failed")
            return self._auth_result(result)
        except IntegrationError:
            cache.deserialize(snapshot)
            raise

    def _auth_result(self, result: Mapping[str, Any]) -> AuthResult:
        claims = result.get("id_token_claims")
        if not isinstance(claims, Mapping):
            raise IntegrationError("Sign-in did not return an id token", category=FailureCategory.AUTHORIZATION)
        oid, tid = _claim(claims, "oid"), _claim(claims, "tid")
        if not oid or not tid:
            raise IntegrationError(
                "The id token is missing the oid/tid claims (work or school account required)",
                category=FailureCategory.AUTHORIZATION,
            )
        allowed = {t.lower() for t in self._settings.entra_allowed_tenant_ids}
        if allowed and tid.lower() not in allowed:
            log.warning("entra_tenant_rejected")
            raise IntegrationError(
                "Your organization is not allowed to use this application", category=FailureCategory.PERMISSION
            )
        return AuthResult(
            oid=oid,
            tid=tid,
            home_account_id=_home_account_id(result, oid, tid),
            preferred_username=_claim(claims, "preferred_username"),
            email=_claim(claims, "email"),
            name=_claim(claims, "name"),
            granted_scopes=_granted_scopes(result),
        )

    async def acquire_token_silent(
        self, scopes: Iterable[str], home_account_id: str, cache: msal.SerializableTokenCache
    ) -> str:
        """Return an access token for `scopes` from `cache`, refreshing with the refresh token if needed.

        Raises `IntegrationError(category="authorization")` when user interaction is required.
        The caller persists `cache` when `cache.has_state_changed`.
        """
        self._ensure_configured()
        requested = clean_scopes(scopes)

        def _run() -> dict[str, Any] | None:
            app = self._build_app(cache)
            accounts = app.get_accounts()
            account = next((a for a in accounts if a.get("home_account_id") == home_account_id), None)
            if account is None:
                return {"error": "login_required"}
            return app.acquire_token_silent_with_error(requested, account)

        try:
            result = await asyncio.to_thread(_run)
        except (OSError, RuntimeError, ValueError) as exc:
            raise IntegrationError(
                "Microsoft Entra ID token refresh failed", category=FailureCategory.TRANSIENT, retryable=True
            ) from exc
        if result is None:
            raise IntegrationError(
                "No cached credentials for the requested scopes; re-authentication is required",
                category=FailureCategory.AUTHORIZATION,
            )
        if "error" in result:
            raise map_msal_error(result, action="Token refresh failed")
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise IntegrationError("MSAL returned no access token", category=FailureCategory.AUTHORIZATION)
        return token


def new_token_cache(serialized: str | None = None) -> msal.SerializableTokenCache:
    """Create an `msal.SerializableTokenCache`, optionally loaded from a serialized state."""
    cache = msal.SerializableTokenCache()
    if serialized:
        cache.deserialize(serialized)
    return cache
