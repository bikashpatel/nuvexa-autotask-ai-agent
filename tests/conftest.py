"""Shared fixtures. Default: SQLite file DB per test, heuristic agent, mock integrations, no network.

Set TEST_DATABASE_URL (postgresql+psycopg://…) to run the same suite against PostgreSQL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppEnv, AzureDevOpsMode, LLMProvider, MailMode, Settings
from app.core.timeutil import utcnow
from app.db.base import Base
from app.db.models import Tenant, User
from app.db.session import Database
from app.domain.enums import UserRole

TEST_FERNET_KEY = Fernet.generate_key().decode()


def make_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": AppEnv.TEST,
        "database_url": database_url,
        "demo_mode": True,
        "token_encryption_keys": TEST_FERNET_KEY,
        "llm_provider": LLMProvider.HEURISTIC,
        "azure_devops_mode": AzureDevOpsMode.MOCK,
        "mail_mode": MailMode.DEMO,
        "rate_limit_enabled": False,
        "log_format": "console",
        "otel_enabled": False,
        "azure_devops_default_project": "NUVEXA-Demo",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def settings(database_url: str) -> Settings:
    return make_settings(database_url)


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings)
    async with database.engine.begin() as conn:
        if settings.is_postgres:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield database
    finally:
        if settings.is_postgres:
            async with database.engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
        await database.dispose()


@pytest.fixture
async def session(db: Database) -> AsyncIterator[AsyncSession]:
    async with db.session() as s:
        yield s


UserFactory = Callable[..., Coroutine[Any, Any, User]]


@pytest.fixture
async def tenant(session: AsyncSession) -> Tenant:
    t = Tenant(name="Acme Test", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(t)
    await session.commit()
    return t


@pytest.fixture
def make_user(session: AsyncSession) -> UserFactory:
    async def _make(tenant: Tenant, role: UserRole = UserRole.MEMBER, email: str | None = None) -> User:
        user = User(
            tenant_id=tenant.id,
            email=email or f"user-{uuid.uuid4().hex[:8]}@example.com",
            display_name=f"Test {role.value}",
            role=role,
            is_active=True,
            last_login_at=utcnow(),
        )
        session.add(user)
        await session.commit()
        return user

    return _make
