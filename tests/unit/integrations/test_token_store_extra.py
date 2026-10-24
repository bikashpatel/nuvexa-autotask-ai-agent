"""Credential store envelope validation, key rotation and the cache-persistence rules of the token provider."""

from __future__ import annotations

import json
from typing import Any

import msal
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.errors import ConfigurationError, IntegrationError
from app.core.security import SecretBox
from app.db.models import CredentialRecord
from app.domain.enums import FailureCategory
from app.integrations.microsoft_graph.token_store import CredentialStore, MsalAccessTokenProvider
from tests.unit.integrations.test_token_store import CACHE_JSON, REFRESH

OLD_KEY = Fernet.generate_key().decode()
NEW_KEY = Fernet.generate_key().decode()


async def _store_raw(session, acct, plaintext: str, key: str = OLD_KEY) -> None:
    session.add(
        CredentialRecord(
            tenant_id=acct.tenant_id,
            connected_account_id=acct.id,
            encrypted_payload=SecretBox([key]).encrypt_str(plaintext),
        )
    )
    await session.flush()


@pytest.mark.parametrize(
    ("plaintext", "message"),
    [
        ("not json at all", "malformed"),
        ("[]", "unsupported format"),
        (json.dumps({"v": 1, "type": "other", "data": "{}"}), "unsupported format"),
        (json.dumps({"v": 2, "type": "msal_token_cache", "data": "{}"}), "unsupported format"),
        (json.dumps({"v": "1", "type": "msal_token_cache", "data": "{}"}), "unsupported format"),
        (json.dumps({"v": 1, "type": "msal_token_cache", "data": {"RefreshToken": {}}}), "unsupported format"),
        (json.dumps({"v": 1, "type": "msal_token_cache"}), "unsupported format"),
    ],
)
async def test_unexpected_envelopes_are_configuration_errors(session, account, plaintext: str, message: str) -> None:
    await _store_raw(session, account, plaintext)
    with pytest.raises(ConfigurationError, match=message):
        await CredentialStore(session, SecretBox([OLD_KEY])).load(account.id, tenant_id=account.tenant_id)


async def test_tampered_ciphertext_is_rejected(session, account) -> None:
    store = CredentialStore(session, SecretBox([OLD_KEY]))
    await store.save(account.id, CACHE_JSON, tenant_id=account.tenant_id)
    record = (await session.execute(select(CredentialRecord))).scalar_one()
    record.encrypted_payload = record.encrypted_payload[:-2] + b"AA"
    await session.flush()
    with pytest.raises(ConfigurationError, match="cannot be decrypted"):
        await store.load(account.id, tenant_id=account.tenant_id)


async def test_payload_is_a_versioned_fernet_envelope(session, account) -> None:
    await CredentialStore(session, SecretBox([OLD_KEY])).save(account.id, CACHE_JSON, tenant_id=account.tenant_id)
    record = (await session.execute(select(CredentialRecord))).scalar_one()
    assert record.encrypted_payload.startswith(b"gAAAAA")  # Fernet token, version byte 0x80
    assert REFRESH.encode() not in record.encrypted_payload
    envelope = json.loads(SecretBox([OLD_KEY]).decrypt_str(record.encrypted_payload))
    assert envelope == {"v": 1, "type": "msal_token_cache", "data": CACHE_JSON}


async def test_key_rotation_decrypts_old_payloads_and_reencrypts_with_the_new_key(session, account) -> None:
    await CredentialStore(session, SecretBox([OLD_KEY])).save(account.id, CACHE_JSON, tenant_id=account.tenant_id)
    rotated = CredentialStore(session, SecretBox([NEW_KEY, OLD_KEY]))
    assert await rotated.load(account.id, tenant_id=account.tenant_id) == CACHE_JSON
    await rotated.save(account.id, CACHE_JSON, tenant_id=account.tenant_id)
    new_only = CredentialStore(session, SecretBox([NEW_KEY]))
    assert await new_only.load(account.id, tenant_id=account.tenant_id) == CACHE_JSON
    with pytest.raises(ConfigurationError):
        await CredentialStore(session, SecretBox([OLD_KEY])).load(account.id, tenant_id=account.tenant_id)


async def test_load_cache_returns_none_when_nothing_is_stored(session, account) -> None:
    assert await CredentialStore(session, SecretBox([OLD_KEY])).load_cache(account) is None


class ScriptedOAuth:
    """Mutates the MSAL cache like a refresh would, optionally failing afterwards."""

    def __init__(self, *, mutate: bool, error: Exception | None = None) -> None:
        self.mutate = mutate
        self.error = error
        self.scopes: list[tuple[str, ...]] = []

    async def acquire_token_silent(self, scopes: Any, home_account_id: str, cache: msal.SerializableTokenCache) -> str:
        self.scopes.append(scopes)
        if self.mutate:
            cache.deserialize(json.dumps({"AccessToken": {"k": {"secret": "rotated-refresh"}}}))
            cache.has_state_changed = True
        if self.error is not None:
            raise self.error
        return "access-token"


async def test_changed_cache_is_persisted_even_when_acquisition_fails(session, account) -> None:
    store = CredentialStore(session, SecretBox([OLD_KEY]))
    await store.save(account.id, CACHE_JSON, tenant_id=account.tenant_id)
    oauth = ScriptedOAuth(mutate=True, error=IntegrationError("re-auth", category=FailureCategory.AUTHORIZATION))
    with pytest.raises(IntegrationError):
        await MsalAccessTokenProvider(account, store, oauth).get_access_token(s for s in ["Mail.Read"])  # type: ignore[arg-type]
    stored = await store.load(account.id, tenant_id=account.tenant_id)
    assert stored is not None
    assert "rotated-refresh" in stored  # the refreshed tokens are not lost
    assert oauth.scopes == [("Mail.Read",)]  # iterables are materialized as a tuple


async def test_unchanged_cache_is_not_rewritten(session, account) -> None:
    store = CredentialStore(session, SecretBox([OLD_KEY]))
    await store.save(account.id, CACHE_JSON, tenant_id=account.tenant_id)
    before = (await session.execute(select(CredentialRecord))).scalar_one().encrypted_payload
    oauth = ScriptedOAuth(mutate=False, error=IntegrationError("down", category=FailureCategory.TRANSIENT))
    with pytest.raises(IntegrationError):
        await MsalAccessTokenProvider(account, store, oauth).get_access_token(("Mail.Read",))  # type: ignore[arg-type]
    after = (await session.execute(select(CredentialRecord))).scalar_one().encrypted_payload
    assert after == before  # Fernet tokens are randomized, so equality proves no rewrite happened


async def test_undecryptable_credentials_surface_before_any_token_call(session, account) -> None:
    await CredentialStore(session, SecretBox([OLD_KEY])).save(account.id, CACHE_JSON, tenant_id=account.tenant_id)
    oauth = ScriptedOAuth(mutate=False)
    with pytest.raises(ConfigurationError):
        await MsalAccessTokenProvider(account, CredentialStore(session, SecretBox([NEW_KEY])), oauth).get_access_token(  # type: ignore[arg-type]
            ("Mail.Read",)
        )
    assert oauth.scopes == []
