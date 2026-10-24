"""Tenant settings (defaults seeded from deployment configuration)."""

from __future__ import annotations

import uuid

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AzureDevOpsMode, Settings
from app.core.errors import StaleVersion
from app.db.models import AppSettings
from app.integrations.azure_devops.mock import MOCK_PROJECTS
from app.observability.logging import get_logger
from app.schemas.settings import AzureDevOpsDefaults, TenantSettings
from app.services.audit import record_audit
from app.services.authz import Actor, Perm, require

log = get_logger(__name__)


def default_tenant_settings(settings: Settings) -> TenantSettings:
    project = settings.azure_devops_default_project
    if project is None and settings.azure_devops_mode is AzureDevOpsMode.MOCK:
        project = MOCK_PROJECTS[0]  # mock mode works out of the box; real modes must be configured
    return TenantSettings(
        azure_devops=AzureDevOpsDefaults(
            project=project,
            work_item_type=settings.azure_devops_default_work_item_type,
            area_path=settings.azure_devops_default_area_path,
            iteration_path=settings.azure_devops_default_iteration_path,
        )
    )


async def load_settings_row(session: AsyncSession, tenant_id: uuid.UUID, settings: Settings) -> AppSettings:
    stmt = select(AppSettings).where(AppSettings.tenant_id == tenant_id)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row
    candidate = AppSettings(
        tenant_id=tenant_id, settings=default_tenant_settings(settings).model_dump(mode="json"), version=1
    )
    try:
        async with session.begin_nested():  # savepoint: a lost race must not abort the caller's transaction
            session.add(candidate)
    except IntegrityError:
        # Another transaction created the tenant's settings first; use the committed row.
        return (await session.execute(stmt)).scalar_one()
    return candidate


async def get_tenant_settings(session: AsyncSession, tenant_id: uuid.UUID, settings: Settings) -> TenantSettings:
    row = await load_settings_row(session, tenant_id, settings)
    try:
        return TenantSettings.model_validate(row.settings)
    except ValidationError:
        # Never fail a workflow because stored settings drifted from the schema; fall back to defaults.
        log.warning("tenant_settings_invalid_using_defaults", tenant_id=str(tenant_id))
        return default_tenant_settings(settings)


async def update_tenant_settings(
    session: AsyncSession, actor: Actor, *, expected_version: int, new: TenantSettings, settings: Settings
) -> AppSettings:
    require(actor, Perm.SETTINGS_WRITE, resource_type="settings")
    row = await load_settings_row(session, actor.tenant_id, settings)
    if row.version != expected_version:
        raise StaleVersion("Settings were changed by someone else. Reload and try again.")
    before = row.settings
    row.settings = new.model_dump(mode="json")
    row.version += 1
    row.updated_by_user_id = actor.user_id
    changed = sorted(k for k in row.settings if row.settings.get(k) != before.get(k))
    record_audit(
        session,
        tenant_id=actor.tenant_id,
        actor=actor,
        event_type="settings_updated",
        resource_type="settings",
        resource_id=row.id,
        details={"changed_sections": changed, "version": row.version},
    )
    await session.commit()
    return row
