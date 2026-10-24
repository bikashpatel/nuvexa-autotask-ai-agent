from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import StaleVersion
from app.db.models import AppSettings
from app.domain.enums import UserRole
from app.integrations.azure_devops.mock import MOCK_PROJECTS
from app.schemas.settings import TenantSettings
from app.services.authz import AuditedPermissionDenied, Perm
from app.services.settings_service import (
    default_tenant_settings,
    get_tenant_settings,
    load_settings_row,
    update_tenant_settings,
)
from tests.conftest import make_settings
from tests.unit.services.conftest import Factory, Fresh, World


def test_defaults_use_configured_project_and_paths() -> None:
    settings = make_settings(
        "sqlite+aiosqlite://",
        azure_devops_default_project="Customer-Portal",
        azure_devops_default_work_item_type="Bug",
        azure_devops_default_area_path="Portal\\Auth",
        azure_devops_default_iteration_path="Portal\\Sprint 1",
    )
    ado = default_tenant_settings(settings).azure_devops
    assert ado.project == "Customer-Portal"
    assert ado.work_item_type == "Bug"
    assert ado.area_path == "Portal\\Auth"
    assert ado.iteration_path == "Portal\\Sprint 1"


def test_defaults_fall_back_to_mock_project_only_in_mock_mode() -> None:
    mock = make_settings("sqlite+aiosqlite://", azure_devops_default_project=None)
    assert default_tenant_settings(mock).azure_devops.project == MOCK_PROJECTS[0]

    real = make_settings(
        "sqlite+aiosqlite://",
        azure_devops_default_project=None,
        azure_devops_mode="pat",
        azure_devops_organization="contoso-test",
        azure_devops_pat="fake-pat-for-unit-tests-only",
    )
    assert default_tenant_settings(real).azure_devops.project is None  # real modes must be configured


async def test_load_settings_row_seeds_defaults_once(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh
) -> None:
    row = await load_settings_row(session, world.tenant.id, settings)
    assert row.version == 1
    assert row.settings == default_tenant_settings(settings).model_dump(mode="json")
    assert row.id is not None  # flushed
    await session.commit()
    again = await load_settings_row(session, world.tenant.id, settings)
    assert again.id == row.id
    assert len(await fresh.all(AppSettings)) == 1


async def test_get_tenant_settings_returns_stored_values(
    session: AsyncSession, settings: Settings, world: World
) -> None:
    custom = TenantSettings.model_validate(
        {"azure_devops": {"project": "Customer-Portal"}, "auto_analyze_new_emails": True}
    )
    session.add(AppSettings(tenant_id=world.tenant.id, settings=custom.model_dump(mode="json"), version=4))
    await session.commit()
    ts = await get_tenant_settings(session, world.tenant.id, settings)
    assert ts.azure_devops.project == "Customer-Portal"
    assert ts.auto_analyze_new_emails is True


async def test_get_tenant_settings_falls_back_to_defaults_on_invalid_stored_json(
    session: AsyncSession, settings: Settings, world: World
) -> None:
    session.add(
        AppSettings(
            tenant_id=world.tenant.id,
            settings={"azure_devops": {"project": "X", "unknown_field": 1}, "retention": {"audit_days": 1}},
            version=2,
        )
    )
    await session.commit()
    ts = await get_tenant_settings(session, world.tenant.id, settings)
    assert ts == default_tenant_settings(settings)


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.MEMBER, UserRole.APPROVER])
async def test_update_requires_admin(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh, role: UserRole
) -> None:
    user = await factory.user(world.tenant, role)
    with pytest.raises(AuditedPermissionDenied) as info:
        await update_tenant_settings(
            session, world.actor(user), expected_version=1, new=TenantSettings(), settings=settings
        )
    assert info.value.permission == Perm.SETTINGS_WRITE
    assert info.value.resource_type == "settings"
    assert await fresh.all(AppSettings) == []


async def test_update_rejects_stale_version(session: AsyncSession, settings: Settings, world: World) -> None:
    with pytest.raises(StaleVersion):
        await update_tenant_settings(
            session, world.actor(world.admin), expected_version=7, new=TenantSettings(), settings=settings
        )


async def test_update_bumps_version_and_audits_changed_sections(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh
) -> None:
    current = await get_tenant_settings(session, world.tenant.id, settings)
    new = current.model_copy(
        update={
            "auto_analyze_new_emails": True,
            "azure_devops": current.azure_devops.model_copy(update={"allowed_projects": ["NUVEXA-Demo"]}),
        }
    )
    row = await update_tenant_settings(
        session, world.actor(world.admin), expected_version=1, new=new, settings=settings
    )
    assert row.version == 2

    stored = await fresh.get(AppSettings, row.id)
    assert stored is not None
    assert stored.version == 2
    assert stored.updated_by_user_id == world.admin.id
    assert TenantSettings.model_validate(stored.settings) == new
    [audit] = await fresh.audits("settings_updated")
    assert audit.actor_user_id == world.admin.id
    assert audit.resource_id == str(row.id)
    assert audit.details == {"changed_sections": ["auto_analyze_new_emails", "azure_devops"], "version": 2}

    with pytest.raises(StaleVersion):  # the old version is now stale
        await update_tenant_settings(session, world.actor(world.admin), expected_version=1, new=new, settings=settings)


async def test_update_without_changes_still_versions_and_audits(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh
) -> None:
    same = await get_tenant_settings(session, world.tenant.id, settings)
    row = await update_tenant_settings(
        session, world.actor(world.admin), expected_version=1, new=same, settings=settings
    )
    assert row.version == 2
    [audit] = await fresh.audits("settings_updated")
    assert audit.details["changed_sections"] == []
