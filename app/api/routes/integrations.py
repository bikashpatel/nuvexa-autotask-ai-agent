from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select

from app.api.deps import ActorDep, SessionDep, SettingsDep, rate_limit
from app.api.routes.auth import _redirect_with_binding
from app.core.config import AzureDevOpsMode, LLMProvider
from app.core.errors import AppError, Conflict
from app.db.models import ConnectedAccount, EmailMessage, EmailSyncCheckpoint, User
from app.domain.enums import ActionType, ConnectionProvider, ConnectionStatus, OAuthPurpose
from app.integrations.azure_devops.base import FieldIssue
from app.integrations.azure_devops.mapping import build_create_request
from app.integrations.microsoft_graph.base import GRAPH_MAIL_SCOPES
from app.schemas.actions import AzureDevOpsWorkItemPayload, SourceEmailReference
from app.schemas.api import (
    AdoConnectionTestOut,
    AdoProjectsOut,
    AdoValidateOut,
    AdoValidateRequest,
    AdoWorkItemTypesOut,
    AzureDevOpsIntegrationOut,
    ConnectionOut,
    IntegrationsOut,
    LLMIntegrationOut,
    MicrosoftIntegrationOut,
    SyncStateOut,
)
from app.services.action_policy import ActionPolicy
from app.services.auth_service import EntraAuthService, disconnect_account
from app.services.authz import Perm, require
from app.services.integration_clients import find_active_account, make_ado_client_factory
from app.services.settings_service import get_tenant_settings

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("", response_model=IntegrationsOut)
async def integrations(actor: ActorDep, session: SessionDep, settings: SettingsDep) -> IntegrationsOut:
    accounts = (
        (
            await session.execute(
                select(ConnectedAccount)
                .where(ConnectedAccount.tenant_id == actor.tenant_id, ConnectedAccount.user_id == actor.user_id)
                .order_by(ConnectedAccount.created_at)
            )
        )
        .scalars()
        .all()
    )
    connections = []
    for account in accounts:
        out = ConnectionOut.model_validate(account)
        cp = (
            await session.execute(
                select(EmailSyncCheckpoint).where(EmailSyncCheckpoint.connected_account_id == account.id)
            )
        ).scalar_one_or_none()
        out.sync = SyncStateOut.model_validate(cp) if cp else None
        connections.append(out)
    ts = await get_tenant_settings(session, actor.tenant_id, settings)
    ado_mode = settings.azure_devops_mode
    user_connected = True
    if ado_mode is AzureDevOpsMode.ENTRA_DELEGATED:
        user_connected = await find_active_account(session, actor, ConnectionProvider.AZURE_DEVOPS) is not None
    # The user's own delegated Azure DevOps grant (it holds stored tokens), so the dashboard can revoke it.
    ado_connection = next(
        (
            c
            for c in reversed(connections)
            if c.provider is ConnectionProvider.AZURE_DEVOPS and c.status is not ConnectionStatus.DISCONNECTED
        ),
        None,
    )
    return IntegrationsOut(
        microsoft=MicrosoftIntegrationOut(
            mode=settings.mail_mode.value,
            entra_configured=settings.entra_configured,
            required_scopes=[*GRAPH_MAIL_SCOPES, "offline_access"],
            connections=[c for c in connections if c.provider is not ConnectionProvider.AZURE_DEVOPS],
        ),
        azure_devops=AzureDevOpsIntegrationOut(
            mode=ado_mode.value,
            organization=settings.azure_devops_organization if ado_mode is not AzureDevOpsMode.MOCK else "mock",
            configured=ado_mode is AzureDevOpsMode.MOCK or bool(settings.azure_devops_organization),
            requires_user_connection=ado_mode is AzureDevOpsMode.ENTRA_DELEGATED,
            user_connected=user_connected,
            connection=ado_connection,
            default_project=ts.azure_devops.project,
            default_work_item_type=ts.azure_devops.work_item_type,
            api_version=settings.azure_devops_api_version,
        ),
        llm=LLMIntegrationOut(
            provider=settings.llm_provider.value,
            model=settings.llm_model_label,
            structured_outputs=settings.llm_provider is not LLMProvider.HEURISTIC,
            observability_enabled=settings.otel_enabled,
        ),
        demo_mode=settings.demo_mode,
    )


async def _connect(
    purpose: OAuthPurpose, actor_user_id: uuid.UUID, session: SessionDep, settings: SettingsDep, redirect_to: str | None
) -> RedirectResponse:
    user = await session.get(User, actor_user_id)
    auth_uri, binding = await EntraAuthService(settings).begin(
        session, purpose, redirect_to=redirect_to or "/integrations", user=user
    )
    return _redirect_with_binding(auth_uri, binding, settings)


