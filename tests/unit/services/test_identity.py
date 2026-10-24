from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationRequired
from app.domain.enums import UserRole
from app.services.authz import ROLE_PERMISSIONS
from app.services.identity import actor_from_user, load_actor
from tests.unit.services.conftest import Factory, World


async def test_actor_from_user_maps_identity_and_role(world: World) -> None:
    actor = actor_from_user(world.approver, "198.51.100.4")
    assert actor.user_id == world.approver.id
    assert actor.tenant_id == world.tenant.id
    assert actor.role is UserRole.APPROVER
    assert actor.email == "approver@example.com"
    assert actor.display_name == world.approver.display_name
    assert actor.ip_address == "198.51.100.4"
    assert actor.permissions == ROLE_PERMISSIONS[UserRole.APPROVER]
    assert actor_from_user(world.owner).ip_address is None


async def test_load_actor_returns_current_role(session: AsyncSession, world: World) -> None:
    world.owner.role = UserRole.ADMIN  # role changed since the request was made
    await session.commit()
    actor = await load_actor(session, world.owner.id)
    assert actor.role is UserRole.ADMIN
    assert actor.user_id == world.owner.id
    assert actor.ip_address is None


async def test_load_actor_rejects_missing_user(session: AsyncSession, world: World) -> None:
    with pytest.raises(AuthenticationRequired):
        await load_actor(session, uuid.uuid4())


async def test_load_actor_rejects_inactive_user(session: AsyncSession, factory: Factory, world: World) -> None:
    gone = await factory.user(world.tenant, UserRole.APPROVER, active=False)
    with pytest.raises(AuthenticationRequired) as info:
        await load_actor(session, gone.id)
    assert info.value.status_code == 401
