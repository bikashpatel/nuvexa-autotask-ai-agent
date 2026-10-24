from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import (
    ActorDep,
    ResolvedSessionDep,
    SessionDep,
    SettingsDep,
    get_optional_resolved_session,
    rate_limit,
)
from app.api.middleware import client_ip
from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import Tenant, User
from app.domain.enums import OAuthPurpose
from app.observability.logging import get_logger
from app.schemas.api import AuthConfigOut, DemoLoginRequest, MeOut, UserOut
from app.services.auth_service import (
    DemoAuthService,
    EntraAuthService,
    IssuedSession,
    ResolvedSession,
    SessionService,
    safe_redirect_path,
)
from app.services.authz import ROLE_PERMISSIONS
from app.services.identity import actor_from_user

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)

OAUTH_BINDING_COOKIE = "nuvexa_oauth_bind"


def set_session_cookies(response: Response, settings: Settings, issued: IssuedSession) -> None:
    common = {"secure": settings.secure_cookies, "samesite": "lax", "path": "/", "max_age": issued.expires_at_seconds}
    response.set_cookie(settings.session_cookie_name, issued.token, httponly=True, **common)  # type: ignore[arg-type]
    # Readable by the SPA; echoed back in X-CSRF-Token (validated against the hash stored with the session).
    response.set_cookie(settings.csrf_cookie_name, issued.csrf_token, httponly=False, **common)  # type: ignore[arg-type]


def clear_session_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


def me_out(user_row: User, tenant_name: str) -> MeOut:
    actor = actor_from_user(user_row)
    user = UserOut(
        id=actor.user_id,
        email=actor.email,
        display_name=actor.display_name,
        role=actor.role,
        tenant_id=actor.tenant_id,
        tenant_name=tenant_name,
    )
    return MeOut(user=user, permissions=sorted(ROLE_PERMISSIONS[actor.role]))


@router.get("/config", response_model=AuthConfigOut)
async def auth_config(settings: SettingsDep) -> AuthConfigOut:
    return AuthConfigOut(
        app_name=settings.app_name,
        environment=settings.app_env.value,
        demo_mode=settings.demo_mode,
        demo_login_enabled=settings.demo_login_enabled,
        entra_login_enabled=settings.entra_configured,
    )


@router.post("/demo-login", response_model=MeOut, dependencies=[Depends(rate_limit("auth", 20, per="ip"))])
async def demo_login(
    body: DemoLoginRequest, request: Request, response: Response, session: SessionDep, settings: SettingsDep
) -> MeOut:
    sessions = SessionService(settings)
    await sessions.revoke_token(session, request.cookies.get(settings.session_cookie_name))
    user = await DemoAuthService(settings).login(session, body.persona, ip=client_ip(request))
    issued = await sessions.issue(session, user, ip=client_ip(request), user_agent=request.headers.get("user-agent"))
    await session.commit()
    set_session_cookies(response, settings, issued)
    tenant = await session.get(Tenant, user.tenant_id)
    return me_out(user, tenant.name if tenant else "")


@router.get("/login", dependencies=[Depends(rate_limit("auth", 20, per="ip"))])
async def login(session: SessionDep, settings: SettingsDep, redirect_to: str | None = None) -> RedirectResponse:
    auth_uri, binding = await EntraAuthService(settings).begin(
        session, OAuthPurpose.LOGIN, redirect_to=redirect_to, user=None
    )
    return _redirect_with_binding(auth_uri, binding, settings)


def _redirect_with_binding(url: str, binding: str, settings: Settings) -> RedirectResponse:
    response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        OAUTH_BINDING_COOKIE,
        binding,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/api/v1/auth/callback",
        max_age=600,
    )
    return response


@router.get("/callback", include_in_schema=True, dependencies=[Depends(rate_limit("auth", 30, per="ip"))])
async def oauth_callback(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    current: ResolvedSession | None = Depends(get_optional_resolved_session),
) -> RedirectResponse:
    params = {
        k: v
        for k, v in request.query_params.items()
        if k in ("code", "state", "session_state", "error", "error_description", "client_info")
    }
    base = settings.app_base_url
    if "error" in params:
        log.warning("oauth_error_returned", error=params.get("error"))
        response = RedirectResponse(f"{base}/login?error=oauth_denied", status_code=status.HTTP_302_FOUND)
        response.delete_cookie(OAUTH_BINDING_COOKIE, path="/api/v1/auth/callback")
        return response
    try:
        completion = await EntraAuthService(settings).complete(
            session,
            params,
            binding=request.cookies.get(OAUTH_BINDING_COOKIE),
            current_user=current.user if current else None,
            ip=client_ip(request),
        )
    except AppError as exc:
        await session.rollback()
        log.warning("oauth_callback_failed", code=exc.code)
        response = RedirectResponse(f"{base}/login?error={exc.code}", status_code=status.HTTP_302_FOUND)
        response.delete_cookie(OAUTH_BINDING_COOKIE, path="/api/v1/auth/callback")
        return response
    response = RedirectResponse(
        f"{base}{safe_redirect_path(completion.redirect_to)}", status_code=status.HTTP_302_FOUND
    )
    response.delete_cookie(OAUTH_BINDING_COOKIE, path="/api/v1/auth/callback")
    if completion.purpose is OAuthPurpose.LOGIN:
        if current is not None:  # rotate: never keep a pre-login session
            await SessionService(settings).revoke(session, current)
        issued = await SessionService(settings).issue(
            session, completion.user, ip=client_ip(request), user_agent=request.headers.get("user-agent")
        )
        await session.commit()
        set_session_cookies(response, settings, issued)
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(resolved: ResolvedSessionDep, session: SessionDep, settings: SettingsDep) -> Response:
    await SessionService(settings).revoke(session, resolved)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response, settings)
    return response


@router.get("/me", response_model=MeOut)
async def me(resolved: ResolvedSessionDep, _actor: ActorDep) -> MeOut:
    return me_out(resolved.user, resolved.tenant.name)
