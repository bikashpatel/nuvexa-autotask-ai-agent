"""Least-privilege database roles: the runtime role's grants and a production startup safety net (risk R-4).

PostgreSQL deployments use two roles (docs/security-model.md, "Database roles"):

* the **owner** (migration) role owns the schema and every table; only `nuvexa migrate` connects as it;
* the **runtime** role (`DATABASE_RUNTIME_ROLE`), which `nuvexa api` and `nuvexa worker` connect as: SELECT, INSERT,
  UPDATE and DELETE on the application and LangGraph checkpoint tables, USAGE/SELECT on sequences, nothing else —
  no CREATE, no ownership, no TRUNCATE, no UPDATE of the append-only audit trail.

`nuvexa migrate` re-applies the grants after every upgrade (`grant_runtime_privileges`). The step is idempotent and
convergent: it first revokes everything the runtime role holds on the schema's tables, so a privilege granted by
hand is removed again on the next release. ALTER DEFAULT PRIVILEGES makes tables created by later migrations usable
immediately. Creating the role itself (LOGIN, password) is a one-time operator step — infra/postgres/initdb/ locally,
docs/deployment.md in Azure — because it needs CREATEROLE, which the owner role should not hold.

A new append-only or migration-owned table must be added to `APPEND_ONLY_TABLES` / `READ_ONLY_TABLES`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.db.session import create_engine
from app.observability.logging import get_logger

log = get_logger(__name__)

RUNTIME_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"
RUNTIME_SEQUENCE_PRIVILEGES = "USAGE, SELECT"
# Rows are never updated (the trigger also refuses it); DELETE stays for the retention purge, which the trigger
# still gates with the per-transaction `nuvexa.audit_purge` flag.
APPEND_ONLY_TABLES = ("audit_events",)
# Bookkeeping written only by migrations (Alembic, LangGraph's setup): readable, never writable at runtime.
READ_ONLY_TABLES = ("alembic_version", "checkpoint_migrations")

# Excess privileges reported by the safety net, in a stable order (log field `findings`).
PRIVILEGE_FINDINGS: tuple[str, ...] = (
    "superuser",
    "createrole",
    "owns_schema",
    "owns_tables",
    "create_in_schema",
    "create_in_database",
    "truncate_tables",
    "update_audit_events",
)
NOT_RUNTIME_ROLE = "not_runtime_role"
REMEDIATION = (
    "Connect api/worker as the least-privilege runtime role and run `nuvexa migrate` as the owner with "
    "DATABASE_RUNTIME_ROLE set (docs/deployment.md, Database)"
)
DEFAULT_CHECK_TIMEOUT_SECONDS = 10.0

# One row of booleans for the role named :role, in the current database and schema. pg_has_role(..., 'MEMBER') also
# counts roles that could SET ROLE to the owner; superusers are members of every role and hold every privilege.
_FINDINGS_SQL = """
SELECT
    r.rolsuper AS superuser,
    r.rolcreaterole AS createrole,
    pg_has_role(r.oid, n.nspowner, 'MEMBER') AS owns_schema,
    EXISTS (
        SELECT 1 FROM pg_class c
        WHERE c.relnamespace = n.oid AND pg_has_role(r.oid, c.relowner, 'MEMBER')
    ) AS owns_tables,
    has_schema_privilege(r.oid, n.oid, 'CREATE') AS create_in_schema,
    has_database_privilege(r.oid, current_database(), 'CREATE') AS create_in_database,
    EXISTS (
        SELECT 1 FROM pg_class c
        WHERE c.relnamespace = n.oid AND c.relkind IN ('r', 'p') AND has_table_privilege(r.oid, c.oid, 'TRUNCATE')
    ) AS truncate_tables,
    has_table_privilege(r.oid, to_regclass(quote_ident(n.nspname) || '.audit_events'), 'UPDATE') AS update_audit_events
