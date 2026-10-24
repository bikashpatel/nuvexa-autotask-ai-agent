from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

import msal
import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    AuthenticationRequired,
    ConfigurationError,
    Conflict,
    IntegrationError,
    NotFound,
    PermissionDenied,
)
from app.core.security import SecretBox, hash_token
from app.core.timeutil import utcnow
from app.db.models import ConnectedAccount, CredentialRecord, OAuthState, Tenant, User, UserSession
from app.demo import DEMO_PERSONAS, DEMO_TENANT
from app.domain.enums import (
    AuditOutcome,
    ConnectionProvider,
    ConnectionStatus,
    FailureCategory,
    OAuthPurpose,
    UserRole,
)
from app.integrations.microsoft_graph.base import (
    AZURE_DEVOPS_DELEGATED_SCOPES,
    GRAPH_LOGIN_SCOPES,
    GRAPH_MAIL_SCOPES,
)
from app.integrations.microsoft_graph.oauth import AuthResult, EntraOAuthClient
from app.integrations.microsoft_graph.token_store import CredentialStore
from app.services.auth_service import (
    DemoAuthService,
    EntraAuthService,
    ResolvedSession,
    SessionService,
    disconnect_account,
    safe_redirect_path,
)
from tests.conftest import make_settings
from tests.unit.services.conftest import Factory, Fresh, World

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "33333333-3333-3333-3333-333333333333"
OID = "22222222-2222-2222-2222-222222222222"
HOME = f"{OID}.{TENANT_A}"
LOGIN_EMAIL = "pat.user@contoso.example"
VERIFIER = "pkce-verifier-must-stay-encrypted"
REFRESH_TOKEN = "fake-refresh-token-for-unit-tests"
OLD_REFRESH_TOKEN = "old-fake-refresh-token"
IP = "203.0.113.9"


# ---------------------------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------------------------


def auth_result(**overrides: Any) -> AuthResult:
    values: dict[str, Any] = {
        "oid": OID,
        "tid": TENANT_A,
        "home_account_id": HOME,
        "email": "Pat.User@Contoso.example",
        "name": "Pat User",
        "granted_scopes": ["Mail.Read", "User.Read"],
    }
    values.update(overrides)
    return AuthResult(**values)


class FakeOAuth(EntraOAuthClient):
    """EntraOAuthClient without MSAL: records calls, writes fake tokens into the caller's real token cache."""

    def __init__(
        self, settings: Settings, result: AuthResult | None = None, error: BaseException | None = None
    ) -> None:
        super().__init__(settings)
        self.result = result or auth_result()
        self.error = error
        self.flows: list[dict[str, Any]] = []
        self.begin_calls: list[tuple[list[str], str | None]] = []
        self.complete_calls: list[tuple[dict[str, Any], dict[str, str], str]] = []

    async def begin_flow(
        self, scopes: Iterable[str], *, login_hint: str | None = None, prompt: str | None = None
    ) -> dict[str, Any]:
        self.begin_calls.append((list(scopes), login_hint))
        state = f"state-{uuid.uuid4().hex}"
        flow = {
            "state": state,
            "auth_uri": f"https://login.example.invalid/authorize?state={state}",
            "code_verifier": VERIFIER,
            "nonce": "nonce-1",
            "scope": list(scopes),
        }
        self.flows.append(flow)
        return flow

    async def complete_flow(
        self, flow: dict[str, Any], auth_response: dict[str, str], cache: msal.SerializableTokenCache
    ) -> AuthResult:
        self.complete_calls.append((flow, dict(auth_response), cache.serialize()))
        if self.error is not None:
            raise self.error
        state = json.loads(cache.serialize() or "{}")
        state.setdefault("RefreshToken", {})["new"] = {"secret": REFRESH_TOKEN}
        cache.deserialize(json.dumps(state))
        return self.result


@pytest.fixture
def entra_settings(database_url: str) -> Settings:
    return make_settings(
        database_url,
        entra_client_id="00000000-0000-0000-0000-000000000001",
        entra_client_secret="not-a-real-secret",
        entra_tenant_id=TENANT_A,
    )


def _service(settings: Settings, **kw: Any) -> tuple[EntraAuthService, FakeOAuth]:
    oauth = FakeOAuth(settings, **kw)
    return EntraAuthService(settings, oauth), oauth


async def _begin_flow(
    svc: EntraAuthService,
    session: AsyncSession,
    purpose: OAuthPurpose = OAuthPurpose.LOGIN,
    user: User | None = None,
    redirect_to: str | None = "/done",
) -> tuple[dict[str, str], str]:
    """Start a flow; returns (callback params, browser-binding token)."""
    _url, binding = await svc.begin(session, purpose, redirect_to=redirect_to, user=user)
    oauth = svc.oauth
    assert isinstance(oauth, FakeOAuth)
    return {"state": oauth.flows[-1]["state"], "code": "auth-code"}, binding


async def _state_row(session: AsyncSession, state: str) -> OAuthState:
    row = (await session.execute(select(OAuthState).where(OAuthState.state == state))).scalar_one()
    return row


async def _store_cache(session: AsyncSession, settings: Settings, account: ConnectedAccount, secret: str) -> None:
    store = CredentialStore(session, SecretBox.from_settings(settings))
    await store.save(account.id, json.dumps({"RefreshToken": {"old": {"secret": secret}}}), tenant_id=account.tenant_id)
    await session.commit()


async def _graph_accounts(fresh: Fresh, user: User, provider: ConnectionProvider) -> list[ConnectedAccount]:
    return await fresh.all(ConnectedAccount, ConnectedAccount.user_id == user.id, ConnectedAccount.provider == provider)


# ---------------------------------------------------------------------------------------------
# safe_redirect_path
# ---------------------------------------------------------------------------------------------

SAFE_CHARS = "ABCXYZabcxyz0189/_-?=&.%"


