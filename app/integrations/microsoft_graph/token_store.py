"""Encrypted persistence of MSAL token caches (`credential_store`) and a silent access-token provider.

The serialized MSAL cache (refresh + access tokens) is wrapped in a small versioned envelope and encrypted
with `SecretBox` (Fernet). It is never logged and never leaves the backend. Writes are flushed, not
committed: the caller owns the transaction.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable

import msal
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConfigurationError, IntegrationError
from app.core.security import SecretBox
from app.db.models import ConnectedAccount, CredentialRecord
from app.domain.enums import FailureCategory
from app.integrations.microsoft_graph.oauth import EntraOAuthClient, new_token_cache
from app.observability.logging import get_logger

log = get_logger(__name__)

_ENVELOPE_VERSION = 1
_ENVELOPE_TYPE = "msal_token_cache"


class CredentialStore:
    """Tenant-scoped load/save/delete of the encrypted token cache of one connected account."""

    def __init__(self, session: AsyncSession, secret_box: SecretBox) -> None:
        self._session = session
        self._box = secret_box

    async def _record(self, connected_account_id: uuid.UUID, tenant_id: uuid.UUID) -> CredentialRecord | None:
        result = await self._session.execute(
            select(CredentialRecord).where(
                CredentialRecord.tenant_id == tenant_id,
                CredentialRecord.connected_account_id == connected_account_id,
            )
        )
        return result.scalar_one_or_none()

    async def load(self, connected_account_id: uuid.UUID, *, tenant_id: uuid.UUID) -> str | None:
        """Return the decrypted serialized MSAL cache, or None when nothing is stored."""
        record = await self._record(connected_account_id, tenant_id)
        if record is None:
            return None
        plaintext = self._box.decrypt_str(record.encrypted_payload)
        try:
            envelope = json.loads(plaintext)
        except ValueError as exc:
            raise ConfigurationError("Stored credential payload is malformed") from exc
        if (
            not isinstance(envelope, dict)
            or envelope.get("type") != _ENVELOPE_TYPE
            or envelope.get("v") != _ENVELOPE_VERSION
            or not isinstance(envelope.get("data"), str)
        ):
            raise ConfigurationError("Stored credential payload has an unsupported format")
        data: str = envelope["data"]
        return data

    async def save(self, connected_account_id: uuid.UUID, serialized_cache: str, *, tenant_id: uuid.UUID) -> None:
        """Encrypt and upsert the serialized cache (flushes; the caller commits)."""
        envelope = json.dumps({"v": _ENVELOPE_VERSION, "type": _ENVELOPE_TYPE, "data": serialized_cache})
        encrypted = self._box.encrypt_str(envelope)
        record = await self._record(connected_account_id, tenant_id)
        if record is None:
            self._session.add(
                CredentialRecord(
                    tenant_id=tenant_id, connected_account_id=connected_account_id, encrypted_payload=encrypted
                )
            )
        else:
            record.encrypted_payload = encrypted
        await self._session.flush()

    async def delete(self, connected_account_id: uuid.UUID, *, tenant_id: uuid.UUID) -> bool:
        """Remove the stored credentials (e.g. on disconnect). Returns True if a row was deleted."""
        result = await self._session.execute(
            delete(CredentialRecord)
            .where(
                CredentialRecord.tenant_id == tenant_id,
                CredentialRecord.connected_account_id == connected_account_id,
            )
            .returning(CredentialRecord.id)
        )
        deleted = result.first() is not None
        await self._session.flush()
        return deleted

    # Convenience wrappers around MSAL cache objects ----------------------------------------------
    async def load_cache(self, account: ConnectedAccount) -> msal.SerializableTokenCache | None:
        serialized = await self.load(account.id, tenant_id=account.tenant_id)
        return new_token_cache(serialized) if serialized is not None else None

    async def save_cache(self, account: ConnectedAccount, cache: msal.SerializableTokenCache) -> None:
        serialized: str = cache.serialize()
        await self.save(account.id, serialized, tenant_id=account.tenant_id)


class MsalAccessTokenProvider:
    """`AccessTokenProvider` for one connected Microsoft account.

    `account.external_account_id` must hold the MSAL `home_account_id` returned by
    `EntraOAuthClient.complete_flow` (`AuthResult.home_account_id`). The cache is persisted only when MSAL
    changed it (token refresh). Raises `IntegrationError(category="authorization")` when the user must
    sign in again; the caller marks the account `needs_reauth`.
    """

    def __init__(self, account: ConnectedAccount, store: CredentialStore, oauth: EntraOAuthClient) -> None:
        self._account = account
        self._store = store
        self._oauth = oauth

    async def get_access_token(self, scopes: Iterable[str]) -> str:
        cache = await self._store.load_cache(self._account)
        if cache is None:
            raise IntegrationError(
                "No stored credentials for this account; reconnect it", category=FailureCategory.AUTHORIZATION
            )
        try:
            return await self._oauth.acquire_token_silent(tuple(scopes), self._account.external_account_id, cache)
        finally:
            if cache.has_state_changed:
                await self._store.save_cache(self._account, cache)
                log.info("token_cache_updated", connected_account_id=str(self._account.id))
