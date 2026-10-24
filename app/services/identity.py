"""Loading actors from the database (used by the API session layer and by worker jobs)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationRequired
from app.db.models import User
from app.services.authz import Actor


def actor_from_user(user: User, ip_address: str | None = None) -> Actor:
    return Actor.build(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        email=user.email,
        display_name=user.display_name,
        ip_address=ip_address,
    )


async def load_actor(session: AsyncSession, user_id: uuid.UUID) -> Actor:
    """Re-load the actor for background work: role or deactivation changes since the request apply."""
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationRequired("The requesting user no longer exists or is inactive.")
    return actor_from_user(user)
