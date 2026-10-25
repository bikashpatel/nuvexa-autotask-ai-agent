# NUVEXA-AutoTask-AI-Agent

NUVEXA-AutoTask-AI-Agent is an AI-powered workflow automation platform developed under **Elarxsys**.
It turns incoming email into **reviewed, approved and auditable work**: it reads authorized Outlook
mail through Microsoft Graph, classifies and extracts structured data with an LLM, proposes concrete
actions (Azure DevOps work items, reply drafts, internal follow-up tasks or "no action"), waits for a
human to approve the exact payload, and only then executes through a deterministic integration adapter.

> **Status:** MVP. The demo stack (synthetic mailbox, heuristic agent, mock Azure DevOps) runs end to
> end without credentials. The Microsoft Graph and Azure DevOps adapters are covered by tests against
> mocked HTTP; they have **not** been verified against live Microsoft services by this repository.

## How it works

```mermaid
flowchart LR
    mail[(Outlook mailbox<br/>or demo mailbox)] -->|Sync: Graph delta query| inbox[Inbox<br/>email_messages]
    inbox -->|Analyze: queued job| norm[Normalize<br/>sanitize + injection screen]
    norm --> classify[Classify<br/>LLM structured output]
    classify --> extract[Extract<br/>LLM structured output]
    extract --> validate[Validate<br/>grounding, schema, confidence]
    validate --> propose[Propose actions<br/>4 strict proposal tools]
    propose --> review{{Human review<br/>edit / approve / reject}}
    review -->|approval bound to<br/>version + payload hash| execute[Execute<br/>deterministic adapter]
    execute --> ado[Azure DevOps work item]
    execute --> draft[Reply draft<br/>never sent]
    execute --> task[Follow-up task]
    execute --> audit[(Audit log +<br/>workflow timeline)]
```

1. **Sync** pulls new messages (Graph delta query, or the synthetic demo mailbox).
2. **Analyze** runs a LangGraph workflow in the worker: normalize and screen for prompt injection,
   classify, extract, validate deterministically, then plan proposals through four strict tools.
3. **Review**: a person sees the category, extracted fields, evidence quotes, confidence and any
   injection flags, can edit the proposed payload, and approves or rejects it.
4. **Execute**: only `ExecutionService` performs writes. It re-checks permissions, the approval
   (`version` + `payload_hash`), payload validity and idempotency before calling the integration.

Screenshots are not included yet; run the demo (below) to see the dashboard.

## Features

- Outlook mail ingestion via Microsoft Graph delta queries (delegated `Mail.Read`), plus a synthetic
  demo mailbox.
- LLM classification (9 categories) and structured extraction with evidence grounding; heuristic
  confidence score with configurable thresholds.
- Action proposals: Azure DevOps work item, reply draft (stored, never sent), internal follow-up task,
  no action.
- Human-in-the-loop approval bound to the exact payload; edits invalidate approvals.
- Azure DevOps work-item creation (PAT or Entra delegated) with tag-based idempotency, bounded retries
  and an explicit **uncertain → Reconcile** path for ambiguous outcomes.
- Prompt-injection defenses: delimited untrusted-data block, deterministic screener, policy-validated
  tool output.
- Multi-tenant data model with roles `viewer`, `member`, `approver`, `admin`; full audit trail.
- Pluggable LLM provider: deterministic heuristic (offline), OpenAI, Azure OpenAI (API key or
  managed identity).
- Durable PostgreSQL job queue (no Redis/Celery), LangGraph checkpoints in PostgreSQL.
- Structured JSON logs with redaction; OpenTelemetry tracing (OTLP, Langfuse compatible).

## Architecture

A modular monolith: one Python 3.14 / FastAPI codebase with two process types (API and worker) and a
React + TypeScript + Mantine dashboard served by nginx. PostgreSQL 18 is the only infrastructure
dependency.

```
browser ──► nginx (frontend, :8080) ──/api──► FastAPI API (:8000) ──► PostgreSQL
                                                     ▲                    ▲
                                        worker (jobs: sync, analyze, execute)
                                          ├─► Microsoft Graph
                                          ├─► Azure DevOps REST 7.1
                                          └─► OpenAI / Azure OpenAI
```

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Components, LangGraph workflow, action lifecycle, data model |
| [docs/security-model.md](docs/security-model.md) | Threats, controls, idempotency, least privilege |
| [docs/api.md](docs/api.md) | REST endpoints, error schema, roles |
| [docs/local-development.md](docs/local-development.md) | Dev setup, tests, migrations |
| [docs/setup-microsoft-entra.md](docs/setup-microsoft-entra.md) | App registration for sign-in and mailbox access |
| [docs/setup-azure-devops.md](docs/setup-azure-devops.md) | PAT / delegated modes, defaults, idempotency |
| [docs/deployment.md](docs/deployment.md) | Azure Container Apps reference architecture |
| [docs/observability.md](docs/observability.md) | Logs, traces, Langfuse, workflow metrics |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common errors and fixes |

