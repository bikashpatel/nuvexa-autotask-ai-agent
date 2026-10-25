"""Least-privilege runtime database role (risk R-4): api/worker can read and write rows, never change the schema.

Every test creates its own throwaway LOGIN role, grants it exactly what `nuvexa migrate` grants the runtime role
(app/db/privileges.py — the production code path) and then connects *as that role*. The owner connection of the
test suite (TEST_DATABASE_URL) plays the migration role. PostgreSQL only; skipped on SQLite.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import AppEnv, Settings
from app.core.errors import ConfigurationError
from app.core.timeutil import utcnow
from app.db.models import AuditEvent, Job, Tenant
from app.db.privileges import check_runtime_privileges, grant_runtime_privileges, inspect_current_role
from app.db.session import Database
from app.domain.enums import ActorType, AuditOutcome, JobStatus, JobType
from app.integrations.microsoft_graph.demo_mailbox import load_demo_fixture
from app.services.audit import compute_audit_integrity, configure_audit_signing
from app.services.retention_service import purge_tenant
from app.workers import queue
from app.workers.runner import worker_context
from app.workflows.checkpointer import open_checkpointer, setup_checkpointer
from tests.conftest import make_settings
from tests.integration.conftest import analyze, approve_and_execute, build_harness, email_id_by_provider_id

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
        reason="requires PostgreSQL (set TEST_DATABASE_URL=postgresql+psycopg://...)",
    ),
]

INSUFFICIENT_PRIVILEGE = "42501"
FEATURE_ID = next(m["id"] for m in load_demo_fixture()["messages"] if m["subject"].startswith("Feature request"))
PRODUCTION = {"app_env": AppEnv.PRODUCTION, "audit_hmac_key": "a" * 40}


@dataclass
class RuntimeRole:
    name: str
    settings: Settings
    db: Database


async def _owner_execute(owner: Database, *statements: str) -> None:
    async with owner.engine.begin() as conn:
        for statement in statements:
            await conn.execute(text(statement))


@pytest.fixture(autouse=True)
def _signing_key() -> None:
    configure_audit_signing(b"k" * 32)


@pytest.fixture
async def runtime(db: Database, settings: Settings) -> AsyncIterator[RuntimeRole]:
    """A fresh runtime role with the production grants, as after `nuvexa migrate`."""
    await setup_checkpointer(settings)  # LangGraph tables exist before the grants, as in `nuvexa migrate`
    name = f"nuvexa_rt_{uuid.uuid4().hex[:10]}"
    password = uuid.uuid4().hex
    await _owner_execute(db, f"CREATE ROLE {name} LOGIN PASSWORD '{password}'")
    await grant_runtime_privileges(settings.model_copy(update={"database_runtime_role": name}))
    url = make_url(settings.database_url).set(username=name, password=password).render_as_string(hide_password=False)
    runtime_settings = make_settings(url, database_runtime_role=name)
    runtime_db = Database(runtime_settings)
    try:
        yield RuntimeRole(name, runtime_settings, runtime_db)
    finally:
        await runtime_db.dispose()
        await _owner_execute(db, f"DROP OWNED BY {name}", f"DROP ROLE {name}")


async def _refused(engine: AsyncEngine, statement: str) -> str:
    """Run `statement` in a transaction that is always rolled back; return PostgreSQL's error message."""
    async with engine.connect() as conn:
        try:
            with pytest.raises(DBAPIError) as refused:
                await conn.execute(text(statement))
        finally:
            await conn.rollback()
    assert getattr(refused.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE, refused.value
    return str(refused.value.orig)


def _audit_event(tenant_id: uuid.UUID, *, age_days: int = 0) -> AuditEvent:
    event = AuditEvent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        actor_type=ActorType.SYSTEM,
        event_type="probe",
        resource_type="test",
        outcome=AuditOutcome.SUCCESS,
        details={},
        created_at=utcnow() - timedelta(days=age_days),
    )
    event.integrity_hash = compute_audit_integrity(event)
    return event


# --------------------------------------------------------------------------- what the runtime role can do


