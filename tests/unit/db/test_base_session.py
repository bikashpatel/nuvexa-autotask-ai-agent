"""Portable column types, engine creation and session handling."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import String, select, text
from sqlalchemy.dialects import postgresql, sqlite

from app.core.config import Settings
from app.db.base import NAMING_CONVENTION, Base, UTCDateTime, short_str, str_enum
from app.db.models import Job, Tenant
from app.db.session import Database, create_engine, create_session_factory
from app.domain.enums import JobStatus, JobType
from tests.conftest import make_settings

PLUS2 = timezone(timedelta(hours=2))


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_utc_datetime_bind_and_result(dialect: object) -> None:
    col = UTCDateTime()
    naive = datetime(2026, 1, 1, 12, 0)
    aware = datetime(2026, 1, 1, 14, 0, tzinfo=PLUS2)
    for fn in (col.process_bind_param, col.process_result_value):
        assert fn(None, dialect) is None  # type: ignore[arg-type]
        out_naive = fn(naive, dialect)  # type: ignore[arg-type]
        assert out_naive == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        assert out_naive is not None
        assert out_naive.tzinfo is UTC
        out_aware = fn(aware, dialect)  # type: ignore[arg-type]
        assert out_aware is not None
        assert out_aware.tzinfo is UTC
        assert out_aware.hour == 12
    assert col.cache_ok is True
    assert col.impl.timezone is True  # type: ignore[attr-defined]


def test_str_enum_is_portable_varchar() -> None:
    enum_type = str_enum(JobStatus)
    assert enum_type.native_enum is False
    assert enum_type.length == 40
    assert enum_type.enums == [s.value for s in JobStatus]
    assert enum_type.validate_strings is True
    assert str_enum(JobStatus, length=12).length == 12


def test_short_str_and_naming_convention() -> None:
    s = short_str()
    assert isinstance(s, String)
    assert s.length == 255
    assert short_str(10).length == 10
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    assert "uq_jobs_dedupe_active" in {ix.name for ix in Job.__table__.indexes}


async def test_datetimes_roundtrip_as_aware_utc(db: Database) -> None:
    instant = datetime(2026, 5, 6, 9, 30, tzinfo=PLUS2)
    async with db.session() as s:
        job = Job(job_type=JobType.RETENTION_PURGE, payload={}, run_after=instant, tenant_id=None)
        s.add(job)
        await s.commit()
        job_id = job.id
    async with db.session() as s:
        loaded = await s.get(Job, job_id)
        assert loaded is not None
        assert loaded.run_after.tzinfo is not None
        assert loaded.run_after.utcoffset() == timedelta(0)
        assert loaded.run_after == instant
        assert loaded.created_at.tzinfo is not None
        # Defaults apply: uuid primary key, timestamps, enum default
        assert isinstance(loaded.id, uuid.UUID)
        assert loaded.status is JobStatus.QUEUED


async def test_enums_are_stored_as_their_values(db: Database) -> None:
    async with db.session() as s:
        s.add(Job(job_type=JobType.SYNC_MAILBOX, payload={"k": [1, 2]}, run_after=datetime.now(UTC)))
        await s.commit()
        raw = (await s.execute(text("SELECT job_type, status FROM jobs"))).one()
        assert tuple(raw) == ("sync_mailbox", "queued")
        payload = (await s.execute(select(Job.payload))).scalar_one()
        assert payload == {"k": [1, 2]}


async def test_timestamps_update_on_change(db: Database) -> None:
    async with db.session() as s:
        tenant = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
        s.add(tenant)
        await s.commit()
        created, updated = tenant.created_at, tenant.updated_at
        tenant.name = "T2"
        await s.commit()
        assert tenant.created_at == created
        assert tenant.updated_at >= updated


def test_create_engine_sqlite_and_session_factory(tmp_path: Path) -> None:
    settings = make_settings(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}", database_echo=True)
    engine = create_engine(settings)
    try:
        assert engine.url.drivername == "sqlite+aiosqlite"
        assert engine.echo is True
        factory = create_session_factory(engine)
        assert factory.kw["expire_on_commit"] is False
        assert factory.kw["autoflush"] is False
    finally:
        engine.sync_engine.dispose()


def test_create_engine_postgres_uses_pool_settings(tmp_path: Path) -> None:
    # No connection is opened: engines connect lazily.
    settings = make_settings(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}", database_pool_size=7)
    engine = create_engine(settings, url="postgresql+psycopg://u:p@127.0.0.1:1/never")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.pool.size() == 7  # type: ignore[attr-defined]
        assert engine.pool._max_overflow == 7  # type: ignore[attr-defined]
        assert engine.pool._pre_ping is True
    finally:
        engine.sync_engine.dispose()


def test_create_engine_defaults_to_process_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sentinel: Settings = make_settings(f"sqlite+aiosqlite:///{tmp_path / 'default.db'}")
    monkeypatch.setattr("app.db.session.get_settings", lambda: sentinel)
    engine = create_engine()
    try:
        assert str(engine.url).endswith("default.db")
    finally:
        engine.sync_engine.dispose()
    database = Database()
    assert database.settings is sentinel
    database.engine.sync_engine.dispose()


async def test_sqlite_pragmas_are_applied_on_connect(tmp_path: Path) -> None:
    database = Database(make_settings(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}"))
    try:
        async with database.engine.connect() as conn:
            assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
            assert (await conn.execute(text("PRAGMA journal_mode"))).scalar_one() == "wal"
            assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar_one() == 5000
    finally:
        await database.dispose()


async def test_database_session_and_dispose(tmp_path: Path) -> None:
    database = Database(make_settings(f"sqlite+aiosqlite:///{tmp_path / 's.db'}"))
    async with database.session() as s:
        assert (await s.execute(text("SELECT 1"))).scalar_one() == 1
        assert s.bind is database.engine
    await database.dispose()
    # The engine can be used again after dispose (a fresh pool is created).
    async with database.session() as s:
        assert (await s.execute(text("SELECT 2"))).scalar_one() == 2
    await database.dispose()


async def test_url_override_wins_over_settings(tmp_path: Path) -> None:
    database = Database(
        make_settings(f"sqlite+aiosqlite:///{tmp_path / 'ignored.db'}"), url=f"sqlite+aiosqlite:///{tmp_path / 'o.db'}"
    )
    try:
        assert str(database.engine.url).endswith("o.db")
    finally:
        await database.dispose()
