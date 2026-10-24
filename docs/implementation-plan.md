# Implementation plan

Repository state at start (2026-10-24): empty apart from a one-line README and the specification
PDF. No existing code to preserve; the layout follows the spec's suggested structure.

## Verified versions (resolved with `uv lock` / `npm view` on 2026-10-24)

| Area | Choice | Notes |
|---|---|---|
| Python | 3.14 | All dependencies publish 3.14 wheels |
| API | FastAPI 0.162, Starlette 1.10, Uvicorn 0.54 | |
| Models | Pydantic 2.14, pydantic-settings 2.16 | |
| DB | SQLAlchemy 2.1, Alembic 1.20, psycopg 3.3 (async), aiosqlite 0.22 (tests/demo) | One Postgres driver for SQLAlchemy *and* LangGraph |
| Agents | LangGraph 1.2 (+ checkpoint-postgres 3.2, checkpoint-sqlite 3.1), openai 3.37 | `chat.completions.parse` structured output; `pydantic_function_tool` strict tools; `AsyncAzureOpenAI` |
| Microsoft | MSAL 1.39, httpx 0.28 | Graph via REST (2 endpoints needed; avoids the heavy kiota SDK); ADO REST `api-version=7.1` (GA) |
| Observability | structlog 26, OpenTelemetry SDK 1.45 | Langfuse via OTLP |
| Tooling | uv 0.12, Ruff 0.17, mypy 2.4, pytest 9.1, pytest-asyncio 1.4, respx 0.23 | |
| Frontend | React 19, Vite 8, Mantine 9, TanStack Query 5, React Router 8 | TypeScript version chosen to satisfy typescript-eslint peer range |

## Phases

| Phase | Scope | Owner |
|---|---|---|
| 1 | Inspection, architecture, security model, CLAUDE.md | lead |
| 2 | uv project, settings, logging, DB models + migration, auth/session/RBAC foundation, health checks, contracts (schemas, integration protocols) | lead |
| 3 | LLM provider abstraction, prompts, injection screening, heuristic demo agent, evaluation dataset + runner | AI sub-agent |
| 3 | LangGraph workflow, checkpointer, approval interrupt/resume, services (analysis, approval, execution, sync, audit, jobs) and API routes | lead |
| 4 | MSAL auth flow, token store, Graph delta sync + normalization, demo mailbox, Azure DevOps client + mock + validation, retry policy | integrations sub-agent |
| 5 | React dashboard (overview, inbox, review, workflow detail, integrations, settings, audit) | frontend sub-agent |
| 6 | Dockerfiles, Compose, CI, README + setup/deployment/troubleshooting docs | infra sub-agent; lead integrates, runs full test/lint/type suite and fixes |

Sub-agents work in disjoint directories against contracts defined in Phase 2
(`app/schemas/*`, `app/integrations/*/base.py`, `app/agents/base.py`, `docs/api.md`).

## Out of scope for the MVP (designed, documented)

* Graph change-notification webhooks (polling + manual sync implemented).
* Sending email (drafts only).
* Attachment download/parsing.
* Cloud deployment (instructions only; no resources created).

## Status (2026-10-25)

All six phases are implemented. Verification performed:

| Check | Result |
|---|---|
| `ruff check .`, `ruff format --check .` | clean |
| `mypy app` (strict) | clean, 95 source files |
| `pytest` on SQLite | 299 passed (unit, integration, evaluation baseline) |
| `pytest` on PostgreSQL 18 (`TEST_DATABASE_URL`) | 299 passed |
| `alembic upgrade head` + `alembic check` on PostgreSQL 18 and SQLite | applies; no model drift |
| Frontend `lint`, `typecheck`, `test` (58), `build` | pass |
| `docker compose up --build` (alternate host ports) | postgres, migrate, api, worker, frontend healthy |
| Black-box HTTP flow through nginx (login → sync → analyze → approve → execute) | 403 before approval, exactly one mock work item after, 409 on re-execute, LangGraph checkpoints in PostgreSQL |
| Headless Chromium walkthrough of the dashboard (desktop + 390 px mobile) | pages render, approve/execute through the UI completes; only console error is the expected pre-login 401 |

Not verified (no credentials/tenants were used): live Microsoft Entra sign-in, Microsoft Graph mail
sync, Azure DevOps REST calls, OpenAI/Azure OpenAI calls, the GitHub Actions workflow on GitHub, and
the Azure Container Apps deployment steps. These paths are covered by mocked tests only.

Known limitations / next steps:

* The heuristic demo agent is a baseline (47 % category accuracy on the holdout set); run
  `python -m app.evaluation.run --provider azure_openai` against a real deployment and tune prompts.
* Graph change notifications are designed (`docs/design-graph-change-notifications.md`), not built.
* Rate limiting is per process; use a gateway limit when running multiple API replicas.
* First-time concurrent credential saves for the same account can conflict (rare; retried by the user).