async def test_runtime_role_reads_and_writes_application_rows(runtime: RuntimeRole) -> None:
    async with runtime.db.session() as s:
        tenant = Tenant(name="Runtime Co", slug=f"rt-{uuid.uuid4().hex[:8]}")
        s.add(tenant)
        await s.flush()
        s.add(_audit_event(tenant.id))
        job = await queue.enqueue(s, job_type=JobType.RETENTION_PURGE, payload={}, tenant_id=tenant.id)
        await s.commit()
        claimed = await queue.claim_next(s, "worker-1")  # SELECT … FOR UPDATE SKIP LOCKED needs UPDATE
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status is JobStatus.RUNNING
        assert await queue.complete(s, claimed, {"ok": True})
        tenant.name = "Renamed Co"
        await s.commit()
        await s.delete(await s.get(Job, job.id))
        await s.commit()
        assert (await s.execute(select(Tenant.name).where(Tenant.id == tenant.id))).scalar_one() == "Renamed Co"


async def test_the_worker_starts_and_checkpoints_as_the_runtime_role(runtime: RuntimeRole) -> None:
    # Regression (R-4): opening the checkpointer ran LangGraph's setup() — CREATE TABLE IF NOT EXISTS — at runtime,
    # which a role without CREATE on the schema cannot do ("permission denied for schema public").
    async with worker_context(runtime.db, runtime.settings) as worker:
        assert worker.settings is runtime.settings
    config: Any = {"configurable": {"thread_id": f"rt-{uuid.uuid4()}", "checkpoint_ns": ""}}
    async with open_checkpointer(runtime.settings) as saver:
        assert await saver.aget_tuple(config) is None
        await saver.adelete_thread(config["configurable"]["thread_id"])


async def test_the_whole_workflow_runs_as_the_runtime_role(runtime: RuntimeRole) -> None:
    """Sync, analysis with PostgreSQL checkpoints, approval, execution and the retention purge — all as DML."""
    async with (
        open_checkpointer(runtime.settings) as saver,
        build_harness(runtime.settings, runtime.db, checkpointer=saver) as h,
    ):
        member = await h.persona("member")
        assert (await member.post("/emails/sync", json={})).status_code == 202
        await h.drain()
        detail = await analyze(h, member, await email_id_by_provider_id(h, member.user_id, FEATURE_ID))
        [work_item] = detail["actions"]
        assert (await approve_and_execute(h, member, work_item))["status"] == "completed"
        async with runtime.db.session() as s:
            s.add(_audit_event(member.tenant_id, age_days=4000))
            await s.commit()
            counts = await purge_tenant(s, member.tenant_id, runtime.settings)
        assert counts["audit_events_deleted"] == 1


async def test_audit_rows_are_deleted_only_through_the_retention_function(runtime: RuntimeRole, tenant: Tenant) -> None:
    # The runtime role cannot delete audit rows itself, not even with the purge flag it could set; the
    # owner-defined purge function deletes only rows older than its 30-day floor.
    recent, old = _audit_event(tenant.id, age_days=1), _audit_event(tenant.id, age_days=40)
    async with runtime.db.session() as s:
        s.add_all([recent, old])
        await s.commit()
    assert "permission denied for table audit_events" in await _refused(
        runtime.db.engine, f"DELETE FROM audit_events WHERE id = '{old.id}'"
    )
    async with runtime.db.engine.connect() as conn:
        await conn.execute(text("SELECT set_config('nuvexa.audit_purge', 'on', true)"))
        with pytest.raises(DBAPIError, match="permission denied for table audit_events"):
            await conn.execute(text(f"DELETE FROM audit_events WHERE id = '{recent.id}'"))
        await conn.rollback()
    async with runtime.db.engine.begin() as conn:
        deleted = await conn.scalar(text("SELECT nuvexa_purge_audit_events(:t, 30)"), {"t": tenant.id})
        ids = set(
            (await conn.execute(text("SELECT id FROM audit_events WHERE tenant_id = :t"), {"t": tenant.id})).scalars()
        )
    assert deleted == 1
    assert recent.id in ids
    assert old.id not in ids


