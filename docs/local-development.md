# Local development

This guide is for engineers working on the code. For a quick look at the product, use the Docker
Compose demo in the [README](../README.md#quick-start).

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | 0.12+ (CI and the Docker image pin 0.12.1) | Installs and manages Python 3.14 from `.python-version` |
| Node.js | 24 (CI); `frontend/package.json` requires `>=22.22` | npm with `frontend/package-lock.json` |
| Docker | Engine 25+, Compose v2.24+ | Only for PostgreSQL / the full stack |

No Redis, message broker or Kubernetes is needed. PostgreSQL is the only service.

## Ports

| Default port | What | Started by | Change it with |
|---|---|---|---|
| 5173 | Vite dev server (proxies `/api` → `localhost:8000`) | `npm run dev` | `npm run dev -- --port <p>`; proxy target `NUVEXA_API_PROXY_TARGET` |
| 8000 | FastAPI API (`/api/v1`, OpenAPI at `/api/docs`) | `nuvexa api`, `nuvexa demo` | `--port <p>` |
| 8000 | same, published by compose `api` | `docker compose up` | `NUVEXA_API_PORT` |
| 8080 | nginx frontend (proxies `/api/` → `api:8000`) | compose `frontend` | `NUVEXA_WEB_PORT` |
| 5432 | PostgreSQL 18 | compose `postgres` | `NUVEXA_DB_PORT` |

Compose publishes every port on `127.0.0.1` only. Sign-in redirects depend on the URL you use in the
browser: `APP_BASE_URL` and `OAUTH_REDIRECT_URI` default to `:5173` (Vite); Compose derives them from
`NUVEXA_WEB_PORT` (default `:8080`).

## The `nuvexa` CLI

Installed by `uv sync` (entry point `app.cli:main`); run it with `uv run nuvexa ...`.

| Command | What it does |
|---|---|
| `nuvexa api [--host 0.0.0.0] [--port 8000] [--reload]` | Runs the API (uvicorn, app factory in `app/main.py`); `--reload` for development |
| `nuvexa worker` | Runs the job worker (mail sync, analysis, execution, retention) |
| `nuvexa migrate` | `alembic upgrade head`, LangGraph checkpoint tables, then the runtime role's grants when `DATABASE_RUNTIME_ROLE` is set (PostgreSQL); run it as the schema owner |
| `nuvexa demo [--port 8000]` | Single process: SQLite at `./data/nuvexa-demo.db`, migrations, embedded worker, mock integrations, API on 127.0.0.1 |
| `nuvexa gen-key` | Prints a new Fernet key for `TOKEN_ENCRYPTION_KEYS` |

## Option 1 — single process demo (fastest loop, no Docker)

```bash
uv sync
uv run nuvexa demo
# second terminal
cd frontend && npm ci && npm run dev
```

Open <http://localhost:5173> and pick a demo persona (`admin`, `approver`, `member`, `viewer`).
Reset the data by stopping the process and deleting `./data/nuvexa-demo.db` (the `data/` directory
is git-ignored).

SQLite is used only for the demo and the default test run. Behaviour that depends on PostgreSQL
(`FOR UPDATE SKIP LOCKED`, concurrent workers) is exercised by the PostgreSQL test run and CI.

## Option 2 — PostgreSQL in Docker, API and worker on the host

```bash
docker compose up -d postgres
uv sync
uv run nuvexa migrate
uv run nuvexa api        # terminal 1
uv run nuvexa worker     # terminal 2
cd frontend && npm ci && npm run dev   # terminal 3
```

The default `DATABASE_URL` (`postgresql+psycopg://nuvexa:nuvexa@localhost:5432/nuvexa`) matches the
compose database. If port 5432 is already in use, set `NUVEXA_DB_PORT=15432` in `.env` and point
`DATABASE_URL` (and `TEST_DATABASE_URL`) at that port.

That default is the schema owner, which is convenient on the host. To run like Compose and production
(owner for migrations, least-privilege runtime role for the processes — see
[Database roles](security-model.md#database-roles)):

```bash
DATABASE_RUNTIME_ROLE=nuvexa_app uv run nuvexa migrate      # as the owner: DDL + grants
# api (terminal 1) and worker (terminal 2) as the runtime role:
DATABASE_URL=postgresql+psycopg://nuvexa_app:nuvexa_app@localhost:5432/nuvexa uv run nuvexa api
DATABASE_URL=postgresql+psycopg://nuvexa_app:nuvexa_app@localhost:5432/nuvexa uv run nuvexa worker
```

The `nuvexa_app` role is created when the volume is first initialised (`infra/postgres/initdb/`) and by
`docker compose run --rm db-roles` for an older volume.

`nuvexa api` trusts `X-Forwarded-*` headers only from `FORWARDED_ALLOW_IPS` (default `127.0.0.1`,
which covers the Vite proxy). It is read from the process environment, not from `.env`.

For readable logs while developing, set `LOG_FORMAT=console` in `.env`.

## Option 3 — everything in Docker

```bash
docker compose up --build
```

The backend image is built once and used by `migrate`, `api` and `worker`. Rebuild after
dependency or code changes (`docker compose up --build`). `docker compose logs -f api worker`
follows the backend logs.

The stack uses two database roles. `migrate` connects as the schema owner (`POSTGRES_USER`) and is the only
service that runs DDL; `api` and `worker` connect as the runtime role `nuvexa_app`
(`NUVEXA_DB_RUNTIME_USER` / `NUVEXA_DB_RUNTIME_PASSWORD`, demo defaults `nuvexa_app`), which can read and write
rows but not change the schema, truncate tables or update the audit trail. The one-shot `db-roles` service
creates or updates that role on every start (also for volumes created before it existed), and `migrate` then
grants its privileges. `docker compose logs migrate` shows `database_runtime_grants_applied`.

## Configuration

Copy `.env.example` to `.env`; it documents every variable. Settings are validated at startup
(`app/core/config.py`), for example:

* `LLM_PROVIDER=openai` requires `OPENAI_API_KEY` and `LLM_MODEL`.
* `LLM_PROVIDER=azure_openai` requires endpoint, deployment, API version and a key or
  `AZURE_OPENAI_USE_ENTRA_ID=true`.
* `AZURE_DEVOPS_MODE` other than `mock` requires `AZURE_DEVOPS_ORGANIZATION`; `pat` also needs
  `AZURE_DEVOPS_PAT`.
* `APP_ENV=production` requires `TOKEN_ENCRYPTION_KEYS` and forbids `COOKIE_SECURE=false`.

Do not leave variables uncommented with empty values; comment them out instead.

## Quality checks

```bash
uv run ruff check . && uv run ruff format --check .   # lint + format (ruff format . to fix)
uv run mypy app                                       # strict typing
uv run pytest                                         # SQLite, mocks, heuristic agent, no network
cd frontend && npm run lint && npm run typecheck && npm test && npm run build
```

### Tests against PostgreSQL

The compose Postgres creates a `nuvexa_test` database on first initialisation
(`infra/postgres/initdb/`). For an existing volume, create it once:

```bash
docker compose exec postgres createdb -U nuvexa nuvexa_test
TEST_DATABASE_URL=postgresql+psycopg://nuvexa:nuvexa@localhost:5432/nuvexa_test uv run pytest
```

The test fixtures drop and recreate all tables in that database; never point `TEST_DATABASE_URL`
at a database you care about. `tests/security/test_database_least_privilege.py` also creates and drops
throwaway login roles (`nuvexa_rt_…`), so the `TEST_DATABASE_URL` user needs `CREATEROLE` (the image's
bootstrap user, as in Compose and CI, has it).

### Paid model calls

Tests marked `live_llm` call a real provider and are excluded unless selected explicitly
(`uv run pytest -m live_llm` with provider credentials in the environment). They cost money.

### Evaluation

```bash
uv run python -m app.evaluation.run --provider heuristic
```

Runs the labelled evaluation dataset through the analysis agent. Use another provider only with
credentials configured, and expect cost.

## Database migrations

Schema changes need an Alembic migration (config: `alembic.ini`, scripts: `app/db/migrations`).

```bash
uv run alembic revision --autogenerate -m "describe the change"
# review the generated file: keep partial indexes valid for both PostgreSQL and SQLite
uv run alembic upgrade head
uv run alembic current        # what the database is at
uv run alembic history        # available revisions
```

CI applies all migrations to empty PostgreSQL and SQLite databases.

## Useful tips

* OpenAPI UI: <http://localhost:8000/api/docs> (enabled unless `APP_ENV=production`; force with
  `EXPOSE_API_DOCS=true|false`).
* Health: `curl -s localhost:8000/api/v1/health/live` and `.../health/ready` (checks the database).
* Every response carries `X-Request-ID`; search logs by `correlation_id` to follow one request.
* Unsafe requests from tools such as curl need the session cookie and an `X-CSRF-Token` header equal
  to the `nuvexa_csrf` cookie (see [api.md](api.md)).
* Reset the compose database: `docker compose down -v`.
