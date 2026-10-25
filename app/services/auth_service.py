"""Browser sessions, demo login and Microsoft Entra sign-in / account connection flows."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import msal
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AuthenticationRequired, ConfigurationError, Conflict, NotFound, PermissionDenied
from app.core.security import SecretBox, constant_time_equals, generate_token, hash_token
from app.core.timeutil import utcnow
from app.db.models import ConnectedAccount, OAuthState, Tenant, User, UserSession
from app.demo import DEMO_PERSONAS, DEMO_TENANT
from app.domain.enums import (
    ActorType,
    AuditOutcome,
    ConnectionProvider,
    ConnectionStatus,
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
from app.services.audit import record_audit
from app.services.authz import Actor
from app.services.identity import actor_from_user

# `\Z`, not `$`: `$` also matches before a trailing newline, which would let "/x\n" into a Location header.
_SAFE_REDIRECT_RE = re.compile(r"^/(?!/)[A-Za-z0-9/_\-?=&.%]*\Z")
_OAUTH_STATE_TTL = timedelta(minutes=10)
_TOUCH_INTERVAL = timedelta(seconds=60)

PURPOSE_SCOPES: dict[OAuthPurpose, tuple[str, ...]] = {
    OAuthPurpose.LOGIN: GRAPH_LOGIN_SCOPES,
    OAuthPurpose.CONNECT_MAILBOX: GRAPH_MAIL_SCOPES,
    OAuthPurpose.CONNECT_AZURE_DEVOPS: AZURE_DEVOPS_DELEGATED_SCOPES,
}


def safe_redirect_path(value: str | None, default: str = "/") -> str:
    """Only same-site relative paths are allowed as post-login redirects (no open redirects)."""
    if value and len(value) <= 300 and _SAFE_REDIRECT_RE.match(value):
        return value
    return default


async def _refuse_login(session: AsyncSession, user: User, *, actor: Actor | None, details: dict[str, Any]) -> None:
    """Audit a refused sign-in to `user` and commit it: the caller rolls its own transaction back on failure."""
    record_audit(
        session,
        tenant_id=user.tenant_id,
        actor=actor,
        actor_type=ActorType.USER,
        event_type="login",
        resource_type="user",
        resource_id=user.id,
        outcome=AuditOutcome.DENIED,
        details=details,
    )
    await session.commit()


@dataclass
class IssuedSession:
    token: str
    csrf_token: str
    expires_at_seconds: int


@dataclass
class ResolvedSession:
    session: UserSession
    user: User
    tenant: Tenant


class SessionService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def issue(
        self, session: AsyncSession, user: User, *, ip: str | None, user_agent: str | None
    ) -> IssuedSession:
        token, csrf = generate_token(), generate_token()
        now = utcnow()
        ttl = timedelta(hours=self.settings.session_ttl_hours)
        session.add(
            UserSession(
                user_id=user.id,
                tenant_id=user.tenant_id,
                token_hash=hash_token(token),
                csrf_token_hash=hash_token(csrf),
                created_at=now,
                last_seen_at=now,
                expires_at=now + ttl,
                ip_address=ip,
                user_agent=(user_agent or "")[:400] or None,
            )
        )
        user.last_login_at = now
        return IssuedSession(token=token, csrf_token=csrf, expires_at_seconds=int(ttl.total_seconds()))

    async def resolve(self, session: AsyncSession, token: str | None) -> ResolvedSession | None:
        if not token or len(token) > 200:
            return None
        row = (
            await session.execute(select(UserSession).where(UserSession.token_hash == hash_token(token)))
        ).scalar_one_or_none()
        now = utcnow()
        if row is None or row.revoked_at is not None or row.expires_at <= now:
            return None
        if now - row.last_seen_at > timedelta(minutes=self.settings.session_idle_minutes):
            return None
        user = await session.get(User, row.user_id)
        if user is None or not user.is_active:
            return None
        tenant = await session.get(Tenant, user.tenant_id)
        if tenant is None:
            return None
        if now - row.last_seen_at > _TOUCH_INTERVAL:
            await session.execute(update(UserSession).where(UserSession.id == row.id).values(last_seen_at=now))
            await session.commit()
        return ResolvedSession(session=row, user=user, tenant=tenant)

    @staticmethod
    def csrf_valid(resolved: ResolvedSession, header_value: str | None) -> bool:
        return bool(header_value) and constant_time_equals(
            hash_token(header_value or ""), resolved.session.csrf_token_hash
        )

    async def revoke(self, session: AsyncSession, resolved: ResolvedSession) -> None:
        resolved.session.revoked_at = utcnow()
        await session.commit()

    async def revoke_token(self, session: AsyncSession, token: str | None) -> None:
        """Revoke the session a browser presents before it is replaced (session-fixation defence).

        Adds to the caller's transaction; unknown or malformed tokens are ignored."""
        if not token or len(token) > 200:
            return
        await session.execute(
            update(UserSession)
            .where(UserSession.token_hash == hash_token(token), UserSession.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )


class DemoAuthService:
    """Demo personas inside a dedicated demo tenant (only when DEMO_MODE and not production)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def login(self, session: AsyncSession, persona: str, *, ip: str | None = None) -> User:
        if not self.settings.demo_login_enabled:
            raise PermissionDenied("Demo login is disabled.")
        spec = DEMO_PERSONAS.get(persona)
        if spec is None:
            raise PermissionDenied("Unknown demo persona.")
        tenant = (await session.execute(select(Tenant).where(Tenant.slug == DEMO_TENANT.slug))).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(name=DEMO_TENANT.name, slug=DEMO_TENANT.slug, is_demo=True)
            session.add(tenant)
            await session.flush()
        email = spec.email.lower()
        user = (
            await session.execute(select(User).where(User.tenant_id == tenant.id, User.email == email))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                tenant_id=tenant.id,
                email=email,
                display_name=spec.display_name,
                role=spec.role,
                is_active=True,
            )
            session.add(user)
            await session.flush()
        elif not user.is_active:
            await _refuse_login(
                session,
                user,
                actor=actor_from_user(user, ip),
                details={"method": "demo", "persona": persona, "reason": "deactivated"},
            )
            raise PermissionDenied("This user is deactivated.")
        account = (
            await session.execute(
                select(ConnectedAccount).where(
                    ConnectedAccount.user_id == user.id, ConnectedAccount.provider == ConnectionProvider.DEMO_MAILBOX
                )
            )
        ).scalar_one_or_none()
        if account is None and user.role is not UserRole.VIEWER:
            session.add(
                ConnectedAccount(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    provider=ConnectionProvider.DEMO_MAILBOX,
                    external_account_id=f"demo:{persona}",
                    email=email,
                    display_name=f"{user.display_name} (demo)",
                    granted_scopes=["demo.mailbox.read"],
                    status=ConnectionStatus.ACTIVE,
                    connected_at=utcnow(),
                )
            )
        record_audit(
            session,
            tenant_id=tenant.id,
            actor=actor_from_user(user, ip),
            event_type="login",
            resource_type="user",
            resource_id=user.id,
            details={"method": "demo", "persona": persona},
        )
        return user


@dataclass
class OAuthCompletion:
    purpose: OAuthPurpose
    user: User
    redirect_to: str


class EntraAuthService:
    def __init__(self, settings: Settings, oauth: EntraOAuthClient | None = None) -> None:
        self.settings = settings
        self._oauth = oauth

    @property
    def oauth(self) -> EntraOAuthClient:
        if self._oauth is None:
            self._oauth = EntraOAuthClient(self.settings)
        return self._oauth

    def _check_login_policy(self) -> None:
        if not self.settings.entra_configured:
            raise ConfigurationError("Microsoft sign-in is not configured.")
        multi_tenant = self.settings.entra_tenant_id.lower() in ("common", "organizations")
        if multi_tenant and not self.settings.entra_allowed_tenant_ids:
            raise ConfigurationError("ENTRA_ALLOWED_TENANT_IDS is required when ENTRA_TENANT_ID is multi-tenant.")

    async def begin(
        self, session: AsyncSession, purpose: OAuthPurpose, *, redirect_to: str | None, user: User | None
    ) -> tuple[str, str]:
        """Returns (authorization URL, browser-binding token to set as a short-lived cookie)."""
        self._check_login_policy()
        if purpose is not OAuthPurpose.LOGIN and user is None:
            raise AuthenticationRequired()
        flow = await self.oauth.begin_flow(
            list(PURPOSE_SCOPES[purpose]), login_hint=user.email if user is not None else None
        )
        binding = generate_token()
        now = utcnow()
        session.add(
            OAuthState(
                state=str(flow["state"]),
                purpose=purpose,
                user_id=user.id if user else None,
                browser_binding_hash=hash_token(binding),
                flow_encrypted=SecretBox.from_settings(self.settings).encrypt_str(json.dumps(flow)),
                redirect_after=safe_redirect_path(redirect_to),
                created_at=now,
                expires_at=now + _OAUTH_STATE_TTL,
            )
        )
        await session.commit()
        return str(flow["auth_uri"]), binding

    async def complete(
        self,
        session: AsyncSession,
        params: dict[str, str],
        *,
        binding: str | None,
        current_user: User | None,
        ip: str | None,
    ) -> OAuthCompletion:
        self._check_login_policy()
        state_value = params.get("state", "")
        row = (
            await session.execute(select(OAuthState).where(OAuthState.state == state_value).with_for_update())
        ).scalar_one_or_none()
        now = utcnow()
        if (
            row is None
            or row.consumed_at is not None
            or row.expires_at <= now
            or not binding
            or not constant_time_equals(hash_token(binding), row.browser_binding_hash)
        ):
            raise PermissionDenied("The sign-in request is invalid or expired. Start again.")
        row.consumed_at = now  # single use, even if the code exchange below fails
        await session.commit()
        flow = json.loads(SecretBox.from_settings(self.settings).decrypt_str(row.flow_encrypted))

        if row.purpose is OAuthPurpose.LOGIN:
            result = await self.oauth.complete_flow(flow, params, msal.SerializableTokenCache())
            user = await self._provision_user(session, result, ip=ip)
            return OAuthCompletion(OAuthPurpose.LOGIN, user, row.redirect_after)

        if current_user is None or row.user_id != current_user.id:
            raise PermissionDenied("The connection must be completed by the user who started it.")
        store = CredentialStore(session, SecretBox.from_settings(self.settings))
        provider = (
            ConnectionProvider.MICROSOFT_GRAPH
            if row.purpose is OAuthPurpose.CONNECT_MAILBOX
            else ConnectionProvider.AZURE_DEVOPS
        )
        account = (
            (
                await session.execute(
                    select(ConnectedAccount).where(
                        ConnectedAccount.user_id == current_user.id, ConnectedAccount.provider == provider
                    )
                )
            )
            .scalars()
            .first()
        )
        # Always start from an empty cache: the redeemed tokens replace whatever was stored, and tokens of a
        # previously connected (different) Microsoft account must never be carried into the new connection.
        cache = msal.SerializableTokenCache()
        result = await self.oauth.complete_flow(flow, params, cache)
        tenant = await session.get(Tenant, current_user.tenant_id)
        if tenant is not None and tenant.entra_tenant_id and result.tid != tenant.entra_tenant_id:
            raise PermissionDenied("The connected account belongs to a different Microsoft Entra tenant.")
        if account is None or account.external_account_id != result.home_account_id:
            if account is not None:
                await session.delete(account)
                await session.flush()
            account = ConnectedAccount(
                tenant_id=current_user.tenant_id,
                user_id=current_user.id,
                provider=provider,
                external_account_id=result.home_account_id,
            )
            session.add(account)
            await session.flush()
        account.email = result.login_email
        account.display_name = result.name
        account.granted_scopes = list(result.granted_scopes)
        account.status = ConnectionStatus.ACTIVE
        account.last_error = None
        account.connected_at = now
        account.disconnected_at = None
        await store.save_cache(account, cache)
        record_audit(
            session,
            tenant_id=current_user.tenant_id,
            actor=actor_from_user(current_user, ip),
            event_type="account_connected",
            resource_type="connected_account",
            resource_id=account.id,
            details={"provider": provider.value, "scopes": list(result.granted_scopes)},
        )
        await session.commit()
        return OAuthCompletion(row.purpose, current_user, row.redirect_after)

    async def _provision_user(self, session: AsyncSession, result: AuthResult, *, ip: str | None) -> User:
        tid, oid = result.tid, result.oid
        email = result.login_email or ""
        name = result.name or email
        allowed = self.settings.entra_allowed_tenant_ids
        if not tid or not oid or not email or (allowed and tid not in allowed):
            raise PermissionDenied("This Microsoft account is not allowed to sign in.")
        tenant = (await session.execute(select(Tenant).where(Tenant.entra_tenant_id == tid))).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(
                name=email.split("@")[-1], slug=f"entra-{tid[:8]}-{uuid.uuid4().hex[:4]}", entra_tenant_id=tid
            )
            session.add(tenant)
            await session.flush()
        # The stable identity is (tid, oid). The `email` / `preferred_username` claims are mutable and unverified,
        # so they may only link a pre-provisioned account that no Entra identity owns yet ("nOAuth").
        user = (
            await session.execute(select(User).where(User.tenant_id == tenant.id, User.entra_object_id == oid))
        ).scalar_one_or_none()
        if user is None:
            user = (
                await session.execute(select(User).where(User.tenant_id == tenant.id, User.email == email))
            ).scalar_one_or_none()
            if user is not None and user.entra_object_id is not None:
                await self._refuse_entra_login(session, user, oid=oid, ip=ip, reason="identity_mismatch")
                raise PermissionDenied("This Microsoft account is not allowed to sign in.")
        # Bootstrap only when the account is created or first bound; never re-elevate on later sign-ins.
        bootstrap = email in self.settings.bootstrap_admin_emails
        if user is None:
            role = UserRole.ADMIN if bootstrap else UserRole.MEMBER
            user = User(tenant_id=tenant.id, email=email, display_name=name, entra_object_id=oid, role=role)
            session.add(user)
            await session.flush()
        else:
            if not user.is_active:
                await self._refuse_entra_login(session, user, oid=oid, ip=ip, reason="deactivated")
                raise PermissionDenied("This user is deactivated.")
            if user.entra_object_id is None:  # first sign-in of a pre-provisioned account binds it
                user.entra_object_id = oid
                if bootstrap:
                    user.role = UserRole.ADMIN
            user.display_name = name
        record_audit(
            session,
            tenant_id=tenant.id,
            actor=actor_from_user(user, ip),
            event_type="login",
            resource_type="user",
            resource_id=user.id,
            details={"method": "entra"},
        )
        return user

    @staticmethod
    async def _refuse_entra_login(session: AsyncSession, user: User, *, oid: str, ip: str | None, reason: str) -> None:
        details: dict[str, Any] = {"method": "entra", "reason": reason, "object_id": oid}
        if user.entra_object_id == oid:  # the account's own identity: attribute the attempt to it
            await _refuse_login(session, user, actor=actor_from_user(user, ip), details=details)
        else:  # another (or a not yet bound) identity: never attribute it to the account it tried to reach
            await _refuse_login(session, user, actor=None, details={**details, "ip_address": ip})


async def disconnect_account(session: AsyncSession, actor: Actor, account_id: uuid.UUID, settings: Settings) -> None:
    account = (
        await session.execute(
            select(ConnectedAccount).where(
                ConnectedAccount.id == account_id,
                ConnectedAccount.tenant_id == actor.tenant_id,
                ConnectedAccount.user_id == actor.user_id,
            )
        )
    ).scalar_one_or_none()
    if account is None:  # unknown, foreign and other-tenant ids look the same: 404 (docs/api.md)
        raise NotFound("Connection not found.")
    if account.provider is ConnectionProvider.DEMO_MAILBOX:
        raise Conflict("The demo mailbox cannot be disconnected.")
    if settings.token_encryption_keys is not None:
        await CredentialStore(session, SecretBox.from_settings(settings)).delete(
            account.id, tenant_id=account.tenant_id
        )
    account.status = ConnectionStatus.DISCONNECTED
    account.disconnected_at = utcnow()
    record_audit(
        session,
        tenant_id=actor.tenant_id,
        actor=actor,
        event_type="account_disconnected",
        resource_type="connected_account",
        resource_id=account.id,
        outcome=AuditOutcome.SUCCESS,
        details={"provider": account.provider.value},
    )
    await session.commit()