@pytest.mark.parametrize("days", [29, 0, -1])
async def test_the_retention_function_refuses_to_purge_recent_audit_history(
    runtime: RuntimeRole, tenant: Tenant, days: int
) -> None:
    event = _audit_event(tenant.id, age_days=1)
    async with runtime.db.session() as s:
        s.add(event)
        await s.commit()
    async with runtime.db.engine.connect() as conn:
        with pytest.raises(DBAPIError, match="30-day minimum"):
            await conn.execute(text("SELECT nuvexa_purge_audit_events(:t, :d)"), {"t": tenant.id, "d": days})
        await conn.rollback()
    async with runtime.db.session() as s:
        assert await s.get(AuditEvent, event.id) is not None


async def test_the_retention_function_is_hardened_and_not_public(runtime: RuntimeRole) -> None:
    async with runtime.db.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner) AS owner, "
                    "has_function_privilege('public', p.oid, 'EXECUTE') AS public_execute, "
                    "has_function_privilege(:role, p.oid, 'EXECUTE') AS runtime_execute "
                    "FROM pg_proc p WHERE p.proname = 'nuvexa_purge_audit_events'"
                ),
                {"role": runtime.name},
            )
        ).one()
    assert row.prosecdef is True  # runs with the owner's DELETE privilege
    assert any(c.startswith("search_path=") and c.endswith("pg_temp") for c in row.proconfig), row.proconfig
    assert row.owner != runtime.name
    assert (row.public_execute, row.runtime_execute) == (False, True)


# --------------------------------------------------------------------------- what it can never do


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("CREATE TABLE nuvexa_probe (id integer)", "permission denied for schema"),
        ("CREATE TABLE IF NOT EXISTS jobs (id integer)", "permission denied for schema"),
        ("CREATE INDEX nuvexa_probe_ix ON jobs (status)", "must be owner of table jobs"),
        ("CREATE SCHEMA nuvexa_probe", "permission denied for database"),
        ("CREATE VIEW nuvexa_probe AS SELECT 1", "permission denied for schema"),
        ("DROP TABLE jobs", "must be owner of table jobs"),
        ("DROP TABLE checkpoints", "must be owner of table checkpoints"),
        ("ALTER TABLE jobs ADD COLUMN probe integer", "must be owner of table jobs"),
        ("ALTER TABLE audit_events OWNER TO CURRENT_USER", "must be owner of table audit_events"),
        ("TRUNCATE jobs", "permission denied for table jobs"),
        ("TRUNCATE checkpoint_writes", "permission denied for table checkpoint_writes"),
        ("TRUNCATE audit_events", "permission denied for table audit_events"),
        ("UPDATE audit_events SET event_type = 'forged'", "permission denied for table audit_events"),
        ("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only", "must be owner of table audit_events"),
        ("ALTER TABLE audit_events DISABLE TRIGGER ALL", "must be owner of table audit_events"),
        ("DROP TRIGGER audit_events_append_only ON audit_events", "must be owner of relation audit_events"),
        ("SET session_replication_role = replica", "permission denied to set parameter"),
        (
            "CREATE OR REPLACE FUNCTION nuvexa_audit_events_guard() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RETURN OLD; END $$",
            "permission denied for schema",
        ),
        ("DROP FUNCTION nuvexa_audit_events_guard()", "must be owner of function"),
        ("INSERT INTO checkpoint_migrations (v) VALUES (100000)", "permission denied for table checkpoint_migrations"),
        ("DELETE FROM checkpoint_migrations", "permission denied for table checkpoint_migrations"),
        ("ALTER ROLE CURRENT_USER CREATEROLE", "permission denied to alter role"),
    ],
)
async def test_runtime_role_cannot_change_the_schema_or_the_audit_trail(
    runtime: RuntimeRole, statement: str, reason: str
) -> None:
    message = await _refused(runtime.db.engine, statement)
    assert reason in message
    assert "append-only" not in message  # refused by the privilege system, before any trigger runs


