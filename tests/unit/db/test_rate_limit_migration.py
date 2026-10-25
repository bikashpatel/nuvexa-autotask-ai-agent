"""The `rate_limit_counters` migration upgrades, matches the model, and downgrades cleanly (SQLite, empty database).

PostgreSQL is verified by `alembic upgrade head` / `downgrade -1` against an empty database (docs/TESTING_STRATEGY.md).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext

from app.core.config import get_settings
from app.db.base import Base

REPO = Path(__file__).resolve().parents[3]
REVISION = "b5d1f0c3a2e7"
TABLE = "rate_limit_counters"


@pytest.fixture
def migrated_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    path = tmp_path / "migrations.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    get_settings.cache_clear()  # app/db/migrations/env.py reads DATABASE_URL through get_settings()
    try:
        yield f"sqlite:///{path}"
    finally:
        get_settings.cache_clear()


def alembic_config() -> Config:
    cfg = Config()  # no ini file: the test process's logging configuration stays untouched
    cfg.set_main_option("script_location", str(REPO / "app" / "db" / "migrations"))
    return cfg


def table_diffs(engine: sa.Engine) -> list[Any]:
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    return [d for d in diffs if TABLE in repr(d)]


def test_upgrade_creates_the_table_as_modelled_and_downgrade_removes_it(migrated_url: str) -> None:
    cfg = alembic_config()
    engine = sa.create_engine(migrated_url)
    try:
        command.upgrade(cfg, "head")
        inspector = sa.inspect(engine)
        assert TABLE in inspector.get_table_names()
        assert inspector.get_pk_constraint(TABLE)["constrained_columns"] == ["bucket", "window_index"]
        assert {ix["name"]: ix["column_names"] for ix in inspector.get_indexes(TABLE)} == {
            "ix_rate_limit_counters_expires_at": ["expires_at"]
        }
        assert table_diffs(engine) == []

        command.downgrade(cfg, f"{REVISION}-1")
        tables = sa.inspect(engine).get_table_names()
        assert TABLE not in tables
        assert "audit_events" in tables  # only this revision was undone

        command.upgrade(cfg, "head")  # and it can be applied again
        assert TABLE in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
