"""Browser sessions and Microsoft Entra ID flows through the real routes/services.

Only the MSAL `ConfidentialClientApplication` is faked (`tests/integration/fakes.FakeEntra`, injected via
`EntraOAuthClient(app_factory=...)`); state, browser binding, PKCE flow storage (encrypted), session issue /
rotation, account connection and the encrypted MSAL token cache all run for real.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select, update

from app.api.routes.auth import OAUTH_BINDING_COOKIE
from app.core.security import hash_token
from app.core.timeutil import utcnow
from app.db.models import ConnectedAccount, CredentialRecord, OAuthState, Tenant, User, UserSession
from app.domain.enums import ConnectionProvider, ConnectionStatus, OAuthPurpose, UserRole
from app.integrations.microsoft_graph.base import AZURE_DEVOPS_DELEGATED_SCOPES
from app.schemas.api import IntegrationsOut, MeOut
from tests.integration.conftest import API, Harness, HarnessFactory, assert_error
from tests.integration.fakes import FAKE_AUTHORIZE_HOST, FAKE_TENANT_ID, FakeEntra, FakeIdentity

BASE = "http://localhost:5173"
ENTRA: dict[str, Any] = {
    "entra_client_id": "00000000-0000-0000-0000-0000000c11e7",
    "entra_client_secret": "fake-client-secret-for-tests",
    "entra_tenant_id": "organizations",
    "entra_allowed_tenant_ids": [FAKE_TENANT_ID],
    "app_base_url": BASE,
    "oauth_redirect_uri": f"{BASE}/api/v1/auth/callback",
}


def cookie_attrs(resp: httpx.Response, name: str) -> dict[str, str]:
    """Attributes of the Set-Cookie header for `name` (lower-cased keys; flags map to "")."""
    for header in resp.headers.get_list("set-cookie"):
        first, *attrs = [part.strip() for part in header.split(";")]
        key, _, value = first.partition("=")
        if key == name:
            out = {"value": value.strip('"')}
            for attr in attrs:
                k, _, v = attr.partition("=")
                out[k.lower()] = v
            return out
    raise AssertionError(f"no Set-Cookie for {name}: {resp.headers.get_list('set-cookie')}")


def jar_value(client: httpx.AsyncClient, name: str) -> str | None:
    return next((c.value for c in client.cookies.jar if c.name == name), None)


@pytest.fixture
def entra(monkeypatch: pytest.MonkeyPatch) -> FakeEntra:
    return FakeEntra().install(monkeypatch)


async def _begin(client: httpx.AsyncClient, entra: FakeEntra, path: str = "/auth/login", **params: str) -> str:
    resp = await client.get(f"{API}{path}", params=params)
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith(FAKE_AUTHORIZE_HOST)
    return str(entra.flows_started[-1]["state"])


async def _callback(client: httpx.AsyncClient, **params: str) -> httpx.Response:
    resp = await client.get(f"{API}/auth/callback", params=params)
    assert resp.status_code == 302, resp.text
    return resp


async def entra_login(client: httpx.AsyncClient, entra: FakeEntra, redirect_to: str = "/inbox") -> httpx.Response:
    state = await _begin(client, entra, redirect_to=redirect_to)
    return await _callback(client, code="auth-code", state=state)


def _error_of(resp: httpx.Response) -> str | None:
    location = urlsplit(resp.headers["location"])
    return parse_qs(location.query).get("error", [None])[0]


# ---------------------------------------------------------------------------------------------------
# Demo login sessions and cookies
# ---------------------------------------------------------------------------------------------------


async def test_demo_login_sets_hardened_cookies(harness: Harness) -> None:
    resp = await harness.client.post(f"{API}/auth/demo-login", json={"persona": "member"})
    assert resp.status_code == 200
    ttl = str(harness.settings.session_ttl_hours * 3600)
    session = cookie_attrs(resp, harness.settings.session_cookie_name)
    csrf = cookie_attrs(resp, harness.settings.csrf_cookie_name)
    assert "httponly" in session
    assert "httponly" not in csrf  # the SPA must be able to read it
    for cookie in (session, csrf):
        assert (cookie["samesite"].lower(), cookie["path"], cookie["max-age"]) == ("lax", "/", ttl)
        assert "secure" not in cookie  # APP_ENV=test → not secure unless COOKIE_SECURE
        assert len(cookie["value"]) >= 32
    assert session["value"] != csrf["value"]
    async with harness.db.session() as s:
        row = (
            await s.execute(select(UserSession).where(UserSession.token_hash == hash_token(session["value"])))
        ).scalar_one()
    assert row.csrf_token_hash == hash_token(csrf["value"])  # only hashes are stored
    assert row.expires_at - row.created_at == timedelta(hours=harness.settings.session_ttl_hours)
    assert row.user_agent


async def test_cookies_are_secure_when_configured(make_harness: HarnessFactory) -> None:
    h = await make_harness(cookie_secure=True)
    resp = await h.client.post(f"{API}/auth/demo-login", json={"persona": "member"})
    assert "secure" in cookie_attrs(resp, h.settings.session_cookie_name)
    assert "secure" in cookie_attrs(resp, h.settings.csrf_cookie_name)


async def test_logout_revokes_the_session_and_clears_cookies(harness: Harness) -> None:
    member = await harness.persona("member")
    token = jar_value(member.client, harness.settings.session_cookie_name)
    assert token
    assert_error(await member.client.post(f"{API}/auth/logout"), 403, "csrf_failed")
    resp = await member.post("/auth/logout")
    assert resp.status_code == 204
    for name in (harness.settings.session_cookie_name, harness.settings.csrf_cookie_name):
        attrs = cookie_attrs(resp, name)
        assert attrs["value"] == ""
        assert attrs.get("max-age") == "0"
    replay = harness.tracked_client()
    replay.cookies.set(harness.settings.session_cookie_name, token)
    assert_error(await replay.get(f"{API}/auth/me"), 401, "authentication_required")
    async with harness.db.session() as s:
        row = (await s.execute(select(UserSession).where(UserSession.token_hash == hash_token(token)))).scalar_one()
    assert row.revoked_at is not None
    assert_error(await harness.client.post(f"{API}/auth/logout"), 401)


async def _session_row_update(h: Harness, token: str, **values: Any) -> None:
    async with h.db.session() as s:
        await s.execute(update(UserSession).where(UserSession.token_hash == hash_token(token)).values(**values))
        await s.commit()


async def test_absolute_expiry_idle_timeout_and_touch(harness: Harness) -> None:
    member = await harness.persona("member")
    token = jar_value(member.client, harness.settings.session_cookie_name)
    assert token
    # Seen 10 minutes ago: still valid, and last_seen_at is refreshed.
    await _session_row_update(harness, token, last_seen_at=utcnow() - timedelta(minutes=10))
    assert (await member.get("/auth/me")).status_code == 200
    async with harness.db.session() as s:
        row = (await s.execute(select(UserSession).where(UserSession.token_hash == hash_token(token)))).scalar_one()
    assert utcnow() - row.last_seen_at < timedelta(minutes=1)
    # Idle longer than SESSION_IDLE_MINUTES → rejected.
    idle = timedelta(minutes=harness.settings.session_idle_minutes + 1)
    await _session_row_update(harness, token, last_seen_at=utcnow() - idle)
    assert_error(await member.get("/auth/me"), 401, "authentication_required")
    # Fresh activity but past the absolute expiry → rejected.
    await _session_row_update(harness, token, last_seen_at=utcnow(), expires_at=utcnow() - timedelta(seconds=1))
    assert_error(await member.get("/auth/me"), 401, "authentication_required")


async def test_deactivated_users_and_garbage_tokens_are_rejected(harness: Harness) -> None:
    member = await harness.persona("member")
    async with harness.db.session() as s:
        await s.execute(update(User).where(User.id == member.user_id).values(is_active=False))
        await s.commit()
    assert_error(await member.get("/auth/me"), 401, "authentication_required")
    for bogus in ("x" * 201, "not-a-real-session-token"):
        client = harness.tracked_client()
        client.cookies.set(harness.settings.session_cookie_name, bogus)
        assert_error(await client.get(f"{API}/emails"), 401, "authentication_required")


async def test_csrf_token_is_bound_to_its_session(harness: Harness) -> None:
    member = await harness.persona("member")
    approver = await harness.persona("approver")
    resp = await member.client.post(f"{API}/emails/sync", json={}, headers=approver.headers)
    assert_error(resp, 403, "csrf_failed")
    assert (await member.post("/emails/sync", json={})).status_code == 202


# ---------------------------------------------------------------------------------------------------
# Microsoft Entra ID sign-in
# ---------------------------------------------------------------------------------------------------


async def test_entra_sign_in_flow(make_harness: HarnessFactory, entra: FakeEntra) -> None:
    h = await make_harness(**ENTRA)
    assert (await h.get("/auth/config")).json()["entra_login_enabled"] is True
    begin = await h.client.get(f"{API}/auth/login", params={"redirect_to": "/inbox"})
    assert begin.status_code == 302
    binding = cookie_attrs(begin, OAUTH_BINDING_COOKIE)
    assert "httponly" in binding
    assert (binding["path"], binding["max-age"], binding["samesite"].lower()) == ("/api/v1/auth/callback", "600", "lax")
    [flow] = entra.flows_started
    assert flow["scopes"] == ["User.Read"]
    assert flow["login_hint"] is None
    assert flow["redirect_uri"] == ENTRA["oauth_redirect_uri"]
    assert parse_qs(urlsplit(begin.headers["location"]).query)["state"] == [flow["state"]]
    async with h.db.session() as s:
        state_row = (await s.execute(select(OAuthState).where(OAuthState.state == flow["state"]))).scalar_one()
    assert (state_row.purpose, state_row.redirect_after, state_row.consumed_at) == (OAuthPurpose.LOGIN, "/inbox", None)
    assert state_row.browser_binding_hash == hash_token(binding["value"])
    assert b"code_verifier" not in state_row.flow_encrypted  # the PKCE flow is stored encrypted

    resp = await _callback(h.client, code="auth-code", state=flow["state"], session_state="s1")
    assert resp.headers["location"] == f"{BASE}/inbox"
    assert cookie_attrs(resp, OAUTH_BINDING_COOKIE)["value"] == ""
    assert "httponly" in cookie_attrs(resp, h.settings.session_cookie_name)
    me = MeOut.model_validate((await h.get("/auth/me")).json())
    assert (me.user.email, me.user.display_name, me.user.role) == (
        "casey.entra@contoso.example",
        "Casey Entra",
        UserRole.MEMBER,
    )
    assert me.user.tenant_name == "contoso.example"
    async with h.db.session() as s:
        tenant = await s.get(Tenant, me.user.tenant_id)
        user = await s.get(User, me.user.id)
        consumed = (await s.execute(select(OAuthState).where(OAuthState.state == flow["state"]))).scalar_one()
    assert tenant is not None
    assert tenant.entra_tenant_id == FAKE_TENANT_ID
    assert user is not None
    assert user.entra_object_id == entra.identity.oid
    assert consumed.consumed_at is not None
    [login] = await h.audit_events(event_type="login", actor_user_id=me.user.id)
    assert login.details["method"] == "entra"
    assert entra.redeemed == [{"code": "auth-code", "scopes": ["User.Read"]}]

    # Signing in again maps to the same tenant and user (matched by object id).
    second = h.tracked_client()
    await entra_login(second, entra)
    assert (await second.get(f"{API}/auth/me")).json()["user"]["id"] == str(me.user.id)


async def test_bootstrap_admin_emails_get_the_admin_role(make_harness: HarnessFactory, entra: FakeEntra) -> None:
    h = await make_harness(**ENTRA, bootstrap_admin_emails=["Casey.Entra@contoso.example"])
    await entra_login(h.client, entra)
    me = (await h.get("/auth/me")).json()
    assert me["user"]["role"] == "admin"
    assert "settings:write" in me["permissions"]


async def test_used_state_cannot_be_replayed(make_harness: HarnessFactory, entra: FakeEntra) -> None:
    h = await make_harness(**ENTRA)
    state = await _begin(h.client, entra)
    binding = jar_value(h.client, OAUTH_BINDING_COOKIE)
    assert binding
    assert _error_of(await _callback(h.client, code="c1", state=state)) is None
    attacker = h.tracked_client()
    attacker.cookies.set(OAUTH_BINDING_COOKIE, binding)
    replay = await _callback(attacker, code="c1", state=state)
    assert replay.headers["location"] == f"{BASE}/login?error=permission_denied"
    assert h.settings.session_cookie_name not in replay.headers.get("set-cookie", "")
    assert len(entra.redeemed) == 1  # the code was never redeemed twice


async def test_wrong_or_missing_binding_is_rejected_without_consuming_the_state(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**ENTRA)
    state = await _begin(h.client, entra)
    binding = jar_value(h.client, OAUTH_BINDING_COOKIE)
    other = h.tracked_client()
    assert _error_of(await _callback(other, code="c", state=state)) == "permission_denied"  # no binding cookie
    other.cookies.set(OAUTH_BINDING_COOKIE, "forged-binding-value")
    assert _error_of(await _callback(other, code="c", state=state)) == "permission_denied"
    assert entra.redeemed == []
    assert_error(await other.get(f"{API}/auth/me"), 401)
    # The legitimate browser can still finish its own sign-in.
    assert binding
    resp = await _callback(h.client, code="c", state=state)
    assert resp.headers["location"] == f"{BASE}/"  # no redirect_to was requested
    assert (await h.get("/auth/me")).status_code == 200


async def test_unknown_and_expired_states_are_rejected(make_harness: HarnessFactory, entra: FakeEntra) -> None:
    h = await make_harness(**ENTRA)
    assert _error_of(await _callback(h.client, code="c", state="never-issued")) == "permission_denied"
    assert _error_of(await _callback(h.client, code="c")) == "permission_denied"
    state = await _begin(h.client, entra)
    async with h.db.session() as s:
        await s.execute(
            update(OAuthState).where(OAuthState.state == state).values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await s.commit()
    assert _error_of(await _callback(h.client, code="c", state=state)) == "permission_denied"
    assert entra.redeemed == []


async def test_provider_error_redirects_to_login(make_harness: HarnessFactory, entra: FakeEntra) -> None:
    h = await make_harness(**ENTRA)
    state = await _begin(h.client, entra)
    resp = await _callback(h.client, error="access_denied", error_description="User cancelled", state=state)
    assert resp.headers["location"] == f"{BASE}/login?error=oauth_denied"
    assert cookie_attrs(resp, OAUTH_BINDING_COOKIE)["value"] == ""
    assert "User cancelled" not in resp.headers["location"]
    assert entra.redeemed == []


async def test_code_redemption_failure_consumes_the_state(make_harness: HarnessFactory, entra: FakeEntra) -> None:
    h = await make_harness(**ENTRA)
    entra.redeem_error = "invalid_grant"
    state = await _begin(h.client, entra)
    binding = jar_value(h.client, OAUTH_BINDING_COOKIE)
    resp = await _callback(h.client, code="expired-code", state=state)
    assert resp.headers["location"] == f"{BASE}/login?error=integration_error"
    assert "AADSTS" not in resp.headers["location"]
    entra.redeem_error = None
    retry = h.tracked_client()
    assert binding
    retry.cookies.set(OAUTH_BINDING_COOKIE, binding)
    assert _error_of(await _callback(retry, code="fresh-code", state=state)) == "permission_denied"
    async with h.db.session() as s:
        assert (await s.execute(select(User).where(User.email == "casey.entra@contoso.example"))).first() is None


async def test_accounts_from_tenants_outside_the_allow_list_are_refused(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**ENTRA)
    entra.identity = FakeIdentity(tid="99999999-9999-9999-9999-999999999999", email="eve@rival.example")
    resp = await entra_login(h.client, entra)
    assert _error_of(resp) == "integration_error"
    async with h.db.session() as s:
        assert (await s.execute(select(Tenant).where(Tenant.entra_tenant_id.is_not(None)))).first() is None
    assert_error(await h.get("/auth/me"), 401)


@pytest.mark.parametrize(
    ("redirect_to", "landing"),
    [
        ("/emails?status=open", "/emails?status=open"),
        ("https://evil.example/phish", "/"),
        ("//evil.example/phish", "/"),
        ("/\\evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("/inbox\r\nSet-Cookie: pwned=1", "/"),
        ("/" + "a" * 400, "/"),
    ],
)
async def test_post_login_redirect_is_always_same_site(
    make_harness: HarnessFactory, entra: FakeEntra, redirect_to: str, landing: str
) -> None:
    h = await make_harness(**ENTRA)
    resp = await entra_login(h.client, entra, redirect_to=redirect_to)
    assert resp.headers["location"] == f"{BASE}{landing}"


async def test_sign_in_rotates_an_existing_session(make_harness: HarnessFactory, entra: FakeEntra) -> None:
    h = await make_harness(**ENTRA)
    assert (await h.client.post(f"{API}/auth/demo-login", json={"persona": "member"})).status_code == 200
    before = jar_value(h.client, h.settings.session_cookie_name)
    assert before
    await entra_login(h.client, entra)
    after = jar_value(h.client, h.settings.session_cookie_name)
    assert after
    assert after != before
    assert (await h.get("/auth/me")).json()["user"]["email"] == "casey.entra@contoso.example"
    stale = h.tracked_client()
    stale.cookies.set(h.settings.session_cookie_name, before)
    assert_error(await stale.get(f"{API}/auth/me"), 401)


async def test_multi_tenant_authority_without_allow_list_is_refused(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**{**ENTRA, "entra_allowed_tenant_ids": []})
    err = assert_error(await h.get("/auth/login"), 500, "configuration_error")
    assert "ENTRA_ALLOWED_TENANT_IDS" in err["message"]
    assert entra.flows_started == []
    assert _error_of(await _callback(h.client, code="c", state="s")) == "configuration_error"


async def test_single_tenant_authority_needs_no_allow_list(make_harness: HarnessFactory, entra: FakeEntra) -> None:
    h = await make_harness(**{**ENTRA, "entra_tenant_id": FAKE_TENANT_ID, "entra_allowed_tenant_ids": []})
    resp = await entra_login(h.client, entra)
    assert resp.headers["location"] == f"{BASE}/inbox"


# ---------------------------------------------------------------------------------------------------
# Mailbox / Azure DevOps account connection
# ---------------------------------------------------------------------------------------------------


async def _connect(h: Harness, client: httpx.AsyncClient, entra: FakeEntra, path: str) -> httpx.Response:
    state = await _begin(client, entra, path=path)
    return await _callback(client, code="connect-code", state=state)


async def test_connect_mailbox_stores_encrypted_tokens(make_harness: HarnessFactory, entra: FakeEntra) -> None:
    h = await make_harness(**ENTRA, mail_mode="graph")
    await entra_login(h.client, entra)
    me = (await h.get("/auth/me")).json()["user"]
    resp = await _connect(h, h.client, entra, "/integrations/microsoft/connect")
    assert resp.headers["location"] == f"{BASE}/integrations"
    assert entra.flows_started[-1]["scopes"] == ["Mail.Read"]
    assert entra.flows_started[-1]["login_hint"] == me["email"]
    overview = await h.get("/integrations")
    raw = overview.text
    assert entra.access_token not in raw
    assert "refresh" not in raw.lower()
    out = IntegrationsOut.model_validate(overview.json())
    assert out.microsoft.entra_configured is True
    [conn] = out.microsoft.connections
    assert (conn.provider, conn.status, conn.email, conn.display_name) == (
        ConnectionProvider.MICROSOFT_GRAPH,
        ConnectionStatus.ACTIVE,
        "casey.entra@contoso.example",
        "Casey Entra",
    )
    assert conn.granted_scopes == ["Mail.Read"]
    async with h.db.session() as s:
        account = await s.get(ConnectedAccount, conn.id)
        record = (
            await s.execute(select(CredentialRecord).where(CredentialRecord.connected_account_id == conn.id))
        ).scalar_one()
    assert account is not None
    assert account.external_account_id == entra.identity.home_account_id
    assert entra.access_token.encode() not in record.encrypted_payload
    assert b"fake-refresh-token" not in record.encrypted_payload
    [event] = await h.audit_events(event_type="account_connected")
    assert (event.details["provider"], event.details["scopes"]) == ("microsoft_graph", ["Mail.Read"])
    assert entra.access_token not in str(event.details)

    # Reconnecting the same Microsoft account updates the row instead of duplicating it.
    await _connect(h, h.client, entra, "/integrations/microsoft/connect")
    again = IntegrationsOut.model_validate((await h.get("/integrations")).json())
    assert [c.id for c in again.microsoft.connections] == [conn.id]


async def test_connect_must_be_completed_by_the_user_who_started_it(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**ENTRA)
    victim = await h.persona("member")
    state = await _begin(victim.client, entra, path="/integrations/microsoft/connect")
    binding = jar_value(victim.client, OAUTH_BINDING_COOKIE)
    attacker = await h.persona("approver")
    assert binding
    attacker.client.cookies.set(OAUTH_BINDING_COOKIE, binding)
    resp = await attacker.client.get(f"{API}/auth/callback", params={"code": "c", "state": state})
    assert _error_of(resp) == "permission_denied"
    anonymous = h.tracked_client()
    assert_error(await anonymous.get(f"{API}/integrations/microsoft/connect"), 401)
    async with h.db.session() as s:
        graph_accounts = (
            await s.execute(
                select(ConnectedAccount).where(ConnectedAccount.provider == ConnectionProvider.MICROSOFT_GRAPH)
            )
        ).all()
    assert graph_accounts == []


async def test_connected_mailbox_must_belong_to_the_users_entra_tenant(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    other_tid = "77777777-7777-7777-7777-777777777777"
    h = await make_harness(**{**ENTRA, "entra_allowed_tenant_ids": [FAKE_TENANT_ID, other_tid]})
    await entra_login(h.client, entra)
    entra.identity = FakeIdentity(tid=other_tid, oid=str(uuid.uuid4()), email="someone@other.example")
    resp = await _connect(h, h.client, entra, "/integrations/microsoft/connect")
    assert _error_of(resp) == "permission_denied"
    assert (await h.get("/integrations")).json()["microsoft"]["connections"] == []


async def test_demo_member_can_connect_a_mailbox_and_disconnect_it(
    make_harness: HarnessFactory, entra: FakeEntra
) -> None:
    h = await make_harness(**ENTRA, mail_mode="graph")
    member = await h.persona("member")
    resp = await _connect(h, member.client, entra, "/integrations/microsoft/connect")
    assert resp.headers["location"] == f"{BASE}/integrations"
    conns = (await member.get("/integrations")).json()["microsoft"]["connections"]
    graph = next(c for c in conns if c["provider"] == "microsoft_graph")
    resp = await member.delete(f"/integrations/connections/{graph['id']}")
    assert resp.status_code == 204
    async with h.db.session() as s:
        account = await s.get(ConnectedAccount, uuid.UUID(graph["id"]))
        records = (
            await s.execute(select(CredentialRecord).where(CredentialRecord.connected_account_id == account.id))  # type: ignore[union-attr]
        ).all()
    assert account is not None
    assert account.status is ConnectionStatus.DISCONNECTED
    assert account.disconnected_at is not None
    assert records == []  # tokens are deleted, not just flagged
    conns = (await member.get("/integrations")).json()["microsoft"]["connections"]
    assert next(c for c in conns if c["id"] == graph["id"])["status"] == "disconnected"
    [event] = await h.audit_events(event_type="account_disconnected")
    assert event.details["provider"] == "microsoft_graph"
    assert_error(await member.post("/emails/sync", json={"connected_account_id": graph["id"]}), 409, "conflict")
    # Another user cannot disconnect someone else's connection.
    approver = await h.persona("approver")
    assert_error(await approver.delete(f"/integrations/connections/{graph['id']}"), 404, "not_found")


async def test_azure_devops_delegated_connection(make_harness: HarnessFactory, entra: FakeEntra) -> None:
    h = await make_harness(**ENTRA, azure_devops_mode="entra_delegated", azure_devops_organization="contoso")
    member = await h.persona("member")
    before = IntegrationsOut.model_validate((await member.get("/integrations")).json()).azure_devops
    assert (before.mode, before.organization, before.requires_user_connection, before.user_connected) == (
        "entra_delegated",
        "contoso",
        True,
        False,
    )
    check = (await member.get("/integrations/azure-devops/test")).json()
    assert check["ok"] is False
    assert "Connect your Azure DevOps account" in check["message"]
    assert_error(await member.get("/integrations/azure-devops/projects"), 409, "integration_not_configured")

    resp = await _connect(h, member.client, entra, "/integrations/azure-devops/connect")
    assert resp.headers["location"] == f"{BASE}/integrations"
    assert entra.flows_started[-1]["scopes"] == list(AZURE_DEVOPS_DELEGATED_SCOPES)
    after = IntegrationsOut.model_validate((await member.get("/integrations")).json())
    assert after.azure_devops.user_connected is True
    # The Azure DevOps grant is not a mailbox and is not listed as one.
    assert {c.provider for c in after.microsoft.connections} == {ConnectionProvider.DEMO_MAILBOX}
    async with h.db.session() as s:
        ado = (
            await s.execute(
                select(ConnectedAccount).where(ConnectedAccount.provider == ConnectionProvider.AZURE_DEVOPS)
            )
        ).scalar_one()
    assert ado.user_id == member.user_id
    assert ado.granted_scopes == list(AZURE_DEVOPS_DELEGATED_SCOPES)
