"""Least-privilege database roles: runtime grants (`nuvexa migrate`) and the production startup safety net.

The SQL itself runs against a real PostgreSQL in tests/security/test_database_least_privilege.py; these tests pin
the statements, the setting's validation and the control flow (what is skipped, logged or refused) on SQLite.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import AppEnv
from app.core.errors import ConfigurationError
from app.db import privileges
from app.db.privileges import (
    PRIVILEGE_FINDINGS,
    GrantContext,
    RuntimeRoleError,
    apply_runtime_grants,
    check_runtime_privileges,
    findings_from_row,
    grant_runtime_privileges,
    inspect_current_role,
    quote_ident,
    runtime_grant_statements,
)
from app.observability.logging import configure_logging
from tests.conftest import make_settings

PG_URL = "postgresql+psycopg://nuvexa_app:secret@db.invalid:5432/nuvexa"
PRODUCTION = {"app_env": AppEnv.PRODUCTION, "audit_hmac_key": "a" * 40}


@pytest.fixture
def logs() -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """JSON log lines written by the application's own logging configuration (into a buffer, not stdout)."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    configure_logging("INFO", "json")
    buffer = io.StringIO()
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(buffer)

    def read() -> list[dict[str, Any]]:
        return [json.loads(line) for line in buffer.getvalue().splitlines()]

    try:
        yield read
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def _events(lines: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [line for line in lines if line.get("event") == name]


class _ExplodingEngine:
    """Stands in for an engine that must not be used."""

    def connect(self) -> Any:
        raise AssertionError("the database must not be queried")


def _engine() -> AsyncEngine:
    return _ExplodingEngine()  # type: ignore[return-value]


# --------------------------------------------------------------------------- the DATABASE_RUNTIME_ROLE setting


def test_runtime_role_is_unset_by_default() -> None:
    assert make_settings("sqlite+aiosqlite://").database_runtime_role is None


@pytest.mark.parametrize("role", ["nuvexa_app", "app", "_r1", "a" * 63])
def test_runtime_role_accepts_plain_identifiers(role: str) -> None:
    assert make_settings("sqlite+aiosqlite://", database_runtime_role=role).database_runtime_role == role


@pytest.mark.parametrize(
    "role", ["", "Nuvexa", "1app", "app-role", 'app"; DROP TABLE jobs; --', "a b", "a" * 64, "app\n", "pg_x$"]
)
def test_runtime_role_rejects_anything_but_lowercase_identifiers(role: str) -> None:
    with pytest.raises(ValidationError, match="database_runtime_role"):
        make_settings("sqlite+aiosqlite://", database_runtime_role=role)


def test_runtime_role_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "nuvexa_app")
    assert Settings(_env_file=None).database_runtime_role == "nuvexa_app"  # type: ignore[call-arg]


# --------------------------------------------------------------------------- grant statements


def _context(**overrides: Any) -> GrantContext:
    values: dict[str, Any] = {
        "role": "nuvexa_app",
        "owner": "nuvexa",
        "schema": "public",
        "database": "nuvexa",
        "existing_tables": frozenset({"audit_events", "alembic_version", "checkpoint_migrations"}),
        "public_can_create": False,
        "role_can_connect": True,
    }
    values.update(overrides)
    return GrantContext(**values)


def test_quote_ident_doubles_embedded_quotes() -> None:
    assert quote_ident("public") == '"public"'
    assert quote_ident('we"ird') == '"we""ird"'


def test_runtime_grants_are_dml_only_and_converge() -> None:
    assert runtime_grant_statements(_context()) == [
        # Start from nothing, so privileges granted by hand earlier (drift) are removed on every migrate.
        'REVOKE ALL ON ALL TABLES IN SCHEMA "public" FROM "nuvexa_app"',
        'REVOKE ALL ON ALL SEQUENCES IN SCHEMA "public" FROM "nuvexa_app"',
        'REVOKE ALL ON SCHEMA "public" FROM "nuvexa_app"',
        'GRANT USAGE ON SCHEMA "public" TO "nuvexa_app"',
        'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "public" TO "nuvexa_app"',
        'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "public" TO "nuvexa_app"',
        # Append-only audit trail: no UPDATE, no DELETE (and TRUNCATE is never granted); the retention purge runs
        # through the owner-defined function (granted below when it exists).
        'REVOKE UPDATE, DELETE ON "public"."audit_events" FROM "nuvexa_app"',
        # Migration bookkeeping is read-only at runtime.
        'REVOKE INSERT, UPDATE, DELETE ON "public"."alembic_version" FROM "nuvexa_app"',
        'REVOKE INSERT, UPDATE, DELETE ON "public"."checkpoint_migrations" FROM "nuvexa_app"',
        # Tables and sequences created by later migrations get the same DML grants automatically.
        'ALTER DEFAULT PRIVILEGES FOR ROLE "nuvexa" IN SCHEMA "public" '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "nuvexa_app"',
        'ALTER DEFAULT PRIVILEGES FOR ROLE "nuvexa" IN SCHEMA "public" '
        'GRANT USAGE, SELECT ON SEQUENCES TO "nuvexa_app"',
    ]


