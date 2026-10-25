"""LangGraph checkpointer selection: PostgreSQL in production, SQLite file for the local demo."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.config import Settings
from app.db.session import postgres_connect_args


def checkpoint_serializer() -> JsonPlusSerializer:
    """Strict msgpack allowlist: checkpoints never instantiate arbitrary classes on load.

    Graph state holds only primitives, so nothing beyond LangGraph's built-in safe types is needed; a
    row written by an attacker into the checkpoint tables cannot turn into code execution.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=None)


def _postgres_conninfo(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _sqlite_checkpoint_path(database_url: str) -> str:
    raw = database_url.split("///", 1)[1] if "///" in database_url else ""
    if not raw or raw == ":memory:":
        return ":memory:"
    path = Path(raw)
    return str(path.with_name(f"{path.stem}-checkpoints{path.suffix or '.db'}"))


@asynccontextmanager
async def open_checkpointer(settings: Settings, *, setup: bool = False) -> AsyncIterator[BaseCheckpointSaver[str]]:
    """The checkpointer for a worker. `setup=True` creates/migrates its tables (DDL): only `nuvexa migrate` passes it.

    api/worker connect as a runtime role without CREATE on the schema (app/db/privileges.py), so they must never run
    LangGraph's setup(): even its CREATE TABLE IF NOT EXISTS needs CREATE privilege.
    """
    if settings.is_postgres:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            _postgres_conninfo(settings.database_url),
            min_size=1,
            max_size=max(2, settings.worker_concurrency),
            # Same statement/lock timeouts as the SQLAlchemy engine: a checkpoint read/write stuck on a lock or a
            # runaway query must not pin a worker slot indefinitely.
            kwargs={
                **postgres_connect_args(settings),
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            open=False,
        )
        await pool.open()
        try:
            saver = AsyncPostgresSaver(pool, serde=checkpoint_serializer())  # type: ignore[arg-type]
            if setup:
                await saver.setup()
            yield saver
        finally:
            await pool.close()
    elif settings.database_url.startswith("sqlite"):
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        async with aiosqlite.connect(_sqlite_checkpoint_path(settings.database_url)) as conn:
            sqlite_saver = AsyncSqliteSaver(conn, serde=checkpoint_serializer())
            if setup:  # (the SQLite saver also creates its tables lazily; SQLite has no roles)
                await sqlite_saver.setup()
            yield sqlite_saver
    else:  # pragma: no cover - unsupported URL schemes fail elsewhere first
        yield InMemorySaver(serde=checkpoint_serializer())


async def setup_checkpointer(settings: Settings) -> None:
    """Create/upgrade checkpoint tables (run by `nuvexa migrate`, connected as the schema owner)."""
    async with open_checkpointer(settings, setup=True):
        pass
