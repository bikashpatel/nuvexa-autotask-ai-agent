"""Checkpointer selection: SQLite file for the demo, PostgreSQL pool in production (mocked here)."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.workflows import checkpointer as cp
from tests.conftest import make_settings


def test_postgres_conninfo_drops_the_sqlalchemy_driver() -> None:
    assert cp._postgres_conninfo("postgresql+psycopg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert cp._postgres_conninfo("postgresql://u:p@h/db") == "postgresql://u:p@h/db"
    # Only the scheme prefix is rewritten.
    assert cp._postgres_conninfo("postgresql+psycopg://u:postgresql+psycopg://@h/db").count("+psycopg") == 1


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite+aiosqlite:////var/data/app.db", "/var/data/app-checkpoints.db"),
        ("sqlite+aiosqlite:///./data/nuvexa-demo.db", "data/nuvexa-demo-checkpoints.db"),
        ("sqlite+aiosqlite:///relative.sqlite3", "relative-checkpoints.sqlite3"),
        ("sqlite:///nosuffix", "nosuffix-checkpoints.db"),
        ("sqlite+aiosqlite:///:memory:", ":memory:"),
        ("sqlite+aiosqlite://", ":memory:"),
        ("sqlite+aiosqlite:///", ":memory:"),
    ],
)
def test_sqlite_checkpoint_path(url: str, expected: str) -> None:
    assert cp._sqlite_checkpoint_path(url) == expected


async def test_sqlite_checkpointer_is_created_next_to_the_database(tmp_path: Path) -> None:
    settings = make_settings(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    async with cp.open_checkpointer(settings) as saver:
        assert isinstance(saver, AsyncSqliteSaver)
        config: Any = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
        assert await saver.aget_tuple(config) is None
    path = tmp_path / "app-checkpoints.db"
    assert path.exists()
    with contextlib.closing(sqlite3.connect(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"checkpoints", "writes"} <= tables


async def test_setup_checkpointer_creates_tables(tmp_path: Path) -> None:
    await cp.setup_checkpointer(make_settings(f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}"))
    assert (tmp_path / "mig-checkpoints.db").exists()
    await cp.setup_checkpointer(make_settings(f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}"))  # idempotent
    with contextlib.closing(sqlite3.connect(tmp_path / "mig-checkpoints.db")) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"checkpoints", "writes"} <= tables


class _FakePool:
    instances: list[_FakePool] = []

    def __class_getitem__(cls, _item: Any) -> type[_FakePool]:  # used in type annotations of the real module
        return cls

    def __init__(self, conninfo: str, **kwargs: Any) -> None:
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.events: list[str] = []
        _FakePool.instances.append(self)

    async def open(self) -> None:
        self.events.append("open")

    async def close(self) -> None:
        self.events.append("close")


class _FakeSaver:
    def __init__(self, pool: _FakePool, *args: Any, **kwargs: Any) -> None:
        self.pool = pool

    async def setup(self) -> None:
        self.pool.events.append("setup")


@pytest.fixture
def fake_postgres(monkeypatch: pytest.MonkeyPatch) -> type[_FakePool]:
    import langgraph.checkpoint.postgres.aio  # noqa: F401 - import the real module before patching its deps

    _FakePool.instances = []
    monkeypatch.setattr("psycopg_pool.AsyncConnectionPool", _FakePool)
    monkeypatch.setattr("langgraph.checkpoint.postgres.aio.AsyncPostgresSaver", _FakeSaver)
    return _FakePool


async def test_postgres_checkpointer_uses_a_pool_and_closes_it(fake_postgres: type[_FakePool]) -> None:
    from psycopg.rows import dict_row

    settings = make_settings("postgresql+psycopg://u:p@db:5432/nuvexa", worker_concurrency=6)
    async with cp.open_checkpointer(settings) as saver:
        assert isinstance(saver, _FakeSaver)
        (pool,) = fake_postgres.instances
        assert saver.pool is pool
        assert pool.events == ["open"]
    assert pool.conninfo == "postgresql://u:p@db:5432/nuvexa"
    assert pool.kwargs["min_size"] == 1
    assert pool.kwargs["max_size"] == 6
    assert pool.kwargs["open"] is False  # opened explicitly inside the event loop
    connection_kwargs = pool.kwargs["kwargs"]
    assert connection_kwargs["autocommit"] is True
    assert connection_kwargs["prepare_threshold"] == 0
    assert connection_kwargs["row_factory"] is dict_row
    assert pool.events == ["open", "close"]


# Regression (R-4): the runtime (api/worker) connects as a role without CREATE on the schema, so the checkpointer it
# opens must never run LangGraph's setup() (CREATE TABLE IF NOT EXISTS / migrations). Only `nuvexa migrate` does.
async def test_the_runtime_checkpointer_never_runs_ddl(fake_postgres: type[_FakePool]) -> None:
    async with cp.open_checkpointer(make_settings("postgresql+psycopg://u:p@db/n")):
        pass
    assert "setup" not in fake_postgres.instances[0].events


async def test_setup_checkpointer_creates_the_postgres_tables(fake_postgres: type[_FakePool]) -> None:
    await cp.setup_checkpointer(make_settings("postgresql+psycopg://owner:p@db/n"))
    (pool,) = fake_postgres.instances
    assert pool.conninfo == "postgresql://owner:p@db/n"
    assert pool.events == ["open", "setup", "close"]


async def test_postgres_pool_has_a_floor_of_two_connections(fake_postgres: type[_FakePool]) -> None:
    async with cp.open_checkpointer(make_settings("postgresql+psycopg://u:p@db/n", worker_concurrency=1)):
        pass
    assert fake_postgres.instances[0].kwargs["max_size"] == 2


async def test_postgres_pool_is_closed_when_the_body_fails(fake_postgres: type[_FakePool]) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with cp.open_checkpointer(make_settings("postgresql+psycopg://u:p@db/n")):
            raise RuntimeError("boom")
    assert fake_postgres.instances[0].events == ["open", "close"]


async def test_postgres_pool_is_closed_when_setup_fails(fake_postgres: type[_FakePool], monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def failing_setup(self: _FakeSaver) -> None:
        raise ConnectionError("db down")

    monkeypatch.setattr(_FakeSaver, "setup", failing_setup)
    with pytest.raises(ConnectionError):
        await cp.setup_checkpointer(make_settings("postgresql+psycopg://u:p@db/n"))
    assert fake_postgres.instances[0].events == ["open", "close"]
