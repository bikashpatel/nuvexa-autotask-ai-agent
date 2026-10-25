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

# Test layers (docs/TESTING_STRATEGY.md): the marker follows the directory, so `pytest -m unit` etc. work
# without anyone having to remember to tag a test.
_LAYER_BY_DIRECTORY = {"unit": "unit", "integration": "integration", "security": "security", "evaluation": "evaluation"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    tests_root = Path(__file__).resolve().parent
    layerless: list[str] = []
    for item in items:
        try:
            relative = Path(str(item.path)).resolve().relative_to(tests_root)
        except ValueError:
            continue  # outside tests/ (e.g. e2e/), which has its own conftest
        layer = _LAYER_BY_DIRECTORY.get(relative.parts[0])
        if layer is None:
            layerless.append(relative.as_posix())
        else:
            item.add_marker(getattr(pytest.mark, layer))
    if layerless:
        # CI runs each layer as its own step; a test in no layer would silently run in none of them.
        layers = ",".join(_LAYER_BY_DIRECTORY)
        raise pytest.UsageError(f"Tests must live under tests/{{{layers}}}/; move: {', '.join(sorted(set(layerless)))}")


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
