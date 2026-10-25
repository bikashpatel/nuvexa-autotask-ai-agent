#!/bin/sh
# Creates (or updates) the least-privilege runtime role that the compose `api` and `worker` connect as.
#
# Runs idempotently in two places:
#   * by the postgres image on first initialisation of the volume (files in /docker-entrypoint-initdb.d run as
#     POSTGRES_USER against POSTGRES_DB, over the local socket), and
#   * by the one-shot compose service `db-roles` on every `docker compose up`, so volumes created before this
#     role existed and changed passwords converge too.
#
# It only creates the role (LOGIN, no other attributes). Its privileges — DML on the application and LangGraph
# checkpoint tables, nothing else — are granted by `nuvexa migrate` (app/db/privileges.py) as the schema owner,
# exactly as in production (docs/deployment.md, "Database").
#
# Variables: NUVEXA_DB_RUNTIME_USER (default nuvexa_app), NUVEXA_DB_RUNTIME_PASSWORD (default nuvexa_app — a
# local demo value; never reuse it anywhere else).
#
# The body runs in a subshell: the image's entrypoint sources non-executable *.sh files, and options or an exit
# here must not leak into it.
(
  set -eu
  psql -v ON_ERROR_STOP=1 --no-psqlrc --quiet \
    --username "${POSTGRES_USER:-postgres}" --dbname "${POSTGRES_DB:-postgres}" \
    --set=runtime_role="${NUVEXA_DB_RUNTIME_USER:-nuvexa_app}" \
    --set=runtime_password="${NUVEXA_DB_RUNTIME_PASSWORD:-nuvexa_app}" <<'SQL'
SELECT format('CREATE ROLE %I', :'runtime_role')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'runtime_role') \gexec
SELECT format(
  'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT PASSWORD %L',
  :'runtime_role', :'runtime_password'
) \gexec
SQL
  echo "runtime database role ${NUVEXA_DB_RUNTIME_USER:-nuvexa_app} is present (grants: nuvexa migrate)"
)
