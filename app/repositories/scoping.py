"""Tenant/owner scoping applied to every query on business tables."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.services.authz import Actor

# SQLAlchemy 2.1 `Select` is variadic over its column types.
type AnySelect = Select[*tuple[Any, ...]]


def visible[S: AnySelect](stmt: S, model: Any, actor: Actor) -> S:
    """Restrict to the actor's tenant, and to owned rows unless the actor sees the whole tenant."""
    scoped: Any = stmt.where(model.tenant_id == actor.tenant_id)
    if not actor.sees_tenant and hasattr(model, "owner_user_id"):
        scoped = scoped.where(model.owner_user_id == actor.user_id)
    return cast(S, scoped)


async def get_visible[T](
    session: AsyncSession,
    model: type[T],
    actor: Actor,
    obj_id: uuid.UUID,
    *,
    for_update: bool = False,
    label: str | None = None,
) -> T:
    stmt = visible(select(model).where(model.id == obj_id), model, actor)  # type: ignore[attr-defined]
    if for_update:
        stmt = stmt.with_for_update()
    result = (await session.execute(stmt)).scalar_one_or_none()
    if result is None:
        raise NotFound(f"{label or model.__name__} not found.")
    return result


async def count_rows(session: AsyncSession, stmt: AnySelect) -> int:
    return int((await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one())


async def paginate[T](session: AsyncSession, stmt: Select[T], *, limit: int, offset: int) -> tuple[list[T], int]:
    total = await count_rows(session, stmt)
    rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return list(rows), total