@router.get("/microsoft/connect", dependencies=[Depends(rate_limit("auth", 20, per="ip"))])
async def connect_microsoft(
    actor: ActorDep, session: SessionDep, settings: SettingsDep, redirect_to: str | None = None
) -> RedirectResponse:
    # Stores Mail.Read tokens: only for roles that may sync mail (viewers are read-only).
    require(actor, Perm.EMAIL_SYNC, resource_type="mailbox")
    return await _connect(OAuthPurpose.CONNECT_MAILBOX, actor.user_id, session, settings, redirect_to)


@router.get("/azure-devops/connect", dependencies=[Depends(rate_limit("auth", 20, per="ip"))])
async def connect_azure_devops(
    actor: ActorDep, session: SessionDep, settings: SettingsDep, redirect_to: str | None = None
) -> RedirectResponse:
    # Stores user_impersonation tokens, used only to execute approved work items.
    require(actor, Perm.ACTION_EXECUTE, resource_type="azure_devops")
    if settings.azure_devops_mode is not AzureDevOpsMode.ENTRA_DELEGATED:
        raise Conflict("Azure DevOps user connection is only used in entra_delegated mode.")
    return await _connect(OAuthPurpose.CONNECT_AZURE_DEVOPS, actor.user_id, session, settings, redirect_to)


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(connection_id: uuid.UUID, actor: ActorDep, session: SessionDep, settings: SettingsDep) -> Response:
    await disconnect_account(session, actor, connection_id, settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/azure-devops/test", response_model=AdoConnectionTestOut)
async def ado_test(actor: ActorDep, session: SessionDep, settings: SettingsDep) -> AdoConnectionTestOut:
    require(actor, Perm.ACTION_EDIT, resource_type="azure_devops")  # uses the service credentials
    try:
        client = await make_ado_client_factory(settings)(session, actor)
    except AppError as exc:
        return AdoConnectionTestOut(
            ok=False,
            mode=settings.azure_devops_mode.value,
            organization=settings.azure_devops_organization,
            message=exc.message,
        )
    try:
        check = await client.test_connection()
    finally:
        await client.aclose()
    return AdoConnectionTestOut.model_validate(check.model_dump())


@router.get("/azure-devops/projects", response_model=AdoProjectsOut)
async def ado_projects(actor: ActorDep, session: SessionDep, settings: SettingsDep) -> AdoProjectsOut:
    require(actor, Perm.ACTION_EDIT, resource_type="azure_devops")  # uses the service credentials
    client = await make_ado_client_factory(settings)(session, actor)
    try:
        return AdoProjectsOut(items=await client.list_projects())
    finally:
        await client.aclose()


@router.get("/azure-devops/projects/{project}/work-item-types", response_model=AdoWorkItemTypesOut)
async def ado_work_item_types(
    project: str, actor: ActorDep, session: SessionDep, settings: SettingsDep
) -> AdoWorkItemTypesOut:
    require(actor, Perm.ACTION_EDIT, resource_type="azure_devops")  # uses the service credentials
    client = await make_ado_client_factory(settings)(session, actor)
    try:
        return AdoWorkItemTypesOut(project=project, items=await client.list_work_item_types(project))
    finally:
        await client.aclose()


@router.post(
    "/azure-devops/validate", response_model=AdoValidateOut, dependencies=[Depends(rate_limit("validate", 30))]
)
async def ado_validate(
    body: AdoValidateRequest, actor: ActorDep, session: SessionDep, settings: SettingsDep
) -> AdoValidateOut:
    require(actor, Perm.ACTION_EDIT, resource_type="azure_devops")  # uses the service credentials
    data = dict(body.payload)
    data["kind"] = "create_azure_devops_work_item"
    if "source_email" not in data:
        email_id = (
            await session.execute(select(EmailMessage.id).where(EmailMessage.tenant_id == actor.tenant_id).limit(1))
        ).scalar_one_or_none()
        data["source_email"] = SourceEmailReference(email_id=email_id or uuid.uuid4()).model_dump(mode="json")
    try:
        payload = AzureDevOpsWorkItemPayload.model_validate(data)
    except ValidationError as exc:
        issues = [FieldIssue(field=".".join(str(p) for p in e["loc"]), message=e["msg"]) for e in exc.errors()]
        return AdoValidateOut(
            valid=False, schema_issues=issues, policy_issues=[], server_issues=[], server_validated=False
        )
    policy = ActionPolicy(await get_tenant_settings(session, actor.tenant_id, settings), settings)
    policy_issues = policy.policy_issues(ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM, payload)
    server_issues: list[FieldIssue] = []
    server_validated = False
    if body.server_side and not policy_issues:
        client = await make_ado_client_factory(settings)(session, actor)
        try:
            result = await client.validate_work_item(build_create_request(payload, idempotency_key="0" * 64))
            server_issues, server_validated = result.issues, True
        finally:
            await client.aclose()
    return AdoValidateOut(
        valid=not policy_issues and not server_issues,
        schema_issues=[],
        policy_issues=policy_issues,
        server_issues=server_issues,
        server_validated=server_validated,
    )