async def test_runtime_role_cannot_create_a_table_through_the_checkpointer_setup(runtime: RuntimeRole) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied for schema"):
        await setup_checkpointer(runtime.settings)


# --------------------------------------------------------------------------- the grants step


async def test_tables_created_by_later_migrations_get_dml_grants_automatically(
    db: Database, runtime: RuntimeRole
) -> None:
    await _owner_execute(db, "CREATE TABLE nuvexa_later_migration (id serial PRIMARY KEY, note text)")
    try:
        async with runtime.db.engine.begin() as conn:
            await conn.execute(text("INSERT INTO nuvexa_later_migration (note) VALUES ('a')"))  # sequence USAGE
            await conn.execute(text("UPDATE nuvexa_later_migration SET note = 'b'"))
            assert (await conn.execute(text("SELECT note FROM nuvexa_later_migration"))).scalar_one() == "b"
            await conn.execute(text("DELETE FROM nuvexa_later_migration"))
        await _refused(runtime.db.engine, "TRUNCATE nuvexa_later_migration")
        await _refused(runtime.db.engine, "DROP TABLE nuvexa_later_migration")
    finally:
        await _owner_execute(db, "DROP TABLE nuvexa_later_migration")


async def test_rerunning_the_grants_reverts_drift(db: Database, settings: Settings, runtime: RuntimeRole) -> None:
    name = runtime.name
    await _owner_execute(
        db,
        f"GRANT UPDATE, TRUNCATE ON audit_events TO {name}",
        f"GRANT TRUNCATE, REFERENCES, TRIGGER ON jobs TO {name}",
        f"GRANT CREATE ON SCHEMA public TO {name}",
        f"GRANT INSERT ON checkpoint_migrations TO {name}",
    )
    _, drifted = await inspect_current_role(runtime.db.engine)
    assert {"create_in_schema", "truncate_tables", "update_audit_events"} <= set(drifted)

    await grant_runtime_privileges(settings.model_copy(update={"database_runtime_role": name}))
    await grant_runtime_privileges(settings.model_copy(update={"database_runtime_role": name}))  # idempotent

    assert (await inspect_current_role(runtime.db.engine)) == (name, [])
    await _refused(runtime.db.engine, "INSERT INTO checkpoint_migrations (v) VALUES (100001)")
    await _refused(runtime.db.engine, "TRUNCATE jobs")


async def test_grants_refuse_a_role_that_does_not_exist(settings: Settings, db: Database) -> None:
    missing = f"nuvexa_missing_{uuid.uuid4().hex[:8]}"
    with pytest.raises(ConfigurationError, match=f"{missing}.*does not exist"):
        await grant_runtime_privileges(settings.model_copy(update={"database_runtime_role": missing}))


async def test_grants_refuse_the_migration_role_as_runtime_role(settings: Settings, db: Database) -> None:
    owner = make_url(settings.database_url).username
    assert owner
    with pytest.raises(ConfigurationError, match="must differ from the migration role"):
        await grant_runtime_privileges(settings.model_copy(update={"database_runtime_role": owner}))


# --------------------------------------------------------------------------- startup safety net (real catalog)


async def test_the_runtime_role_passes_the_production_startup_check(runtime: RuntimeRole) -> None:
    assert await inspect_current_role(runtime.db.engine) == (runtime.name, [])
    production = runtime.settings.model_copy(update=PRODUCTION)
    assert await check_runtime_privileges(runtime.db.engine, production, process="api") == []


async def test_the_owner_role_fails_the_production_startup_check(db: Database, settings: Settings) -> None:
    role, findings = await inspect_current_role(db.engine)
    assert role == make_url(settings.database_url).username
    # The test suite connects as the image's bootstrap superuser, which owns everything.
    assert {"superuser", "owns_schema", "owns_tables", "create_in_schema", "truncate_tables"} <= set(findings)
    production = settings.model_copy(update={**PRODUCTION, "database_runtime_role": "nuvexa_app"})
    reported = await check_runtime_privileges(db.engine, production, process="worker")
    assert reported == [*findings, "not_runtime_role"]
