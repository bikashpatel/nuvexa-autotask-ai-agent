# Observability

Four signals, each with a clear purpose:

| Signal | Where | Use it for |
|---|---|---|
| Structured logs | stdout (JSON) → Docker / Log Analytics | Operations, errors, request correlation |
| Traces | OpenTelemetry → OTLP/HTTP (collector, Application Insights, Langfuse) | Latency, dependency calls, LLM token usage |
| `workflow_events` + `workflow_runs.metrics` | PostgreSQL | Per-run timeline in the UI, product metrics, cost estimates |
| `audit_events` | PostgreSQL | Who approved / rejected / executed what, permission denials, external writes |

Content of emails, prompts and model responses is **not** part of any of these signals.

## Structured logs

`app/observability/logging.py` configures `structlog` for the whole process (including stdlib
loggers). Output is one JSON object per line (`LOG_FORMAT=json`, default) or a coloured console
format (`LOG_FORMAT=console`, convenient locally). `LOG_LEVEL` defaults to `INFO`.

Standard fields:

| Field | Meaning |
|---|---|
| `timestamp` | ISO 8601, UTC |
| `level` | `debug` … `critical` |
| `logger` | Python logger name (module) |
| `event` | Event name, `snake_case` (e.g. `job_succeeded`) |
| `correlation_id` | Request id (`X-Request-ID`, generated if absent) or the job's correlation id; ties the API request to the jobs it queued |
| context fields | e.g. `tenant_id`, `workflow_run_id`, `job_id`, `job_type`, `account_id`, `worker_id` when bound |

Example:

```json
{"event": "job_succeeded", "job_id": "4b0e…", "job_type": "analyze_email", "level": "info",
 "logger": "app.workers.runner", "timestamp": "2026-10-25T09:12:03.512Z", "correlation_id": "9f2c…"}
```

Useful events: `worker_started`, `worker_stopped`, `job_started`, `job_succeeded`, `job_failed`
(with `retryable`, `status`, `error`), `job_unexpected_error`, `job_claim_failed`,
`jobs_recovered_after_lease_expiry`, `mail_delta_expired_full_resync`, `tracing_configured`.

The uvicorn access log is disabled and `httpx`, `httpcore`, `msal`, `openai`, `urllib3` are limited to
`WARNING`, so client libraries do not log request/response bodies or headers.

### Redaction

A processor runs on every log event before rendering:

* Keys matching `token|secret|password|passwd|authorization|cookie|api_key|client_secret|code_verifier|
  code|assertion|credential|body|content|private_key|…pat` (case-insensitive) are replaced with
  `[REDACTED]`, recursively in nested dicts/lists. A short allow-list keeps safe metadata such as
  `content_hash`, `body_length`, `input_tokens`, `output_tokens`, `total_tokens`, `token_usage`.
* Inside any string value, `Bearer …` tokens and JWT-shaped strings are replaced with `[REDACTED]`.
* Strings are truncated to 2,000 characters; nesting deeper than 6 levels is cut.
* The same redaction is applied to `workflow_events.details` before they are stored.

### What is never logged

* Email bodies, subjects-with-body content, attachment content (`LOG_EMAIL_CONTENT=false` is the
  default; keep it that way outside local debugging).
* Prompts and model completions; no chain-of-thought is requested or stored.
* Access/refresh tokens, MSAL caches, PATs, API keys, client secrets, Fernet keys, session and CSRF
  tokens, OAuth codes and PKCE verifiers.
* HTTP request/response headers of outgoing calls.

Redaction is defence in depth: code must not pass such values to the logger in the first place.

## Traces (OpenTelemetry)

Enabled with `OTEL_ENABLED=true`; otherwise no exporter is created. Configuration uses the
standard SDK variables:

| Variable | Example | Notes |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | Base URL; the HTTP exporter appends `/v1/traces` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | `https://…/v1/traces` | Full URL, used as-is (overrides the base) |
| `OTEL_EXPORTER_OTLP_HEADERS` | `Authorization=Basic%20…` | Comma-separated `key=value`; values are URL-decoded |
| `OTEL_SERVICE_NAME` | `nuvexa` | Base name: the API reports `<name>-api`, the worker `<name>-worker` |

The exporter is **OTLP over HTTP/protobuf** (`opentelemetry-exporter-otlp-proto-http`); gRPC
endpoints (port 4317) do not work. Resource attributes: `service.name`, `service.version`,
`deployment.environment.name` (from `APP_ENV`).

Instrumentation:

* FastAPI server spans (health endpoints excluded), SQLAlchemy, and httpx client spans (Graph, Azure
  DevOps, OpenAI). Header capture is off, so tokens are not exported.