def test_runtime_grants_never_include_ddl_or_truncate() -> None:
    statements = runtime_grant_statements(_context(public_can_create=True, role_can_connect=False))
    granted = [re.search(r"\bGRANT (.+?) ON ", s) for s in statements if not s.startswith("REVOKE")]
    assert granted
    assert all(granted), statements
    privileges_granted = {p.strip() for match in granted if match for p in match.group(1).split(",")}
    assert privileges_granted == {"SELECT", "INSERT", "UPDATE", "DELETE", "USAGE", "CONNECT"}
    assert not [s for s in statements if "TO PUBLIC" in s or "WITH GRANT OPTION" in s]


def test_the_runtime_role_may_execute_the_audit_purge_function_when_it_exists() -> None:
    grant = 'GRANT EXECUTE ON FUNCTION "public".nuvexa_purge_audit_events(uuid, integer) TO "nuvexa_app"'
    assert grant not in runtime_grant_statements(_context())
    assert grant in runtime_grant_statements(_context(purge_function_exists=True))


def test_create_on_the_schema_is_revoked_from_public_when_it_was_granted() -> None:
    statements = runtime_grant_statements(_context(public_can_create=True))
    assert statements[0] == 'REVOKE CREATE ON SCHEMA "public" FROM PUBLIC'


def test_connect_is_granted_only_when_missing() -> None:
    assert not [s for s in runtime_grant_statements(_context()) if "CONNECT" in s]
    statements = runtime_grant_statements(_context(role_can_connect=False, database='my"db'))
    assert 'GRANT CONNECT ON DATABASE "my""db" TO "nuvexa_app"' in statements


def test_statements_for_missing_bookkeeping_tables_are_skipped() -> None:
    statements = runtime_grant_statements(_context(existing_tables=frozenset()))
    assert not [s for s in statements if "audit_events" in s or "alembic_version" in s or "checkpoint_migrations" in s]
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "public" TO "nuvexa_app"' in statements


def test_identifiers_from_the_catalog_are_quoted() -> None:
    statements = runtime_grant_statements(_context(owner='own"er', schema="My Schema"))
    assert 'GRANT USAGE ON SCHEMA "My Schema" TO "nuvexa_app"' in statements
    assert any(s.startswith('ALTER DEFAULT PRIVILEGES FOR ROLE "own""er" IN SCHEMA "My Schema"') for s in statements)


# --------------------------------------------------------------------------- findings


def test_findings_name_every_excess_privilege_in_a_stable_order() -> None:
    assert PRIVILEGE_FINDINGS == (
        "superuser",
        "createrole",
        "owns_schema",
        "owns_tables",
        "create_in_schema",
        "create_in_database",
        "truncate_tables",
        "update_audit_events",
        "delete_audit_events",
    )
    assert findings_from_row(dict.fromkeys(PRIVILEGE_FINDINGS, True)) == list(PRIVILEGE_FINDINGS)
    assert findings_from_row(dict.fromkeys(PRIVILEGE_FINDINGS, False)) == []
    assert findings_from_row({**dict.fromkeys(PRIVILEGE_FINDINGS, None), "owns_tables": True}) == ["owns_tables"]


# --------------------------------------------------------------------------- grants step (`nuvexa migrate`)


async def test_grants_are_skipped_for_sqlite(logs: Callable[[], list[dict[str, Any]]]) -> None:
    settings = make_settings("sqlite+aiosqlite://", database_runtime_role="nuvexa_app")
    assert await grant_runtime_privileges(settings) is None
    assert _events(logs(), "database_runtime_grants_applied") == []