## Quick start

All three options run in demo mode by default (`DEMO_MODE=true`, `LLM_PROVIDER=heuristic`,
`MAIL_MODE=demo`, `AZURE_DEVOPS_MODE=mock`): no Microsoft, Azure DevOps or LLM credentials needed.

### A. Docker Compose (recommended)

Requirements: Docker with Compose v2.24 or newer.

```bash
cp .env.example .env          # optional; defaults are demo-safe
docker compose up --build
```

Open <http://localhost:8080>, then:

1. Pick a demo persona (`admin` sees everything; `approver`, `member`, `viewer` show role limits).
2. **Sync mailbox** to load the synthetic emails.
3. Open an email and **Analyze** it; the worker runs the workflow (refresh after a moment).
4. Review classification, extracted fields, evidence, confidence and injection flags. Edit the
   proposed action if needed.
5. **Approve**, then **Execute**. The mock Azure DevOps adapter returns a mock work item; nothing
   leaves your machine. The workflow timeline and audit log record every step.

Compose starts `postgres`, a one-shot `migrate`, `api` (:8000), `worker` and `frontend` (:8080).
All ports are published on `127.0.0.1` only. `docker compose down` stops the stack;
`docker compose down -v` also deletes the database volume.

**Port already allocated?** If another stack already uses 8080, 8000 or 5432 (`Bind for
127.0.0.1:8080 failed: port is already allocated`), pick free host ports in `.env` or the shell:

```bash
NUVEXA_WEB_PORT=18080 NUVEXA_API_PORT=18000 NUVEXA_DB_PORT=15432 docker compose up --build
# then open http://localhost:18080 — APP_BASE_URL and OAUTH_REDIRECT_URI follow NUVEXA_WEB_PORT
```