* Manual spans, e.g. `nuvexa.mail.sync` (attributes `graph.pages`, `graph.upserts`, `graph.removed`,
  `graph.skipped`, `graph.truncated`) and `gen_ai.chat` for each model call
  (`gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
  `gen_ai.response.finish_reasons`), Azure DevOps outcome attributes (`ado.outcome`,
  `ado.retry_count`), and `error.type` on failures.
* Span attributes carry ids, counts, categories and token numbers — never content.

### Local trace viewer

```bash
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/jaeger:2.22.0
# host processes:            OTEL_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
# compose containers (.env): OTEL_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318
```

Open <http://localhost:16686>. (`host.docker.internal` resolves on Docker Desktop; on Linux add
`extra_hosts: ["host.docker.internal:host-gateway"]` or run the collector in the compose network.)

### Langfuse (via OTLP, no extra SDK)

```bash
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel      # EU
# OTEL_EXPORTER_OTLP_ENDPOINT=https://us.cloud.langfuse.com/api/public/otel # US
# self-hosted (Langfuse v3.22+): https://<your-langfuse-host>/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20<base64 public:secret>,x-langfuse-ingestion-version=4
```

Create the auth string with `printf '%s' 'pk-lf-…:sk-lf-…' | base64` (on GNU systems add `-w 0` for
long keys). Langfuse accepts OTLP over HTTP only. Because NUVEXA does not export prompts or
completions, Langfuse shows timing, model, token usage and errors per run — not inputs/outputs.
Treat the header value as a secret (Key Vault in production).

### Azure Monitor / Application Insights

Run an OpenTelemetry Collector (OTLP/HTTP receiver → `azuremonitor` exporter) and point
`OTEL_EXPORTER_OTLP_ENDPOINT` at it. The Azure Container Apps managed OpenTelemetry agent accepts
gRPC only and is not compatible with this exporter as-is; see [deployment.md](deployment.md#telemetry).

## Workflow events and run metrics (database)

Every workflow step writes a `workflow_events` row (sanitized, no email content):

| Column | Content |
|---|---|
| `step` | e.g. `prepare`, `classify`, `extract`, `validate`, `plan`, `review`, `reanalyze`, `finalize`, and execution steps |
| `event_type` | `step_started`, `step_completed`, `step_failed`, `llm_call`, `tool_call`, `interrupt`, `resume`, `integration_call`, `status_changed` |
| `status`, `message`, `latency_ms` | outcome, short human-readable message, duration |
| `details` (JSON) | e.g. `usage` for LLM calls (`provider`, `model`, `input_tokens`, `output_tokens`, `total_tokens`, `latency_ms`, `estimated_cost_usd`, `attempts`), tool name and sanitized structural arguments, retry counts, failure category |

`workflow_runs` also keeps `model_provider`, `model_name`, `prompt_version`, `schema_version`,
`error_category` and aggregated `metrics` (`llm_calls`, `input_tokens`, `output_tokens`,
`total_tokens`, `estimated_cost_usd`, `llm_latency_ms`). The dashboard shows the timeline
(`GET /api/v1/workflows/{id}/events`) and summary (`GET /api/v1/dashboard/summary`).

Cost is an **estimate** computed from `LLM_INPUT_COST_PER_1M_TOKENS` / `LLM_OUTPUT_COST_PER_1M_TOKENS`;
it is empty when those are unset.

Example queries (PostgreSQL):

```sql
-- LLM latency and tokens per step, last 7 days
SELECT step,
       count(*)                                                    AS calls,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)    AS p95_ms,
       sum((details->'usage'->>'total_tokens')::int)               AS tokens
FROM workflow_events
WHERE event_type = 'llm_call' AND created_at > now() - interval '7 days'
GROUP BY step ORDER BY step;

-- Estimated cost and failures per day
SELECT date_trunc('day', created_at)                                AS day,
       count(*)                                                     AS runs,
       sum((metrics->>'estimated_cost_usd')::numeric)               AS est_cost_usd,
       count(*) FILTER (WHERE error_category IS NOT NULL)           AS failed
FROM workflow_runs GROUP BY 1 ORDER BY 1 DESC;

-- Failure categories
SELECT error_category, count(*) FROM workflow_runs
WHERE error_category IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;
```

Failure categories (`app/domain/enums.py::FailureCategory`): `validation`, `authorization`,
`permission`, `not_found`, `rate_limited`, `transient`, `timeout`, `ambiguous`, `configuration`,
`llm_output`, `llm_provider`, `internal`.

Retention: `workflow_events` are purged after the tenant's `retention.workflow_event_days`
(default 180), audit events after `retention.audit_days` (default 365).

## Suggested alerts

* `job_failed` with `status=dead`, or any `dead` rows in `jobs`.
* `jobs_recovered_after_lease_expiry` (workers crashing or lease too short).
* Rate of `uncertain` actions (need a human Reconcile).
* 5xx rate on the API; readiness failures (`/api/v1/health/ready` → 503).
* LLM `llm_output` / `llm_provider` failures and token/cost spikes.
* Mailbox connections in `needs_reauth`.
