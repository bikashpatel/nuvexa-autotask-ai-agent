"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings


def postgres_connect_args(settings: Settings) -> dict[str, Any]:
    """Per-connection limits so a runaway query or lock wait cannot pin a worker or connection forever."""
    options = (
        f"-c statement_timeout={settings.database_statement_timeout_ms} "
        f"-c lock_timeout={settings.database_lock_timeout_ms}"
    )
    return {"options": options}


def create_engine(settings: Settings | None = None, url: str | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    url = url or settings.database_url
    kwargs: dict[str, Any] = {"echo": settings.database_echo, "pool_pre_ping": True}
    if url.startswith("postgresql"):
        kwargs.update(
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_pool_size,
            connect_args=postgres_connect_args(settings),
        )
    engine = create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):
        _enable_sqlite_foreign_keys(engine.sync_engine)
    return engine


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


class Database:
    """Holds the engine and session factory for a process (API or worker)."""

    def __init__(self, settings: Settings | None = None, url: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.engine = create_engine(self.settings, url)
        self.session_factory = create_session_factory(self.engine)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