If you sign in with Microsoft on a non-default port, add the matching redirect URI to the app
registration (see [docs/troubleshooting.md](docs/troubleshooting.md#port-already-allocated)).

### B. Local, without Docker

Requirements: [uv](https://docs.astral.sh/uv/) 0.12+ (it installs Python 3.14), Node.js 24.

```bash
uv sync
uv run nuvexa demo            # SQLite at ./data/nuvexa-demo.db, migrations, embedded worker, API on :8000
```

In a second terminal:

```bash
cd frontend
npm ci
npm run dev                   # Vite on :5173, proxies /api to localhost:8000
```

Open <http://localhost:5173>. Delete `./data/nuvexa-demo.db` to reset the demo data.

### C. Full local development (PostgreSQL in Docker, code on the host)

```bash
docker compose up -d postgres         # published on localhost:5432 (user/password/db: nuvexa)
uv sync
uv run nuvexa migrate                 # alembic upgrade head
uv run nuvexa api                     # terminal 1 — API on :8000
uv run nuvexa worker                  # terminal 2 — job worker
cd frontend && npm ci && npm run dev  # terminal 3 — dashboard on :5173
```

The default `DATABASE_URL` already points at the compose database (with `NUVEXA_DB_PORT` changed,
set `DATABASE_URL` to the same port). If 8000 or 5173 are taken on the host, use
`uv run nuvexa api --port <port>` with `NUVEXA_API_PROXY_TARGET=http://localhost:<port> npm run dev`
(Vite's `/api` proxy target), or `npm run dev -- --port <port>` for the dashboard, and set
`APP_BASE_URL` / `OAUTH_REDIRECT_URI` to the dashboard URL. Details, including running the test suite
against PostgreSQL, are in [docs/local-development.md](docs/local-development.md).

## Configuration

All configuration is environment variables (or `.env`); every variable is listed and explained in
[`.env.example`](.env.example). Tenant-level settings (taxonomy, priorities, confidence thresholds,
Azure DevOps defaults and allow-lists, retention) are edited by admins on the dashboard's Settings page.

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `development` | `production` enforces encryption keys and secure cookies, disables demo login |
| `DEMO_MODE` | `true` | Synthetic mailbox and demo personas |
| `DATABASE_URL` | `postgresql+psycopg://nuvexa:nuvexa@localhost:5432/nuvexa` | SQLAlchemy URL (psycopg 3) |
| `APP_BASE_URL` | `http://localhost:5173` | Public dashboard URL (compose: `http://localhost:8080`) |
| `TOKEN_ENCRYPTION_KEYS` | unset | Comma-separated Fernet keys; first encrypts, all decrypt (`nuvexa gen-key`) |
| `LLM_PROVIDER` | `heuristic` | `heuristic`, `openai` or `azure_openai` |
| `MAIL_MODE` | `demo` | `demo` or `graph` |
| `ENTRA_CLIENT_ID` / `ENTRA_CLIENT_SECRET` | unset | Microsoft Entra app registration |
| `ENTRA_TENANT_ID` | `organizations` | Tenant ID, or `organizations` for multi-tenant |
| `ENTRA_ALLOWED_TENANT_IDS` | empty | Tenants allowed to sign in |
| `OAUTH_REDIRECT_URI` | `http://localhost:5173/api/v1/auth/callback` | Must match the app registration |
| `BOOTSTRAP_ADMIN_EMAILS` | empty | Who may be bootstrapped as the first admin |
| `AZURE_DEVOPS_MODE` | `mock` | `mock`, `pat` or `entra_delegated` |
| `AZURE_DEVOPS_ORGANIZATION` | unset | Required unless `mock` |
| `MAIL_POLL_INTERVAL_SECONDS` | `0` | `0` = manual sync only |
| `OTEL_ENABLED` | `false` | Export traces via `OTEL_EXPORTER_OTLP_*` |

## Connecting real Microsoft 365 and Azure DevOps

1. Register an app in Microsoft Entra ID and grant delegated `User.Read` and `Mail.Read`:
   [docs/setup-microsoft-entra.md](docs/setup-microsoft-entra.md).
2. Set `MAIL_MODE=graph`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `ENTRA_TENANT_ID`,
   `OAUTH_REDIRECT_URI`, `BOOTSTRAP_ADMIN_EMAILS` and a generated `TOKEN_ENCRYPTION_KEYS`.
3. Choose an Azure DevOps mode (`pat` or `entra_delegated`), set `AZURE_DEVOPS_ORGANIZATION` and
   defaults: [docs/setup-azure-devops.md](docs/setup-azure-devops.md).
4. Sign in with Microsoft, connect the mailbox on the Integrations page, use **Test connection** for
   Azure DevOps, then sync.

## Switching the LLM provider

The heuristic provider is deterministic and offline (demo and tests). For real analysis:

```bash
# OpenAI — the model must support strict structured outputs and function calling
LLM_PROVIDER=openai
OPENAI_API_KEY=<your-openai-api-key>
LLM_MODEL=<your-openai-model-name>

# Azure OpenAI with an API key
LLM_PROVIDER=azure_openai
AZURE_OPENAI_ENDPOINT=https://<your-resource-name>.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=<your-deployment-name>
AZURE_OPENAI_API_VERSION=<api-version>     # GA version with structured outputs, e.g. 2024-10-21+
AZURE_OPENAI_API_KEY=<your-azure-openai-key>

# Azure OpenAI with Microsoft Entra ID (managed identity in Azure, `az login` locally) — preferred
LLM_PROVIDER=azure_openai
AZURE_OPENAI_ENDPOINT=https://<your-resource-name>.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=<your-deployment-name>
AZURE_OPENAI_API_VERSION=<api-version>
AZURE_OPENAI_USE_ENTRA_ID=true             # identity needs "Cognitive Services OpenAI User"
```

Optional: `LLM_TEMPERATURE`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `LLM_MAX_EMAIL_CHARS`, and
`LLM_INPUT_COST_PER_1M_TOKENS` / `LLM_OUTPUT_COST_PER_1M_TOKENS` for cost estimates. Settings are
validated at startup; a missing required variable fails fast with a clear message.

## Testing and evaluation

Development is test-first (Red → Green → Refactor); see [docs/TESTING_STRATEGY.md](docs/TESTING_STRATEGY.md).

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run pytest --cov=app                         # all layers on SQLite + 99 % line/branch coverage gate
uv run pytest -m unit | -m integration | -m security | -m evaluation   # one layer at a time
TEST_DATABASE_URL=postgresql+psycopg://nuvexa:nuvexa@localhost:5432/nuvexa_test uv run pytest
scripts/e2e.sh                                  # system E2E: isolated Compose stack + Playwright
uv run --with mutmut mutmut run                 # mutation testing of security-critical modules
uv run python -m app.evaluation.run --provider heuristic   # labelled evaluation dataset
cd frontend && npm ci && npm run lint && npm run typecheck && npm run test:coverage && npm run build
```

Tests that call a paid model are marked `live_llm` and excluded by default. Pull requests that change
production code without changing tests fail the CI `tdd-guard` job.

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) gates every change behind one required check,
`CI gate`. See [docs/CI_SECURITY_GATES.md](docs/CI_SECURITY_GATES.md) for policies, exceptions and local
reproduction. It runs:

* lint and strict types;
* migrations on empty PostgreSQL and SQLite databases;
* the test layers on PostgreSQL 18, plus the full suite on SQLite, with coverage gates;
* the frontend checks, with coverage thresholds;
* pip-audit and npm audit, with SBOMs;
* gitleaks over the full history;
* semgrep and bandit;
* Trivy on both container images and on the configuration;
* actionlint and zizmor;
* the scanner-exception register check;
* the opt-in system E2E suite.

```bash
scripts/security-scan.sh all      # the CI security scans locally (Docker; pinned scanner images)
```

## Security highlights

- **Approval binding:** an approval records `(action_id, version, payload_hash)`; any edit bumps the
  version and invalidates it. Execution recomputes the hash before writing.
- **The LLM is not a security boundary:** model output is Pydantic- and policy-validated. It cannot
  choose URLs, methods, credentials, organization, or unlisted projects/fields, and never approves.
- **Prompt-injection defense:** email content is untrusted data inside a randomly delimited block,
  screened deterministically; flags lower confidence and are shown to reviewers.
- **Tenant isolation:** every query is tenant-scoped (and owner-scoped for non-privileged roles);
  invisible resources return 404.
- **Encrypted tokens:** Microsoft tokens are Fernet-encrypted at rest with rotatable keys, never
  returned to the browser and never logged.
- **Least privilege:** delegated `Mail.Read` only (no send), Azure DevOps PAT scoped to work items;
  reply drafts are never sent. A release gate fails if any new module reaches an external system or
  any new write path appears.
- **Supply chain:** pinned actions and images, hash-locked installs, 7-day release cooldown, no npm
  install scripts, SBOMs, and no silent scanner suppressions (`security-exceptions.toml`).
- **Idempotency:** unique message ids, one active run per email, one succeeded execution per action,
  Azure DevOps tag lookup before create and after ambiguous failures.

Details: [docs/security-model.md](docs/security-model.md).

## Project structure

```
app/
  api/            thin FastAPI routes
  services/       business logic (sync, analysis, approvals, execution, audit, settings)
  workflows/      LangGraph graph, approval interrupt/resume
  agents/         LLM providers, prompts, injection screening, heuristic agent
  integrations/   microsoft_graph/ and azure_devops/ adapters (+ demo/mock)
  repositories/   tenant-scoped queries
  workers/        PostgreSQL job queue and worker runner
  domain/         enums and state machine
  schemas/        Pydantic contracts
  db/             SQLAlchemy models, Alembic migrations (app/db/migrations)
  observability/  logging, redaction, tracing
  core/           settings, security helpers, errors
frontend/         React + TypeScript + Mantine dashboard (Vite; nginx image)
tests/            unit, integration, evaluation
docs/             architecture, security, API, setup, deployment, operations
infra/            reference material (Postgres init script, Azure notes)
Dockerfile        backend image (api / worker / migrate)
docker-compose.yml
```

## Limitations and roadmap

- Graph change-notification webhooks are designed but not enabled; ingestion uses manual sync or
  polling (`MAIL_POLL_INTERVAL_SECONDS`).
- The system never sends email; reply actions create drafts in the app only.
- Attachment content is not downloaded or parsed (metadata only).
- Rate limiting is in-process (per replica); use the ingress or an API gateway for shared limits.
- Azure DevOps has no server-side idempotency key; rare ambiguous cases surface as `uncertain` and
  need a human **Reconcile**.
- Entra app authentication uses a client secret; certificate or federated credentials are not yet
  supported by the configuration.
- Confidence is a heuristic score, not a calibrated probability. Humans approve every action.

## License

Proprietary — © Elarxsys. All rights reserved.
