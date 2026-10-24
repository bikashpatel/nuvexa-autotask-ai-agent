from __future__ import annotations

import json
import uuid

import msal
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.errors import ConfigurationError, IntegrationError
from app.core.security import SecretBox
from app.db.models import CredentialRecord
from app.domain.enums import FailureCategory
from app.integrations.microsoft_graph.token_store import CredentialStore, MsalAccessTokenProvider
from tests.conftest import TEST_FERNET_KEY

REFRESH = "fake-refresh-token-value-0123456789"
CACHE_JSON = json.dumps({"RefreshToken": {"home-env-client": {"secret": REFRESH, "credential_type": "RefreshToken"}}})


@pytest.fixture
def store(session) -> CredentialStore:
    return CredentialStore(session, SecretBox([TEST_FERNET_KEY]))


async def test_roundtrip_is_encrypted_and_upserts(session, store, account):
    assert await store.load(account.id, tenant_id=account.tenant_id) is None
    await store.save(account.id, CACHE_JSON, tenant_id=account.tenant_id)
    await session.commit()

    rows = (await session.execute(select(CredentialRecord))).scalars().all()
    assert len(rows) == 1
    assert REFRESH.encode() not in rows[0].encrypted_payload
    assert b"RefreshToken" not in rows[0].encrypted_payload
    assert await store.load(account.id, tenant_id=account.tenant_id) == CACHE_JSON

    await store.save(account.id, "{}", tenant_id=account.tenant_id)
    await session.commit()
    assert len((await session.execute(select(CredentialRecord))).scalars().all()) == 1
    assert await store.load(account.id, tenant_id=account.tenant_id) == "{}"


async def test_tenant_scoping_and_delete(session, store, account):
    await store.save(account.id, CACHE_JSON, tenant_id=account.tenant_id)
    await session.commit()
    assert await store.load(account.id, tenant_id=uuid.uuid4()) is None
    assert await store.delete(account.id, tenant_id=uuid.uuid4()) is False
    assert await store.delete(account.id, tenant_id=account.tenant_id) is True
    await session.commit()
    assert await store.load(account.id, tenant_id=account.tenant_id) is None


async def test_wrong_key_cannot_decrypt(session, store, account):
    await store.save(account.id, CACHE_JSON, tenant_id=account.tenant_id)
    await session.commit()
    other = CredentialStore(session, SecretBox([Fernet.generate_key().decode()]))
    with pytest.raises(ConfigurationError):
        await other.load(account.id, tenant_id=account.tenant_id)


async def test_msal_cache_helpers_roundtrip(session, store, account):
    cache = msal.SerializableTokenCache()
    cache.deserialize(CACHE_JSON)
    await store.save_cache(account, cache)
    loaded = await store.load_cache(account)
    assert loaded is not None
    assert json.loads(loaded.serialize()) == json.loads(CACHE_JSON)


class FakeOAuth:
    def __init__(self, *, change_cache: bool, error: IntegrationError | None = None) -> None:
        self.change_cache = change_cache
        self.error = error
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def acquire_token_silent(self, scopes, home_account_id, cache):
        self.calls.append((tuple(scopes), home_account_id))
        if self.change_cache:
            cache.deserialize(json.dumps({"AccessToken": {"k": {"secret": "new-access-token"}}}))
            cache.has_state_changed = True
        if self.error:
            raise self.error
        return "new-access-token"


async def test_provider_persists_cache_only_when_changed(session, store, account):
    await store.save(account.id, CACHE_JSON, tenant_id=account.tenant_id)
    await session.commit()

    unchanged = FakeOAuth(change_cache=False)
    token = await MsalAccessTokenProvider(account, store, unchanged).get_access_token(("Mail.Read",))  # type: ignore[arg-type]
    assert token == "new-access-token"
    assert unchanged.calls == [(("Mail.Read",), "oid-1.tid-1")]
    assert await store.load(account.id, tenant_id=account.tenant_id) == CACHE_JSON

    changed = FakeOAuth(change_cache=True)
    await MsalAccessTokenProvider(account, store, changed).get_access_token(("Mail.Read",))  # type: ignore[arg-type]
    stored = await store.load(account.id, tenant_id=account.tenant_id)
    assert stored is not None
    assert "new-access-token" in stored


async def test_provider_requires_stored_credentials(store, account):
    with pytest.raises(IntegrationError) as info:
        await MsalAccessTokenProvider(account, store, FakeOAuth(change_cache=False)).get_access_token(  # type: ignore[arg-type]
            ("Mail.Read",)
        )
    assert info.value.category == FailureCategory.AUTHORIZATION


async def test_provider_propagates_reauth_errors(session, store, account):
    await store.save(account.id, CACHE_JSON, tenant_id=account.tenant_id)
    oauth = FakeOAuth(change_cache=False, error=IntegrationError("re-auth", category=FailureCategory.AUTHORIZATION))
    with pytest.raises(IntegrationError) as info:
        await MsalAccessTokenProvider(account, store, oauth).get_access_token(("Mail.Read",))  # type: ignore[arg-type]
    assert info.value.category == FailureCategory.AUTHORIZATION