@pytest.mark.parametrize(
    "value",
    ["/", "/emails", "/emails?status=pending&page=2", "/a/b-c_d.e", "/settings%20x", "/a//b", "/%2F%2Fevil.example"],
)
def test_safe_redirect_accepts_same_site_paths(value: str) -> None:
    assert safe_redirect_path(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "//evil.example.com",
        "///evil.example.com",
        "https://evil.example.com",
        "http:/evil.example.com",
        "javascript:alert(1)",
        "\\\\evil.example.com",
        "/\\evil.example.com",
        "evil.example.com",
        "/path with space",
        "/foo#fragment",
        "/@evil.example.com",
        "/emails\r\n",
        "/emails\nSet-Cookie: a=b",
        "/emails\t",
        "/" + "a" * 300,
    ],
)
def test_safe_redirect_rejects_open_redirect_attempts(value: str | None) -> None:
    assert safe_redirect_path(value) == "/"
    assert safe_redirect_path(value, default="/home") == "/home"


def test_safe_redirect_trailing_newline_is_rejected_regression() -> None:
    # `$` used to match before a trailing "\n", letting "/emails\n" through into the Location header.
    assert safe_redirect_path("/emails\n") == "/"
    assert safe_redirect_path("/\n") == "/"


def test_safe_redirect_length_boundary() -> None:
    at_limit = "/" + "a" * 299
    assert safe_redirect_path(at_limit) == at_limit
    assert safe_redirect_path(at_limit + "a") == "/"


@hyp_settings(max_examples=300, derandomize=True, database=None)
@given(st.one_of(st.text(), st.text(alphabet=SAFE_CHARS + "\\:@#/ \n\r\t"), st.text(alphabet=SAFE_CHARS)))
def test_safe_redirect_output_is_always_a_single_slash_relative_path(value: str) -> None:
    result = safe_redirect_path(value, default="/fallback")
    if result != "/fallback":
        assert result == value
        assert result.startswith("/")
        assert not result.startswith("//")
        assert len(result) <= 300
        assert set(result) <= set(SAFE_CHARS)


@hyp_settings(max_examples=200, derandomize=True, database=None)
@given(
    st.text(alphabet=SAFE_CHARS, max_size=50),
    st.sampled_from(["//", "https://", "http://", "\\\\", "/\\", "javascript:", "data:", " /", "\n/"]),
)
def test_safe_redirect_rejects_any_scheme_or_protocol_relative_prefix(suffix: str, prefix: str) -> None:
    assert safe_redirect_path(prefix + suffix) == "/"


@hyp_settings(max_examples=200, derandomize=True, database=None)
@given(st.text(alphabet=SAFE_CHARS, max_size=299), st.sampled_from(["\n", "\r", "\\", " ", ":", "@", "\x00"]))
def test_safe_redirect_rejects_paths_with_unsafe_characters(path: str, bad: str) -> None:
    assert safe_redirect_path("/" + path.lstrip("/") + bad) == "/"


@hyp_settings(max_examples=200, derandomize=True, database=None)
@given(st.text(alphabet=SAFE_CHARS, max_size=299).filter(lambda s: not s.startswith("/")))
def test_safe_redirect_keeps_every_safe_path_unchanged(path: str) -> None:
    assert safe_redirect_path("/" + path) == "/" + path


# ---------------------------------------------------------------------------------------------
# SessionService
# ---------------------------------------------------------------------------------------------


async def _issue(session: AsyncSession, settings: Settings, user: User) -> tuple[str, str, UserSession]:
    issued = await SessionService(settings).issue(session, user, ip=IP, user_agent="pytest-agent")
    await session.commit()
    row = (
        await session.execute(select(UserSession).where(UserSession.token_hash == hash_token(issued.token)))
    ).scalar_one()
    return issued.token, issued.csrf_token, row


async def test_issue_stores_only_hashes_and_updates_last_login(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh
) -> None:
    before = world.owner.last_login_at
    issued = await SessionService(settings).issue(session, world.owner, ip=IP, user_agent="UA" * 300)
    await session.commit()

    [row] = await fresh.all(UserSession, UserSession.user_id == world.owner.id)
    assert row.token_hash == hash_token(issued.token) != issued.token
    assert row.csrf_token_hash == hash_token(issued.csrf_token) != issued.csrf_token
    assert issued.token != issued.csrf_token
    assert row.tenant_id == world.tenant.id
    assert row.expires_at - row.created_at == timedelta(hours=settings.session_ttl_hours)
    assert row.last_seen_at == row.created_at
    assert row.revoked_at is None
    assert row.ip_address == IP
    assert row.user_agent == ("UA" * 300)[:400]
    assert issued.expires_at_seconds == settings.session_ttl_hours * 3600
    user = await fresh.get(User, world.owner.id)
    assert user is not None
    assert user.last_login_at == row.created_at
    assert user.last_login_at != before


@pytest.mark.parametrize("agent", [None, ""])
async def test_issue_without_user_agent_stores_null(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh, agent: str | None
) -> None:
    await SessionService(settings).issue(session, world.owner, ip=None, user_agent=agent)
    await session.commit()
    [row] = await fresh.all(UserSession, UserSession.user_id == world.owner.id)
    assert row.user_agent is None
    assert row.ip_address is None


async def test_resolve_valid_session_returns_user_and_tenant_without_touching(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh
) -> None:
    token, _csrf, row = await _issue(session, settings, world.owner)
    seen = row.last_seen_at
    resolved = await SessionService(settings).resolve(session, token)
    assert resolved is not None
    assert resolved.session.id == row.id
    assert resolved.user.id == world.owner.id
    assert resolved.tenant.id == world.tenant.id
    stored = await fresh.get(UserSession, row.id)
    assert stored is not None
    assert stored.last_seen_at == seen  # touched at most once a minute


@pytest.mark.parametrize("token", [None, "", "unknown-token"])
async def test_resolve_missing_or_unknown_token(
    session: AsyncSession, settings: Settings, world: World, token: str | None
) -> None:
    await _issue(session, settings, world.owner)
    assert await SessionService(settings).resolve(session, token) is None


