"""Database role split (risk R-4): `migrate` runs as the schema owner, `api` and `worker` as a DML-only role.

Black-box checks against the running Compose stack: which database user the API's and the worker's live
connections use, what that role may do according to the catalog, and what PostgreSQL refuses when the API
container's own credentials attempt DDL.
"""

from __future__ import annotations

import json
import os

import httpx

from e2e.conftest import API, compose, psql

RUNTIME_ROLE = os.environ.get("NUVEXA_DB_RUNTIME_USER", "nuvexa_app")
OWNER_ROLE = os.environ.get("POSTGRES_USER", "nuvexa")

# Each statement runs in its own transaction, which is always rolled back: nothing changes even if one succeeds.
DDL_PROBES = [
    "CREATE TABLE e2e_probe (id integer)",
    "CREATE SCHEMA e2e_probe",
    "DROP TABLE jobs",
    "ALTER TABLE jobs ADD COLUMN e2e_probe integer",
    "TRUNCATE jobs",
    "TRUNCATE audit_events",
    "UPDATE audit_events SET event_type = 'forged'",
    "ALTER TABLE audit_events DISABLE TRIGGER ALL",
    "DROP TRIGGER audit_events_append_only ON audit_events",
    "SET session_replication_role = replica",
    "DROP TABLE checkpoints",
]

# Runs inside the API container with the API's own configuration (DATABASE_URL from its environment).
PROBE = f"""
import asyncio, json
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.core.config import get_settings
from app.db.session import create_engine

async def main():
    engine = create_engine(get_settings())
    results = {{}}
    async with engine.connect() as conn:
        results["current_user"] = (await conn.execute(text("SELECT current_user"))).scalar_one()
    for statement in {DDL_PROBES!r}:
        async with engine.connect() as conn:
            try:
                await conn.execute(text(statement))
                results[statement] = "allowed"
            except DBAPIError as exc:
                results[statement] = getattr(exc.orig, "sqlstate", None) or type(exc.orig).__name__
            finally:
                await conn.rollback()
    await engine.dispose()
    print(json.dumps(results))

asyncio.run(main())
"""


def _container_ip(service: str) -> str:
    script = "import socket; print(socket.gethostbyname(socket.gethostname()))"
    return compose("exec", "-T", service, "python", "-c", script).stdout.strip()


def test_api_and_worker_connect_as_the_runtime_role() -> None:
    assert httpx.get(f"{API}/health/ready", timeout=10).status_code == 200  # the API holds a pooled connection
    for service in ("api", "worker"):
        ip = _container_ip(service)
        users = psql(
            "select coalesce(string_agg(distinct usename, ','), '') from pg_stat_activity "
            f"where datname = current_database() and host(client_addr) = '{ip}'"
        )
        assert users == RUNTIME_ROLE, f"{service} ({ip}) connects as {users!r}"


def test_the_api_credentials_cannot_change_the_schema_or_the_audit_trail() -> None:
    results = json.loads(compose("exec", "-T", "api", "python", "-c", PROBE).stdout.strip().splitlines()[-1])
    assert results.pop("current_user") == RUNTIME_ROLE
    assert results == dict.fromkeys(DDL_PROBES, "42501")  # insufficient_privilege, every one of them


def test_the_runtime_role_holds_dml_privileges_only() -> None:
    role = RUNTIME_ROLE
    attributes = psql(
        f"select rolsuper, rolcreaterole, rolcreatedb, rolbypassrls from pg_roles where rolname = '{role}'"
    )
    assert attributes == "f|f|f|f"
    schema = psql(
        f"select has_schema_privilege('{role}', 'public', 'USAGE'), has_schema_privilege('{role}', 'public', 'CREATE'),"
        f" has_database_privilege('{role}', current_database(), 'CREATE')"
    )
    assert schema == "t|f|f"
    owned = psql(f"select count(*) from pg_class c where pg_has_role('{role}', c.relowner, 'MEMBER')")
    assert owned == "0"
    audit = psql(
        "select string_agg(p, ',' order by p) from unnest(array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE']) p "
        f"where has_table_privilege('{role}', 'audit_events', p)"
    )
    assert audit == "DELETE,INSERT,SELECT"
    tables = psql(
        "select string_agg(tablename, ',' order by tablename) from pg_tables where schemaname = 'public' and not ("
        f"has_table_privilege('{role}', schemaname || '.' || tablename, 'SELECT')"
        f" and not has_table_privilege('{role}', schemaname || '.' || tablename, 'TRUNCATE'))"
    )
    assert tables == ""  # every table is readable, none can be truncated
    writable = psql(
        "select string_agg(tablename, ',' order by tablename) from pg_tables where schemaname = 'public' and "
        f"tablename like 'checkpoint%' and has_table_privilege('{role}', schemaname || '.' || tablename, 'INSERT')"
    )
    assert writable == "checkpoint_blobs,checkpoint_writes,checkpoints"  # not checkpoint_migrations


def test_migrations_ran_as_the_owner_and_granted_the_runtime_role() -> None:
    owners = psql("select string_agg(distinct tableowner, ',') from pg_tables where schemaname = 'public'")
    assert owners == OWNER_ROLE
    status = compose("ps", "-a", "--format", "json", "migrate").stdout.strip()
    # Compose prints one JSON object per line (older releases: one JSON array).
    migrate = json.loads(status) if status.startswith("[") else [json.loads(line) for line in status.splitlines()]
    assert [m["ExitCode"] for m in migrate] == [0]
    logs = compose("logs", "--no-color", "migrate").stdout
    assert "database_runtime_grants_applied" in logs
    assert "database_role_overprivileged" not in logs