FROM pg_roles r, pg_namespace n
WHERE r.rolname = :role AND n.nspname = current_schema()
"""

_GRANT_CONTEXT_SQL = """
SELECT
    current_user AS owner,
    current_schema() AS schema,
    current_database() AS database,
    EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role) AS role_exists,
    has_schema_privilege('public', current_schema(), 'CREATE') AS public_can_create,
    EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = :role AND has_database_privilege(oid, current_database(), 'CONNECT')
    ) AS role_can_connect,
    ARRAY(
        SELECT c.relname::text FROM pg_class c
        WHERE c.relnamespace = to_regnamespace(quote_ident(current_schema())) AND c.relname = ANY(:tables)
    ) AS existing_tables
"""


class RuntimeRoleError(ConfigurationError):
    """`nuvexa migrate` cannot grant the configured runtime role (missing, or the migration role itself)."""


@dataclass(frozen=True)
class GrantContext:
    """Catalog facts the grant statements depend on (read by the migration role)."""

    role: str
    owner: str
    schema: str
    database: str
    existing_tables: frozenset[str]
    public_can_create: bool
    role_can_connect: bool


def quote_ident(name: str) -> str:
    """PostgreSQL identifier quoting (names from the catalog may need it; the role name is validated by Settings)."""
    return '"' + name.replace('"', '""') + '"'


def runtime_grant_statements(ctx: GrantContext) -> list[str]:
    """The convergent, idempotent statements that leave `ctx.role` with DML-only privileges."""
    role, schema, owner = quote_ident(ctx.role), quote_ident(ctx.schema), quote_ident(ctx.owner)
    statements: list[str] = []
    if ctx.public_can_create:  # PostgreSQL < 15 default, or re-granted by hand
        statements.append(f"REVOKE CREATE ON SCHEMA {schema} FROM PUBLIC")
    if not ctx.role_can_connect:  # PUBLIC normally holds CONNECT
        statements.append(f"GRANT CONNECT ON DATABASE {quote_ident(ctx.database)} TO {role}")
    statements += [
        f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role}",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {role}",
        f"REVOKE ALL ON SCHEMA {schema} FROM {role}",
        f"GRANT USAGE ON SCHEMA {schema} TO {role}",
        f"GRANT {RUNTIME_TABLE_PRIVILEGES} ON ALL TABLES IN SCHEMA {schema} TO {role}",
        f"GRANT {RUNTIME_SEQUENCE_PRIVILEGES} ON ALL SEQUENCES IN SCHEMA {schema} TO {role}",
    ]
    statements += [
        f"REVOKE UPDATE ON {schema}.{quote_ident(t)} FROM {role}"
        for t in APPEND_ONLY_TABLES
        if t in ctx.existing_tables
    ]
    statements += [
        f"REVOKE INSERT, UPDATE, DELETE ON {schema}.{quote_ident(t)} FROM {role}"
        for t in READ_ONLY_TABLES
        if t in ctx.existing_tables
    ]
    default = f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema}"
    statements += [
        f"{default} GRANT {RUNTIME_TABLE_PRIVILEGES} ON TABLES TO {role}",
        f"{default} GRANT {RUNTIME_SEQUENCE_PRIVILEGES} ON SEQUENCES TO {role}",
    ]
    return statements


def findings_from_row(row: Mapping[str, Any]) -> list[str]:
    """Names of the excess privileges set in a `_FINDINGS_SQL` row (NULL, e.g. a missing table, is no finding)."""
    return [name for name in PRIVILEGE_FINDINGS if row.get(name) is True]


async def role_findings(conn: AsyncConnection, role: str) -> list[str]:
    row = (await conn.execute(text(_FINDINGS_SQL), {"role": role})).mappings().one()
    return findings_from_row(dict(row))


async def inspect_current_role(engine: AsyncEngine) -> tuple[str, list[str]]:
    """The role this engine connects as, and its excess privileges."""
    async with engine.connect() as conn:
        role = str((await conn.execute(text("SELECT current_user"))).scalar_one())
        return role, await role_findings(conn, role)


async def apply_runtime_grants(conn: AsyncConnection, role: str) -> tuple[GrantContext, list[str]]:
    """Grant `role` DML-only privileges (inside the caller's transaction); returns what remains in excess."""
    tables = [*APPEND_ONLY_TABLES, *READ_ONLY_TABLES]
    facts = (await conn.execute(text(_GRANT_CONTEXT_SQL), {"role": role, "tables": tables})).mappings().one()
    if not facts["role_exists"]:
        raise RuntimeRoleError(
            f"Database role {role!r} (DATABASE_RUNTIME_ROLE) does not exist; create it first "
            "(infra/postgres/initdb/ locally, docs/deployment.md in Azure)."
        )
    if role == facts["owner"]:
        raise RuntimeRoleError(
            "DATABASE_RUNTIME_ROLE must differ from the migration role that runs `nuvexa migrate` "
            f"(both are {role!r}); the runtime role must not own the schema."
        )
    ctx = GrantContext(
        role=role,
        owner=str(facts["owner"]),
        schema=str(facts["schema"]),
        database=str(facts["database"]),
        existing_tables=frozenset(facts["existing_tables"]),
        public_can_create=bool(facts["public_can_create"]),
        role_can_connect=bool(facts["role_can_connect"]),
    )
    for statement in runtime_grant_statements(ctx):
        await conn.execute(text(statement))
    return ctx, await role_findings(conn, role)


async def grant_runtime_privileges(settings: Settings) -> None:
    """`nuvexa migrate` step: (re)apply the runtime role's grants as the schema owner (PostgreSQL only)."""
    if not settings.is_postgres:
        return
    role = settings.database_runtime_role
    if role is None:
        report = log.warning if settings.is_production else log.info
        report(
            "database_runtime_role_not_configured",
            remediation="Set DATABASE_RUNTIME_ROLE so `nuvexa migrate` grants the api/worker role DML-only privileges",
        )
        return
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            ctx, findings = await apply_runtime_grants(conn, role)
    finally:
        await engine.dispose()
    log.info("database_runtime_grants_applied", role=role, owner=ctx.owner, schema=ctx.schema)
    if findings:
        log.warning(
            "database_role_overprivileged", role=role, process="migrate", findings=findings, remediation=REMEDIATION
        )


async def check_runtime_privileges(
    engine: AsyncEngine, settings: Settings, *, process: str, timeout_seconds: float = DEFAULT_CHECK_TIMEOUT_SECONDS
) -> list[str]:
    """Production safety net at api/worker startup: warn (never refuse) when the connection can change the schema.

    Returns the findings (empty when the role is least-privilege, the check does not apply, or it failed).
    """
    if not (settings.is_production and settings.is_postgres):
        return []
    try:
        async with asyncio.timeout(timeout_seconds):
            role, findings = await inspect_current_role(engine)
    except Exception as exc:  # startup must not depend on this check; readiness reports database problems
        log.warning("database_privilege_check_failed", process=process, error=type(exc).__name__)
        return []
    expected = settings.database_runtime_role
    if expected is not None and role != expected:
        findings.append(NOT_RUNTIME_ROLE)
    if findings:
        log.warning(
            "database_role_overprivileged",
            role=role,
            expected_role=expected,
            process=process,
            findings=findings,
            remediation=REMEDIATION,
        )
    else:
        log.info("database_role_least_privilege", role=role, process=process)
    return findings


__all__ = [
    "PRIVILEGE_FINDINGS",
    "GrantContext",
    "RuntimeRoleError",
    "apply_runtime_grants",
    "check_runtime_privileges",
    "findings_from_row",
    "grant_runtime_privileges",
    "inspect_current_role",
    "quote_ident",
    "role_findings",
    "runtime_grant_statements",
]
