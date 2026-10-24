"""LangGraph checkpointer selection: PostgreSQL in production, SQLite file for the local demo."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.core.config import Settings


def _postgres_conninfo(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _sqlite_checkpoint_path(database_url: str) -> str:
    raw = database_url.split("///", 1)[1] if "///" in database_url else ""
    if not raw or raw == ":memory:":
        return ":memory:"
    path = Path(raw)
    return str(path.with_name(f"{path.stem}-checkpoints{path.suffix or '.db'}"))


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[BaseCheckpointSaver[str]]:
    if settings.is_postgres:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            _postgres_conninfo(settings.database_url),
            min_size=1,
            max_size=max(2, settings.worker_concurrency),
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            open=False,
        )
        await pool.open()
        try:
            saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
            await saver.setup()
            yield saver
        finally:
            await pool.close()
    elif settings.database_url.startswith("sqlite"):
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        async with AsyncSqliteSaver.from_conn_string(_sqlite_checkpoint_path(settings.database_url)) as sqlite_saver:
            await sqlite_saver.setup()
            yield sqlite_saver
    else:  # pragma: no cover - unsupported URL schemes fail elsewhere first
        yield InMemorySaver()


async def setup_checkpointer(settings: Settings) -> None:
    """Create/upgrade checkpoint tables (run by `nuvexa migrate`)."""
    async with open_checkpointer(settings):
        pass