@pytest.mark.parametrize(("env", "level"), [(AppEnv.TEST, "info"), (AppEnv.PRODUCTION, "warning")])
async def test_grants_without_a_configured_role_are_skipped_and_reported(
    env: AppEnv, level: str, logs: Callable[[], list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(privileges, "create_engine", lambda *_a, **_k: pytest.fail("must not connect"))
    settings = make_settings(PG_URL, **{**PRODUCTION, "app_env": env})
    assert await grant_runtime_privileges(settings) is None
    [event] = _events(logs(), "database_runtime_role_not_configured")
    assert event["level"] == level
    assert "DATABASE_RUNTIME_ROLE" in event["remediation"]


class _Result:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    def mappings(self) -> _Result:
        return self

    def one(self) -> dict[str, Any]:
        return self._row

    def scalar_one(self) -> Any:
        return next(iter(self._row.values()))


class _CatalogConnection:
    """Stands in for the owner's PostgreSQL connection: answers the catalog queries, records every statement.

    The real catalog queries run in tests/security/test_database_least_privilege.py; this pins the control flow.
    """

    def __init__(self, facts: dict[str, Any] | None = None, findings: list[str] | None = None) -> None:
        self.facts = {
            "owner": "nuvexa",
            "schema": "public",
            "database": "nuvexa",
            "role_exists": True,
            "public_can_create": True,
            "role_can_connect": False,
            "existing_tables": ["audit_events", "checkpoint_migrations"],
            **(facts or {}),
        }
        self.findings = dict.fromkeys(PRIVILEGE_FINDINGS, False) | dict.fromkeys(findings or [], True)
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        self.executed.append((sql, params))
        if "AS role_exists" in sql:
            return _Result(self.facts)
        if "AS superuser" in sql:
            return _Result(self.findings)
        if sql == "SELECT current_user":
            return _Result({"current_user": "nuvexa_app"})
        return _Result({})

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.executed if "AS role_exists" not in sql and "AS superuser" not in sql]


class _Engine:
    def __init__(self, conn: _CatalogConnection) -> None:
        self.conn = conn
        self.disposed = False

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[_CatalogConnection]:
        yield self.conn

    connect = begin

    async def dispose(self) -> None:
        self.disposed = True


async def test_apply_runtime_grants_executes_the_statements_for_the_catalog_state() -> None:
    conn = _CatalogConnection(findings=["owns_tables"])

    ctx, findings = await apply_runtime_grants(conn, "nuvexa_app")  # type: ignore[arg-type]

    assert ctx == GrantContext(
        role="nuvexa_app",
        owner="nuvexa",
        schema="public",
        database="nuvexa",
        existing_tables=frozenset({"audit_events", "checkpoint_migrations"}),
        public_can_create=True,
        role_can_connect=False,
    )
    assert conn.statements == runtime_grant_statements(ctx)
    assert conn.executed[0][1] == {"role": "nuvexa_app", "tables": ["audit_events", *privileges.READ_ONLY_TABLES]}
    assert conn.executed[-1][1] == {"role": "nuvexa_app"}  # the findings are checked after the grants
    assert findings == ["owns_tables"]


async def test_apply_runtime_grants_refuses_a_missing_role() -> None:
    conn = _CatalogConnection({"role_exists": False})
    with pytest.raises(RuntimeRoleError, match=r"'nuvexa_app' .*does not exist; create it first"):
        await apply_runtime_grants(conn, "nuvexa_app")  # type: ignore[arg-type]
    assert conn.statements == []


async def test_apply_runtime_grants_refuses_the_migration_role() -> None:
    conn = _CatalogConnection({"owner": "nuvexa_app"})
    with pytest.raises(RuntimeRoleError, match="must differ from the migration role"):
        await apply_runtime_grants(conn, "nuvexa_app")  # type: ignore[arg-type]
    assert conn.statements == []


@pytest.mark.parametrize(("findings", "warned"), [([], False), (["superuser"], True)])
async def test_grants_step_applies_in_one_transaction_and_reports_what_remains(
    findings: list[str], warned: bool, logs: Callable[[], list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(_CatalogConnection(findings=findings))
    monkeypatch.setattr(privileges, "create_engine", lambda settings: engine)
    settings = make_settings(PG_URL, database_runtime_role="nuvexa_app")

    await grant_runtime_privileges(settings)

    assert engine.disposed
    assert engine.conn.statements
    lines = logs()
    [applied] = _events(lines, "database_runtime_grants_applied")
    assert (applied["role"], applied["owner"], applied["schema"]) == ("nuvexa_app", "nuvexa", "public")
    overprivileged = _events(lines, "database_role_overprivileged")
    assert [(e["process"], e["findings"]) for e in overprivileged] == ([("migrate", findings)] if warned else [])


async def test_grants_step_disposes_the_engine_when_it_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine(_CatalogConnection({"role_exists": False}))
    monkeypatch.setattr(privileges, "create_engine", lambda settings: engine)
    with pytest.raises(RuntimeRoleError):
        await grant_runtime_privileges(make_settings(PG_URL, database_runtime_role="nuvexa_app"))
    assert engine.disposed


async def test_inspect_current_role_checks_the_connected_role() -> None:
    conn = _CatalogConnection(findings=["create_in_schema"])
    assert await inspect_current_role(_Engine(conn)) == ("nuvexa_app", ["create_in_schema"])  # type: ignore[arg-type]
    assert conn.executed[-1][1] == {"role": "nuvexa_app"}


# --------------------------------------------------------------------------- startup safety net


async def test_startup_check_is_skipped_outside_production(logs: Callable[[], list[dict[str, Any]]]) -> None:
    for env in (AppEnv.DEVELOPMENT, AppEnv.TEST):
        settings = make_settings(PG_URL, app_env=env)
        assert await check_runtime_privileges(_engine(), settings, process="api") == []
    assert logs() == []


async def test_startup_check_is_skipped_for_sqlite_in_production(logs: Callable[[], list[dict[str, Any]]]) -> None:
    settings = make_settings("sqlite+aiosqlite://", **PRODUCTION)
    assert await check_runtime_privileges(_engine(), settings, process="api") == []
    assert logs() == []


def _inspect_returning(role: str, findings: list[str]) -> Callable[[AsyncEngine], Any]:
    async def inspect(_engine: AsyncEngine) -> tuple[str, list[str]]:
        return role, list(findings)

    return inspect


async def test_production_warns_when_the_runtime_role_can_change_the_schema(
    logs: Callable[[], list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(privileges, "inspect_current_role", _inspect_returning("nuvexa", ["superuser", "owns_tables"]))
    settings = make_settings(PG_URL, **PRODUCTION)

    findings = await check_runtime_privileges(_engine(), settings, process="worker")

    assert findings == ["superuser", "owns_tables"]
    [event] = _events(logs(), "database_role_overprivileged")
    assert event["level"] == "warning"
    assert event["role"] == "nuvexa"
    assert event["process"] == "worker"
    assert event["findings"] == ["superuser", "owns_tables"]
    assert "docs/deployment.md" in event["remediation"]
    assert "secret" not in json.dumps(event)  # the connection URL (with its password) is never logged


async def test_production_warns_when_connected_as_another_role_than_configured(
    logs: Callable[[], list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(privileges, "inspect_current_role", _inspect_returning("nuvexa_owner", []))
    settings = make_settings(PG_URL, **PRODUCTION, database_runtime_role="nuvexa_app")

    assert await check_runtime_privileges(_engine(), settings, process="api") == ["not_runtime_role"]
    [event] = _events(logs(), "database_role_overprivileged")
    assert (event["role"], event["expected_role"]) == ("nuvexa_owner", "nuvexa_app")


async def test_production_logs_a_least_privilege_runtime_role_at_info(
    logs: Callable[[], list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(privileges, "inspect_current_role", _inspect_returning("nuvexa_app", []))
    settings = make_settings(PG_URL, **PRODUCTION, database_runtime_role="nuvexa_app")

    assert await check_runtime_privileges(_engine(), settings, process="api") == []
    lines = logs()
    assert _events(lines, "database_role_overprivileged") == []
    [event] = _events(lines, "database_role_least_privilege")
    assert (event["level"], event["role"], event["process"]) == ("info", "nuvexa_app", "api")


async def test_a_failing_check_is_logged_and_never_blocks_startup(
    logs: Callable[[], list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable(_engine: AsyncEngine) -> tuple[str, list[str]]:
        raise ConnectionError("connection to db.invalid failed: password=secret")

    monkeypatch.setattr(privileges, "inspect_current_role", unreachable)
    settings = make_settings(PG_URL, **PRODUCTION)

    assert await check_runtime_privileges(_engine(), settings, process="api") == []
    [event] = _events(logs(), "database_privilege_check_failed")
    assert (event["level"], event["error"], event["process"]) == ("warning", "ConnectionError", "api")
    assert "secret" not in json.dumps(event)


async def test_a_hanging_check_times_out(
    logs: Callable[[], list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang(_engine: AsyncEngine) -> tuple[str, list[str]]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    monkeypatch.setattr(privileges, "inspect_current_role", hang)
    settings = make_settings(PG_URL, **PRODUCTION)

    assert await check_runtime_privileges(_engine(), settings, process="api", timeout_seconds=0.01) == []
    [event] = _events(logs(), "database_privilege_check_failed")
    assert event["error"] == "TimeoutError"


def test_configuration_error_is_the_failure_type_of_the_grants_step() -> None:
    # `nuvexa migrate` fails with an actionable message (not a stack of SQL errors) for a missing or wrong role.
    assert issubclass(privileges.RuntimeRoleError, ConfigurationError)


def test_the_audit_purge_floor_matches_the_settings_floor() -> None:
    from app.db.models import AUDIT_MIN_RETENTION_DAYS, AUDIT_PURGE_FUNCTION_POSTGRES
    from app.schemas.settings import RetentionConfig

    sql = "\n".join(AUDIT_PURGE_FUNCTION_POSTGRES)
    assert f"p_retention_days < {AUDIT_MIN_RETENTION_DAYS} THEN" in sql
    assert f"below the {AUDIT_MIN_RETENTION_DAYS}-day minimum" in sql
    assert RetentionConfig.model_fields["audit_days"].metadata[0].ge == AUDIT_MIN_RETENTION_DAYS
    assert "SECURITY DEFINER SET search_path = %1$I, pg_temp" in sql
    assert "FROM PUBLIC" in sql
