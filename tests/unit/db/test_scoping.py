"""Tenant / owner scoping of queries."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.timeutil import utcnow
from app.db.models import AuditEvent, FollowUpTask, Tenant, User
from app.domain.enums import ActorType, AuditOutcome, UserRole
from app.repositories.scoping import count_rows, get_visible, paginate, visible
from app.services.identity import actor_from_user
from tests.conftest import UserFactory


async def _task(session: AsyncSession, owner: User, title: str) -> FollowUpTask:
    task = FollowUpTask(tenant_id=owner.tenant_id, owner_user_id=owner.id, title=title)
    session.add(task)
    await session.commit()
    return task


async def _audit(session: AsyncSession, tenant: Tenant, event_type: str) -> AuditEvent:
    event = AuditEvent(
        tenant_id=tenant.id,
        actor_type=ActorType.SYSTEM,
        event_type=event_type,
        resource_type="x",
        outcome=AuditOutcome.SUCCESS,
        created_at=utcnow(),
    )
    session.add(event)
    await session.commit()
    return event


@pytest.fixture
async def world(session: AsyncSession, tenant: Tenant, make_user: UserFactory) -> dict[str, object]:
    other_tenant = Tenant(name="Other", slug=f"other-{uuid.uuid4().hex[:6]}")
    session.add(other_tenant)
    await session.commit()
    members = {role: await make_user(tenant, role) for role in UserRole}
    peer = await make_user(tenant, UserRole.MEMBER)
    outsider = await make_user(other_tenant, UserRole.ADMIN)
    own = await _task(session, members[UserRole.MEMBER], "own")
    peers = await _task(session, peer, "peer")
    foreign = await _task(session, outsider, "foreign")
    await _audit(session, tenant, "mine")
    await _audit(session, other_tenant, "theirs")
    return {
        "members": members,
        "peer": peer,
        "outsider": outsider,
        "own": own,
        "peers": peers,
        "foreign": foreign,
        "other_tenant": other_tenant,
    }


async def _titles(session: AsyncSession, actor_user: User) -> set[str]:
    actor = actor_from_user(actor_user)
    rows = (await session.execute(visible(select(FollowUpTask), FollowUpTask, actor))).scalars().all()
    return {r.title for r in rows}


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (UserRole.VIEWER, set()),
        (UserRole.MEMBER, {"own"}),
        (UserRole.APPROVER, {"own", "peer"}),
        (UserRole.ADMIN, {"own", "peer"}),
    ],
)
async def test_visible_owner_vs_tenant_wide(
    session: AsyncSession, world: dict[str, object], role: UserRole, expected: set[str]
) -> None:
    members: dict[UserRole, User] = world["members"]  # type: ignore[assignment]
    assert await _titles(session, members[role]) == expected


async def test_tables_without_owner_are_tenant_scoped_only(session: AsyncSession, world: dict[str, object]) -> None:
    members: dict[UserRole, User] = world["members"]  # type: ignore[assignment]
    actor = actor_from_user(members[UserRole.VIEWER])  # not tenant-wide, but AuditEvent has no owner column
    rows = (await session.execute(visible(select(AuditEvent), AuditEvent, actor))).scalars().all()
    assert {r.event_type for r in rows} == {"mine"}


async def test_get_visible(session: AsyncSession, world: dict[str, object]) -> None:
    members: dict[UserRole, User] = world["members"]  # type: ignore[assignment]
    own: FollowUpTask = world["own"]  # type: ignore[assignment]
    peers: FollowUpTask = world["peers"]  # type: ignore[assignment]
    foreign: FollowUpTask = world["foreign"]  # type: ignore[assignment]
    member = actor_from_user(members[UserRole.MEMBER])
    admin = actor_from_user(members[UserRole.ADMIN])

    assert (await get_visible(session, FollowUpTask, member, own.id)).id == own.id
    assert (await get_visible(session, FollowUpTask, admin, peers.id, for_update=True)).id == peers.id
    with pytest.raises(NotFound, match="FollowUpTask not found"):
        await get_visible(session, FollowUpTask, member, peers.id)
    with pytest.raises(NotFound, match="Task not found"):
        await get_visible(session, FollowUpTask, admin, foreign.id, label="Task")  # other tenant → 404, not 403
    with pytest.raises(NotFound):
        await get_visible(session, FollowUpTask, admin, uuid.uuid4())


async def test_paginate_and_count(session: AsyncSession, tenant: Tenant, make_user: UserFactory) -> None:
    owner = await make_user(tenant, UserRole.MEMBER)
    for i in range(5):
        await _task(session, owner, f"t{i}")
    stmt = select(FollowUpTask).where(FollowUpTask.owner_user_id == owner.id).order_by(FollowUpTask.title)
    assert await count_rows(session, stmt) == 5
    rows, total = await paginate(session, stmt, limit=2, offset=1)
    assert total == 5
    assert [r.title for r in rows] == ["t1", "t2"]
    rows, total = await paginate(session, stmt, limit=10, offset=4)
    assert ([r.title for r in rows], total) == (["t4"], 5)
    rows, total = await paginate(session, stmt, limit=10, offset=50)
    assert (rows, total) == ([], 5)
    empty = select(FollowUpTask).where(FollowUpTask.title == "none")
    assert await count_rows(session, empty) == 0