async def test_resolve_rejects_oversized_token_before_lookup(
    session: AsyncSession, settings: Settings, world: World
) -> None:
    now = utcnow()
    for token in ("x" * 200, "y" * 201):
        session.add(
            UserSession(
                user_id=world.owner.id,
                tenant_id=world.tenant.id,
                token_hash=hash_token(token),
                csrf_token_hash=hash_token("csrf"),
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
    await session.commit()
    service = SessionService(settings)
    assert await service.resolve(session, "x" * 200) is not None  # boundary: 200 characters is accepted
    assert await service.resolve(session, "y" * 201) is None


@pytest.mark.parametrize(
    ("field", "delta"),
    [
        ("revoked_at", timedelta(seconds=-1)),
        ("expires_at", timedelta(seconds=-1)),
        ("expires_at", timedelta(0)),
    ],
)
async def test_resolve_rejects_revoked_and_expired(
    session: AsyncSession, settings: Settings, world: World, field: str, delta: timedelta
) -> None:
    token, _csrf, row = await _issue(session, settings, world.owner)
    setattr(row, field, utcnow() + delta)
    await session.commit()
    assert await SessionService(settings).resolve(session, token) is None


async def test_resolve_rejects_idle_session(session: AsyncSession, settings: Settings, world: World) -> None:
    token, _csrf, row = await _issue(session, settings, world.owner)
    row.last_seen_at = utcnow() - timedelta(minutes=settings.session_idle_minutes, seconds=5)
    await session.commit()
    assert await SessionService(settings).resolve(session, token) is None


async def test_resolve_rejects_inactive_user(session: AsyncSession, settings: Settings, world: World) -> None:
    token, _csrf, _row = await _issue(session, settings, world.owner)
    world.owner.is_active = False
    await session.commit()
    assert await SessionService(settings).resolve(session, token) is None


async def test_resolve_touches_last_seen_after_a_minute(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh
) -> None:
    token, _csrf, row = await _issue(session, settings, world.owner)
    stale = utcnow() - timedelta(seconds=90)
    row.last_seen_at = stale
    await session.commit()
    assert await SessionService(settings).resolve(session, token) is not None
    stored = await fresh.get(UserSession, row.id)
    assert stored is not None
    assert stored.last_seen_at > stale + timedelta(seconds=60)  # committed by resolve


async def test_resolve_does_not_touch_within_a_minute(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh
) -> None:
    token, _csrf, row = await _issue(session, settings, world.owner)
    recent = utcnow() - timedelta(seconds=30)
    row.last_seen_at = recent
    await session.commit()
    assert await SessionService(settings).resolve(session, token) is not None
    stored = await fresh.get(UserSession, row.id)
    assert stored is not None
    assert stored.last_seen_at == recent


async def test_csrf_valid_only_for_the_issued_token(session: AsyncSession, settings: Settings, world: World) -> None:
    token, csrf, _row = await _issue(session, settings, world.owner)
    resolved = await SessionService(settings).resolve(session, token)
    assert resolved is not None
    assert SessionService.csrf_valid(resolved, csrf) is True
    assert SessionService.csrf_valid(resolved, csrf + "x") is False
    assert SessionService.csrf_valid(resolved, token) is False  # the session token is not the CSRF token
    assert SessionService.csrf_valid(resolved, "") is False
    assert SessionService.csrf_valid(resolved, None) is False


async def test_revoke_commits_and_invalidates_session(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh
) -> None:
    token, _csrf, row = await _issue(session, settings, world.owner)
    service = SessionService(settings)
    resolved = await service.resolve(session, token)
    assert isinstance(resolved, ResolvedSession)
    await service.revoke(session, resolved)
    stored = await fresh.get(UserSession, row.id)
    assert stored is not None
    assert stored.revoked_at is not None
    assert await service.resolve(session, token) is None


# ---------------------------------------------------------------------------------------------
# DemoAuthService
# ---------------------------------------------------------------------------------------------


# Production settings must be otherwise valid (they require an audit HMAC key); demo login is what's under test.
_PRODUCTION = {"app_env": "production", "audit_hmac_key": "not-a-real-audit-hmac-key-for-tests-only"}


@pytest.mark.parametrize("overrides", [{"demo_mode": False}, _PRODUCTION])
async def test_demo_login_disabled(
    session: AsyncSession, database_url: str, fresh: Fresh, overrides: dict[str, Any]
) -> None:
    disabled = make_settings(database_url, **overrides)
    with pytest.raises(PermissionDenied, match="disabled"):
        await DemoAuthService(disabled).login(session, "member")
    assert await fresh.all(Tenant, Tenant.slug == DEMO_TENANT.slug) == []


async def test_demo_login_unknown_persona(session: AsyncSession, settings: Settings, fresh: Fresh) -> None:
    with pytest.raises(PermissionDenied, match="Unknown demo persona"):
        await DemoAuthService(settings).login(session, "root")
    assert await fresh.all(Tenant, Tenant.slug == DEMO_TENANT.slug) == []


async def test_demo_login_provisions_tenant_user_and_mailbox(
    session: AsyncSession, settings: Settings, fresh: Fresh
) -> None:
    user = await DemoAuthService(settings).login(session, "member")
    await session.commit()

    persona = DEMO_PERSONAS["member"]
    [tenant] = await fresh.all(Tenant, Tenant.slug == DEMO_TENANT.slug)
    assert tenant.is_demo is True
    assert tenant.name == DEMO_TENANT.name
    stored = await fresh.get(User, user.id)
    assert stored is not None
    assert stored.tenant_id == tenant.id
    assert stored.email == persona.email.lower()
    assert stored.display_name == persona.display_name
    assert stored.role is UserRole.MEMBER
    assert stored.is_active is True
    [account] = await fresh.all(ConnectedAccount, ConnectedAccount.user_id == user.id)
    assert account.provider is ConnectionProvider.DEMO_MAILBOX
    assert account.external_account_id == "demo:member"
    assert account.email == persona.email.lower()
    assert account.display_name == f"{persona.display_name} (demo)"
    assert account.granted_scopes == ["demo.mailbox.read"]
    assert account.status is ConnectionStatus.ACTIVE
    assert account.connected_at is not None
    [audit] = await fresh.audits("login", actor_user_id=user.id)
    assert audit.tenant_id == tenant.id
    assert audit.resource_type == "user"
    assert audit.resource_id == str(user.id)
    assert audit.details == {"method": "demo", "persona": "member"}
    assert audit.outcome is AuditOutcome.SUCCESS


async def test_demo_login_is_idempotent(session: AsyncSession, settings: Settings, fresh: Fresh) -> None:
    service = DemoAuthService(settings)
    first = await service.login(session, "member")
    await session.commit()
    second = await service.login(session, "member")
    await session.commit()

    assert second.id == first.id
    assert len(await fresh.all(Tenant, Tenant.slug == DEMO_TENANT.slug)) == 1
    assert len(await fresh.all(User, User.email == DEMO_PERSONAS["member"].email.lower())) == 1
    assert len(await fresh.all(ConnectedAccount, ConnectedAccount.user_id == first.id)) == 1
    assert len(await fresh.audits("login", actor_user_id=first.id)) == 2  # every login is audited


@pytest.mark.parametrize(
    ("persona", "role", "has_mailbox"),
    [
        ("admin", UserRole.ADMIN, True),
        ("approver", UserRole.APPROVER, True),
        ("member", UserRole.MEMBER, True),
        ("viewer", UserRole.VIEWER, False),
    ],
)
async def test_demo_personas_roles_and_mailboxes(
    session: AsyncSession,
    settings: Settings,
    fresh: Fresh,
    persona: str,
    role: UserRole,
    has_mailbox: bool,
) -> None:
    user = await DemoAuthService(settings).login(session, persona)
    await session.commit()
    assert user.role is role
    accounts = await fresh.all(ConnectedAccount, ConnectedAccount.user_id == user.id)
    assert len(accounts) == (1 if has_mailbox else 0)


async def test_demo_personas_share_one_demo_tenant(session: AsyncSession, settings: Settings, fresh: Fresh) -> None:
    service = DemoAuthService(settings)
    member = await service.login(session, "member")
    approver = await service.login(session, "approver")
    await session.commit()
    assert member.tenant_id == approver.tenant_id
    assert len(await fresh.all(Tenant, Tenant.slug == DEMO_TENANT.slug)) == 1


# ---------------------------------------------------------------------------------------------
# EntraAuthService: configuration and begin
# ---------------------------------------------------------------------------------------------


def test_oauth_client_is_built_lazily_once(entra_settings: Settings) -> None:
    service = EntraAuthService(entra_settings)
    client = service.oauth
    assert isinstance(client, EntraOAuthClient)
    assert service.oauth is client
    injected = FakeOAuth(entra_settings)
    assert EntraAuthService(entra_settings, injected).oauth is injected


async def test_begin_and_complete_require_entra_configuration(session: AsyncSession, settings: Settings) -> None:
    service = EntraAuthService(settings, FakeOAuth(settings))
    with pytest.raises(ConfigurationError, match="not configured"):
        await service.begin(session, OAuthPurpose.LOGIN, redirect_to=None, user=None)
    with pytest.raises(ConfigurationError):
        await service.complete(session, {"state": "s"}, binding="b", current_user=None, ip=None)


@pytest.mark.parametrize("tenant_id", ["common", "organizations", "Organizations"])
async def test_multi_tenant_authority_requires_allow_list(
    session: AsyncSession, database_url: str, fresh: Fresh, tenant_id: str
) -> None:
    open_settings = make_settings(
        database_url, entra_client_id="client", entra_client_secret="not-a-real-secret", entra_tenant_id=tenant_id
    )
    service, _oauth = _service(open_settings)
    with pytest.raises(ConfigurationError, match="ENTRA_ALLOWED_TENANT_IDS"):
        await service.begin(session, OAuthPurpose.LOGIN, redirect_to=None, user=None)
    assert await fresh.all(OAuthState) == []

    restricted = make_settings(
        database_url,
        entra_client_id="client",
        entra_client_secret="not-a-real-secret",
        entra_tenant_id=tenant_id,
        entra_allowed_tenant_ids=TENANT_A,
    )
    service, _oauth = _service(restricted)
    url, _binding = await service.begin(session, OAuthPurpose.LOGIN, redirect_to=None, user=None)
    assert url.startswith("https://login.example.invalid/")


async def test_begin_login_persists_encrypted_state(
    session: AsyncSession, entra_settings: Settings, fresh: Fresh
) -> None:
    service, oauth = _service(entra_settings)
    url, binding = await service.begin(session, OAuthPurpose.LOGIN, redirect_to="/emails?tab=new", user=None)

    flow = oauth.flows[0]
    assert url == flow["auth_uri"]
    assert oauth.begin_calls == [(list(GRAPH_LOGIN_SCOPES), None)]
    [row] = await fresh.all(OAuthState)  # committed by begin
    assert row.state == flow["state"]
    assert row.purpose is OAuthPurpose.LOGIN
    assert row.user_id is None
    assert row.browser_binding_hash == hash_token(binding)
    assert binding not in row.browser_binding_hash
    assert VERIFIER.encode() not in row.flow_encrypted
    assert json.loads(SecretBox.from_settings(entra_settings).decrypt_str(row.flow_encrypted)) == flow
    assert row.redirect_after == "/emails?tab=new"
    assert row.expires_at - row.created_at == timedelta(minutes=10)
    assert row.consumed_at is None


async def test_begin_sanitizes_unsafe_redirect(session: AsyncSession, entra_settings: Settings, fresh: Fresh) -> None:
    service, _oauth = _service(entra_settings)
    await service.begin(session, OAuthPurpose.LOGIN, redirect_to="https://evil.example.com/x", user=None)
    [row] = await fresh.all(OAuthState)
    assert row.redirect_after == "/"


@pytest.mark.parametrize(
    ("purpose", "scopes"),
    [
        (OAuthPurpose.CONNECT_MAILBOX, GRAPH_MAIL_SCOPES),
        (OAuthPurpose.CONNECT_AZURE_DEVOPS, AZURE_DEVOPS_DELEGATED_SCOPES),
    ],
)
async def test_begin_connect_binds_user_scopes_and_login_hint(
    session: AsyncSession,
    entra_settings: Settings,
    world: World,
    fresh: Fresh,
    purpose: OAuthPurpose,
    scopes: tuple[str, ...],
) -> None:
    service, oauth = _service(entra_settings)
    await service.begin(session, purpose, redirect_to=None, user=world.owner)
    assert oauth.begin_calls == [(list(scopes), world.owner.email)]
    [row] = await fresh.all(OAuthState)
    assert row.purpose is purpose
    assert row.user_id == world.owner.id
    assert row.redirect_after == "/"


@pytest.mark.parametrize("purpose", [OAuthPurpose.CONNECT_MAILBOX, OAuthPurpose.CONNECT_AZURE_DEVOPS])
async def test_begin_connect_requires_signed_in_user(
    session: AsyncSession, entra_settings: Settings, fresh: Fresh, purpose: OAuthPurpose
) -> None:
    service, oauth = _service(entra_settings)
    with pytest.raises(AuthenticationRequired):
        await service.begin(session, purpose, redirect_to=None, user=None)
    assert oauth.begin_calls == []
    assert await fresh.all(OAuthState) == []


# ---------------------------------------------------------------------------------------------
# EntraAuthService.complete: state validation
# ---------------------------------------------------------------------------------------------


async def test_complete_rejects_unknown_state(session: AsyncSession, entra_settings: Settings) -> None:
    service, oauth = _service(entra_settings)
    await _begin_flow(service, session)
    with pytest.raises(PermissionDenied, match="invalid or expired"):
        await service.complete(session, {"state": "forged"}, binding="anything", current_user=None, ip=IP)
    assert oauth.complete_calls == []


@pytest.mark.parametrize("bad_binding", [None, "", "not-the-binding"])
async def test_complete_rejects_missing_or_wrong_binding_without_consuming(
    session: AsyncSession, entra_settings: Settings, fresh: Fresh, bad_binding: str | None
) -> None:
    service, oauth = _service(entra_settings)
    params, binding = await _begin_flow(service, session)
    with pytest.raises(PermissionDenied):
        await service.complete(session, params, binding=bad_binding, current_user=None, ip=IP)
    assert oauth.complete_calls == []
    [row] = await fresh.all(OAuthState)
    assert row.consumed_at is None  # another browser cannot burn the victim's pending sign-in
    completion = await service.complete(session, params, binding=binding, current_user=None, ip=IP)
    assert completion.purpose is OAuthPurpose.LOGIN


async def test_complete_rejects_expired_state(session: AsyncSession, entra_settings: Settings) -> None:
    service, oauth = _service(entra_settings)
    params, binding = await _begin_flow(service, session)
    row = await _state_row(session, params["state"])
    row.expires_at = utcnow() - timedelta(seconds=1)
    await session.commit()
    with pytest.raises(PermissionDenied):
        await service.complete(session, params, binding=binding, current_user=None, ip=IP)
    assert oauth.complete_calls == []


async def test_complete_state_is_single_use(session: AsyncSession, entra_settings: Settings) -> None:
    service, oauth = _service(entra_settings)
    params, binding = await _begin_flow(service, session)
    await service.complete(session, params, binding=binding, current_user=None, ip=IP)
    await session.commit()
    with pytest.raises(PermissionDenied):
        await service.complete(session, params, binding=binding, current_user=None, ip=IP)
    assert len(oauth.complete_calls) == 1


async def test_complete_consumes_state_even_when_code_exchange_fails(
    session: AsyncSession, entra_settings: Settings, fresh: Fresh
) -> None:
    error = IntegrationError("Sign-in failed", category=FailureCategory.AUTHORIZATION)
    service, oauth = _service(entra_settings, error=error)
    params, binding = await _begin_flow(service, session)
    with pytest.raises(IntegrationError):
        await service.complete(session, params, binding=binding, current_user=None, ip=IP)
    await session.rollback()  # what the callback route does on failure
    [row] = await fresh.all(OAuthState)
    assert row.consumed_at is not None  # committed before the exchange
    oauth.error = None
    with pytest.raises(PermissionDenied):
        await service.complete(session, params, binding=binding, current_user=None, ip=IP)
    assert await fresh.all(User, User.email == LOGIN_EMAIL) == []


async def test_complete_passes_decrypted_flow_and_params_to_msal(
    session: AsyncSession, entra_settings: Settings
) -> None:
    service, oauth = _service(entra_settings)
    params, binding = await _begin_flow(service, session)
    await service.complete(session, {**params, "session_state": "xyz"}, binding=binding, current_user=None, ip=IP)
    [(flow, response, _cache)] = oauth.complete_calls
    assert flow == oauth.flows[0]
    assert response == {**params, "session_state": "xyz"}


# ---------------------------------------------------------------------------------------------
# EntraAuthService.complete: login provisioning
# ---------------------------------------------------------------------------------------------


async def _login(
    session: AsyncSession, settings: Settings, result: AuthResult | None = None
) -> tuple[User, FakeOAuth, str]:
    service, oauth = _service(settings, result=result)
    params, binding = await _begin_flow(service, session, redirect_to="/emails")
    completion = await service.complete(session, params, binding=binding, current_user=None, ip=IP)
    await session.commit()
    assert completion.purpose is OAuthPurpose.LOGIN
    return completion.user, oauth, completion.redirect_to


async def test_login_provisions_new_tenant_and_member(
    session: AsyncSession, entra_settings: Settings, fresh: Fresh
) -> None:
    user, _oauth, redirect = await _login(session, entra_settings)

    assert redirect == "/emails"
    [tenant] = await fresh.all(Tenant, Tenant.entra_tenant_id == TENANT_A)
    assert tenant.name == "contoso.example"
    assert tenant.slug.startswith(f"entra-{TENANT_A[:8]}-")
    assert tenant.is_demo is False
    stored = await fresh.get(User, user.id)
    assert stored is not None
    assert stored.tenant_id == tenant.id
    assert stored.email == LOGIN_EMAIL  # lower-cased
    assert stored.display_name == "Pat User"
    assert stored.entra_object_id == OID
    assert stored.role is UserRole.MEMBER
    [audit] = await fresh.audits("login", actor_user_id=user.id)
    assert audit.details == {"method": "entra"}
    assert audit.ip_address == IP
    assert audit.tenant_id == tenant.id


async def test_login_uses_email_as_name_when_missing(
    session: AsyncSession, entra_settings: Settings, fresh: Fresh
) -> None:
    user, _oauth, _redirect = await _login(session, entra_settings, auth_result(name=None))
    assert user.display_name == LOGIN_EMAIL


async def test_login_matches_existing_user_by_object_id(
    session: AsyncSession, entra_settings: Settings, factory: Factory, fresh: Fresh
) -> None:
    tenant = await factory.tenant("Contoso", entra_tenant_id=TENANT_A)
    existing = User(
        tenant_id=tenant.id,
        email="renamed@contoso.example",
        display_name="Old Name",
        entra_object_id=OID,
        role=UserRole.APPROVER,
    )
    session.add(existing)
    await session.commit()

    user, _oauth, _redirect = await _login(session, entra_settings)
    assert user.id == existing.id
    stored = await fresh.get(User, existing.id)
    assert stored is not None
    assert stored.display_name == "Pat User"
    assert stored.email == "renamed@contoso.example"
    assert stored.role is UserRole.APPROVER  # roles are never downgraded by sign-in
    assert len(await fresh.all(Tenant, Tenant.entra_tenant_id == TENANT_A)) == 1
    assert len(await fresh.all(User, User.tenant_id == tenant.id)) == 1


async def test_login_links_existing_user_by_email(
    session: AsyncSession, entra_settings: Settings, factory: Factory, fresh: Fresh
) -> None:
    tenant = await factory.tenant("Contoso", entra_tenant_id=TENANT_A)
    invited = await factory.user(tenant, UserRole.MEMBER, email=LOGIN_EMAIL)
    assert invited.entra_object_id is None

    user, _oauth, _redirect = await _login(session, entra_settings)
    assert user.id == invited.id
    stored = await fresh.get(User, invited.id)
    assert stored is not None
    assert stored.entra_object_id == OID


async def test_login_bootstrap_admin_for_new_user(session: AsyncSession, database_url: str, fresh: Fresh) -> None:
    admin_settings = make_settings(
        database_url,
        entra_client_id="client",
        entra_client_secret="not-a-real-secret",
        entra_tenant_id=TENANT_A,
        bootstrap_admin_emails=f"other@contoso.example, {LOGIN_EMAIL.upper()}",
    )
    user, _oauth, _redirect = await _login(session, admin_settings)
    assert user.role is UserRole.ADMIN


async def test_login_bootstrap_admin_promotes_existing_user(
    session: AsyncSession, database_url: str, factory: Factory, fresh: Fresh
) -> None:
    tenant = await factory.tenant("Contoso", entra_tenant_id=TENANT_A)
    member = await factory.user(tenant, UserRole.MEMBER, email=LOGIN_EMAIL)
    admin_settings = make_settings(
        database_url,
        entra_client_id="client",
        entra_client_secret="not-a-real-secret",
        entra_tenant_id=TENANT_A,
        bootstrap_admin_emails=LOGIN_EMAIL,
    )
    user, _oauth, _redirect = await _login(session, admin_settings)
    assert user.id == member.id
    stored = await fresh.get(User, member.id)
    assert stored is not None
    assert stored.role is UserRole.ADMIN


async def test_login_refuses_deactivated_user(
    session: AsyncSession, entra_settings: Settings, factory: Factory, fresh: Fresh
) -> None:
    tenant = await factory.tenant("Contoso", entra_tenant_id=TENANT_A)
    gone_id = (await factory.user(tenant, UserRole.ADMIN, email=LOGIN_EMAIL, active=False)).id
    service, _oauth = _service(entra_settings)
    params, binding = await _begin_flow(service, session)
    with pytest.raises(PermissionDenied, match="deactivated"):
        await service.complete(session, params, binding=binding, current_user=None, ip=IP)
    await session.rollback()
    stored = await fresh.get(User, gone_id)
    assert stored is not None
    assert stored.is_active is False
    assert stored.entra_object_id is None
    assert await fresh.audits("login") == []


@pytest.mark.parametrize(
    "result",
    [
        auth_result(tid=TENANT_B),  # not in the allow-list
        auth_result(email=None, preferred_username="no-at-sign"),  # no e-mail-like identifier
        auth_result(oid=""),
        auth_result(tid=""),
    ],
)
async def test_login_refuses_disallowed_or_incomplete_identity(
    session: AsyncSession, database_url: str, fresh: Fresh, result: AuthResult
) -> None:
    restricted = make_settings(
        database_url,
        entra_client_id="client",
        entra_client_secret="not-a-real-secret",
        entra_tenant_id="organizations",
        entra_allowed_tenant_ids=[TENANT_A],
    )
    service, _oauth = _service(restricted, result=result)
    params, binding = await _begin_flow(service, session)
    with pytest.raises(PermissionDenied, match="not allowed"):
        await service.complete(session, params, binding=binding, current_user=None, ip=IP)
    await session.rollback()
    assert await fresh.all(Tenant, Tenant.entra_tenant_id.is_not(None)) == []
    assert await fresh.audits("login") == []


async def test_login_from_allowed_tenant_in_multi_tenant_mode(
    session: AsyncSession, database_url: str, fresh: Fresh
) -> None:
    restricted = make_settings(
        database_url,
        entra_client_id="client",
        entra_client_secret="not-a-real-secret",
        entra_tenant_id="organizations",
        entra_allowed_tenant_ids=[TENANT_A, TENANT_B],
    )
    user, _oauth, _redirect = await _login(session, restricted, auth_result(tid=TENANT_B, home_account_id="x.y"))
    tenant = await fresh.get(Tenant, user.tenant_id)
    assert tenant is not None
    assert tenant.entra_tenant_id == TENANT_B


# ---------------------------------------------------------------------------------------------
# EntraAuthService.complete: connecting a mailbox / Azure DevOps
# ---------------------------------------------------------------------------------------------


async def _connect(
    session: AsyncSession,
    settings: Settings,
    user: User,
    purpose: OAuthPurpose = OAuthPurpose.CONNECT_MAILBOX,
    result: AuthResult | None = None,
) -> tuple[FakeOAuth, Any]:
    service, oauth = _service(settings, result=result)
    params, binding = await _begin_flow(service, session, purpose, user=user, redirect_to="/settings")
    completion = await service.complete(session, params, binding=binding, current_user=user, ip=IP)
    return oauth, completion


@pytest.mark.parametrize("who", ["nobody", "other"])
async def test_connect_must_be_completed_by_the_initiating_user(
    session: AsyncSession, entra_settings: Settings, world: World, fresh: Fresh, who: str
) -> None:
    service, oauth = _service(entra_settings)
    params, binding = await _begin_flow(service, session, OAuthPurpose.CONNECT_MAILBOX, user=world.owner)
    current = None if who == "nobody" else world.other
    with pytest.raises(PermissionDenied, match="user who started it"):
        await service.complete(session, params, binding=binding, current_user=current, ip=IP)
    assert oauth.complete_calls == []
    [row] = await fresh.all(OAuthState)
    assert row.consumed_at is not None
    assert await fresh.all(ConnectedAccount, ConnectedAccount.provider == ConnectionProvider.MICROSOFT_GRAPH) == []


async def test_connect_refuses_account_from_another_entra_tenant(
    session: AsyncSession, entra_settings: Settings, world: World, fresh: Fresh
) -> None:
    world.tenant.entra_tenant_id = TENANT_A
    await session.commit()
    service, _oauth = _service(entra_settings, result=auth_result(tid=TENANT_B))
    params, binding = await _begin_flow(service, session, OAuthPurpose.CONNECT_MAILBOX, user=world.owner)
    with pytest.raises(PermissionDenied, match="different Microsoft Entra tenant"):
        await service.complete(session, params, binding=binding, current_user=world.owner, ip=IP)
    await session.rollback()
    assert await fresh.all(ConnectedAccount, ConnectedAccount.provider == ConnectionProvider.MICROSOFT_GRAPH) == []
    assert await fresh.all(CredentialRecord) == []


async def test_connect_mailbox_creates_account_and_encrypted_credentials(
    session: AsyncSession, entra_settings: Settings, world: World, fresh: Fresh
) -> None:
    oauth, completion = await _connect(session, entra_settings, world.owner)

    assert completion.purpose is OAuthPurpose.CONNECT_MAILBOX
    assert completion.user is world.owner
    assert completion.redirect_to == "/settings"
    assert json.loads(oauth.complete_calls[0][2]) == {}  # no previous cache for a new account
    [account] = await _graph_accounts(fresh, world.owner, ConnectionProvider.MICROSOFT_GRAPH)  # committed
    assert account.tenant_id == world.tenant.id
    assert account.external_account_id == HOME
    assert account.email == LOGIN_EMAIL
    assert account.display_name == "Pat User"
    assert account.granted_scopes == ["Mail.Read", "User.Read"]
    assert account.status is ConnectionStatus.ACTIVE
    assert account.connected_at is not None
    assert account.last_error is None
    [record] = await fresh.all(CredentialRecord, CredentialRecord.connected_account_id == account.id)
    assert record.tenant_id == world.tenant.id
    assert REFRESH_TOKEN.encode() not in record.encrypted_payload
    async with fresh.db.session() as s:
        stored = await CredentialStore(s, SecretBox.from_settings(entra_settings)).load(
            account.id, tenant_id=world.tenant.id
        )
    assert stored is not None
    assert REFRESH_TOKEN in stored
    [audit] = await fresh.audits("account_connected")
    assert audit.actor_user_id == world.owner.id
    assert audit.ip_address == IP
    assert audit.resource_id == str(account.id)
    assert audit.details == {"provider": "microsoft_graph", "scopes": ["Mail.Read", "User.Read"]}


async def test_connect_azure_devops_uses_its_own_provider(
    session: AsyncSession, entra_settings: Settings, world: World, fresh: Fresh
) -> None:
    _oauth, completion = await _connect(session, entra_settings, world.owner, OAuthPurpose.CONNECT_AZURE_DEVOPS)
    assert completion.purpose is OAuthPurpose.CONNECT_AZURE_DEVOPS
    [account] = await _graph_accounts(fresh, world.owner, ConnectionProvider.AZURE_DEVOPS)
    assert account.external_account_id == HOME
    assert await _graph_accounts(fresh, world.owner, ConnectionProvider.MICROSOFT_GRAPH) == []
    [audit] = await fresh.audits("account_connected")
    assert audit.details["provider"] == "azure_devops"


async def test_reconnect_same_account_reuses_row_but_stores_only_the_new_tokens(
    session: AsyncSession, entra_settings: Settings, world: World, factory: Factory, fresh: Fresh
) -> None:
    world.tenant.entra_tenant_id = TENANT_A  # same Entra tenant as the result → allowed
    await session.commit()
    account = await factory.account(
        world.owner,
        ConnectionProvider.MICROSOFT_GRAPH,
        status=ConnectionStatus.NEEDS_REAUTH,
        external_account_id=HOME,
        last_error="Microsoft 365 authorization expired",
        disconnected_at=utcnow(),
    )
    await _store_cache(session, entra_settings, account, OLD_REFRESH_TOKEN)

    oauth, _completion = await _connect(session, entra_settings, world.owner)

    assert OLD_REFRESH_TOKEN not in oauth.complete_calls[0][2]  # MSAL starts from an empty cache
    [stored] = await _graph_accounts(fresh, world.owner, ConnectionProvider.MICROSOFT_GRAPH)
    assert stored.id == account.id
    assert stored.status is ConnectionStatus.ACTIVE
    assert stored.last_error is None
    assert stored.disconnected_at is None
    assert stored.email == LOGIN_EMAIL
    [record] = await fresh.all(CredentialRecord, CredentialRecord.connected_account_id == account.id)
    plaintext = SecretBox.from_settings(entra_settings).decrypt_str(record.encrypted_payload)
    assert REFRESH_TOKEN in plaintext
    assert OLD_REFRESH_TOKEN not in plaintext  # superseded tokens are discarded, not carried forward


async def test_reconnect_without_stored_credentials_starts_from_empty_cache(
    session: AsyncSession, entra_settings: Settings, world: World, factory: Factory, fresh: Fresh
) -> None:
    account = await factory.account(world.owner, ConnectionProvider.MICROSOFT_GRAPH, external_account_id=HOME)
    oauth, _completion = await _connect(session, entra_settings, world.owner)
    assert json.loads(oauth.complete_calls[0][2]) == {}
    [stored] = await _graph_accounts(fresh, world.owner, ConnectionProvider.MICROSOFT_GRAPH)
    assert stored.id == account.id
    assert len(await fresh.all(CredentialRecord, CredentialRecord.connected_account_id == account.id)) == 1


async def test_connect_different_microsoft_account_replaces_previous_one(
    session: AsyncSession, entra_settings: Settings, world: World, factory: Factory, fresh: Fresh
) -> None:
    old = await factory.account(world.owner, ConnectionProvider.MICROSOFT_GRAPH, external_account_id="old-uid.utid")
    await _store_cache(session, entra_settings, old, OLD_REFRESH_TOKEN)

    await _connect(session, entra_settings, world.owner)

    assert await fresh.get(ConnectedAccount, old.id) is None
    assert await fresh.all(CredentialRecord, CredentialRecord.connected_account_id == old.id) == []
    [new] = await _graph_accounts(fresh, world.owner, ConnectionProvider.MICROSOFT_GRAPH)
    assert new.id != old.id
    assert new.external_account_id == HOME
    assert len(await fresh.all(CredentialRecord, CredentialRecord.connected_account_id == new.id)) == 1
    # the demo mailbox of the same user is untouched
    assert await fresh.get(ConnectedAccount, world.account.id) is not None


# ---------------------------------------------------------------------------------------------
# disconnect_account
# ---------------------------------------------------------------------------------------------


async def test_disconnect_unknown_or_foreign_account_is_not_found(
    session: AsyncSession, settings: Settings, world: World, factory: Factory, fresh: Fresh
) -> None:
    foreign = await factory.account(world.other, ConnectionProvider.MICROSOFT_GRAPH)
    actor = World.actor(world.owner)
    other_tenant_user = await factory.user(await factory.tenant(), UserRole.MEMBER)
    other_tenant_account = await factory.account(other_tenant_user, ConnectionProvider.MICROSOFT_GRAPH)
    # Regression: this was a 409 Conflict; invisible resources must be 404 (docs/api.md, tenant isolation).
    for account_id in (uuid.uuid4(), foreign.id, other_tenant_account.id):
        with pytest.raises(NotFound, match="Connection not found") as info:
            await disconnect_account(session, actor, account_id, settings)
        assert info.value.status_code == 404
    stored = await fresh.get(ConnectedAccount, foreign.id)
    assert stored is not None
    assert stored.status is ConnectionStatus.ACTIVE


async def test_disconnect_refuses_demo_mailbox(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh
) -> None:
    with pytest.raises(Conflict, match="demo mailbox"):
        await disconnect_account(session, World.actor(world.owner), world.account.id, settings)
    stored = await fresh.get(ConnectedAccount, world.account.id)
    assert stored is not None
    assert stored.status is ConnectionStatus.ACTIVE
    assert await fresh.audits("account_disconnected") == []


async def test_disconnect_deletes_credentials_and_audits(
    session: AsyncSession, settings: Settings, world: World, factory: Factory, fresh: Fresh
) -> None:
    account = await factory.account(world.owner, ConnectionProvider.MICROSOFT_GRAPH)
    await _store_cache(session, settings, account, REFRESH_TOKEN)

    await disconnect_account(session, World.actor(world.owner, IP), account.id, settings)

    stored = await fresh.get(ConnectedAccount, account.id)
    assert stored is not None
    assert stored.status is ConnectionStatus.DISCONNECTED
    assert stored.disconnected_at is not None
    assert await fresh.all(CredentialRecord, CredentialRecord.connected_account_id == account.id) == []
    [audit] = await fresh.audits("account_disconnected")
    assert audit.actor_user_id == world.owner.id
    assert audit.resource_id == str(account.id)
    assert audit.outcome is AuditOutcome.SUCCESS
    assert audit.details == {"provider": "microsoft_graph"}
    assert audit.ip_address == IP


async def test_disconnect_without_encryption_keys_skips_credential_deletion(
    session: AsyncSession, settings: Settings, database_url: str, world: World, factory: Factory, fresh: Fresh
) -> None:
    account = await factory.account(world.owner, ConnectionProvider.AZURE_DEVOPS)
    await _store_cache(session, settings, account, REFRESH_TOKEN)
    keyless = make_settings(database_url, token_encryption_keys=None)

    await disconnect_account(session, World.actor(world.owner), account.id, keyless)

    stored = await fresh.get(ConnectedAccount, account.id)
    assert stored is not None
    assert stored.status is ConnectionStatus.DISCONNECTED
    assert len(await fresh.all(CredentialRecord, CredentialRecord.connected_account_id == account.id)) == 1
    [audit] = await fresh.audits("account_disconnected")
    assert audit.details == {"provider": "azure_devops"}
