# Troubleshooting

Start with the logs: `docker compose logs -f api worker` (or the terminal running `nuvexa api` /
`nuvexa worker`). Every API response has an `X-Request-ID`; error bodies carry the same value as
`error.correlation_id`, and every log line of that request has `correlation_id`. Set
`LOG_FORMAT=console` locally for readable output.

## Local stack

### Port already allocated

`Bind for 127.0.0.1:8080 failed: port is already allocated` (or 8000 / 5432) means another process
or compose project uses that host port. Choose other host ports in `.env` or the shell:

```bash
NUVEXA_WEB_PORT=18080 NUVEXA_API_PORT=18000 NUVEXA_DB_PORT=15432 docker compose up --build
```

* The dashboard is then at `http://localhost:18080`; `APP_BASE_URL` and `OAUTH_REDIRECT_URI` follow
  `NUVEXA_WEB_PORT` automatically (unless you set them explicitly in `.env`).
* Host processes using the compose database (`uv run nuvexa api`, `TEST_DATABASE_URL` test runs)
  must use the new port in `DATABASE_URL` / `TEST_DATABASE_URL`.
* Microsoft sign-in: Entra ignores the port when matching `http://localhost` redirect URIs, so the
  registered localhost callback still matches.
* Without Docker: `uv run nuvexa api --port <p>` (or `nuvexa demo --port <p>`), and run Vite with
  `NUVEXA_API_PROXY_TARGET=http://localhost:<p> npm run dev`. Find the owner with
  `lsof -nP -iTCP:<port> -sTCP:LISTEN`.

### Containers do not start / frontend returns 502

* `docker compose ps`: `migrate` must have exited with code 0; `api` must be `healthy`. The frontend
  starts only after the API's readiness check passes.
* `docker compose logs migrate` shows migration errors (see [migrations](#migrations-out-of-date)).
* `nginx` 502 on `/api/...` means the `api` container is down or restarting: check its logs for a
  settings validation error (below).

### Settings validation error at startup

The process exits with a pydantic validation message, for example
`LLM_PROVIDER=openai requires OPENAI_API_KEY and LLM_MODEL`,
`AZURE_DEVOPS_ORGANIZATION is required unless AZURE_DEVOPS_MODE=mock`,
`TOKEN_ENCRYPTION_KEYS is required in production`. Set the named variables (see `.env.example`).
A variable left uncommented with an empty value (`LLM_TEMPERATURE=`) is parsed as a value and can
fail validation; comment it out instead.

### PostgreSQL connection errors

| Symptom | Fix |
|---|---|
| `connection refused` on `localhost:5432` | `docker compose up -d postgres`; check `NUVEXA_DB_PORT` matches the port in `DATABASE_URL` |
| `password authentication failed` | `POSTGRES_USER/PASSWORD/DB` apply only when the volume is first created. Use the original credentials or reset with `docker compose down -v` (deletes data) |
| `database "nuvexa_test" does not exist` | `docker compose exec postgres createdb -U nuvexa nuvexa_test` |
| `NoSuchModuleError` / driver errors | The URL must start with `postgresql+psycopg://` (psycopg 3), or `sqlite+aiosqlite:///` |
| Azure: SSL / timeout errors | Append `?sslmode=require`; check private DNS / VNet integration and firewall rules |
| `too many connections` | Lower `DATABASE_POOL_SIZE` or replica counts; each process may open up to twice the pool size |

`GET /api/v1/health/ready` returns 503 while the database is unreachable.

### Migrations out of date

Symptoms: `relation "..." does not exist`, `column ... does not exist`, readiness failures after
pulling new code.

```bash
uv run nuvexa migrate             # or: docker compose run --rm migrate
uv run alembic current            # revision the database is at
uv run alembic heads              # more than one head → merge revisions (alembic merge)
```

`nuvexa demo` applies migrations to its SQLite file on start; if the demo database is from an
incompatible older revision, delete `./data/nuvexa-demo.db`. `nuvexa demo` refuses to run when
`DATABASE_URL` points at a non-SQLite database in the shell environment.

### Demo login missing

Demo personas appear only with `DEMO_MODE=true` **and** `APP_ENV` other than `production`
(`GET /api/v1/auth/config` → `demo_login_enabled`).

## Browser session

### `403 csrf_failed`

Unsafe requests (`POST/PUT/PATCH/DELETE`) must send `X-CSRF-Token` equal to the readable
`nuvexa_csrf` cookie. After clearing cookies, switching between `:5173` and `:8080`, or when a tab
outlived the session, reload the page and sign in again. For scripted calls, send the session cookie
and the CSRF header.

### Session cookie not kept / immediate `401`

* Use the same origin as `APP_BASE_URL` (do not mix `localhost` and `127.0.0.1`).
* Secure cookies are used whenever `APP_ENV` is not `development`. Browsers accept them on
  `http://localhost`, but not on other plain-HTTP hosts; use HTTPS there, or `COOKIE_SECURE=false`
  outside production.

## Microsoft Entra / Graph

### AADSTS50011: redirect URI mismatch

"The redirect URI specified in the request does not match the redirect URIs configured for the
application." NUVEXA sends exactly `OAUTH_REDIRECT_URI`.

* Add that exact value under the app registration → **Authentication** → **Web** platform (not SPA).
* Check scheme, host and path (case-sensitive); `http://` is allowed only for `localhost`.
* Compose uses `http://localhost:${NUVEXA_WEB_PORT}/api/v1/auth/callback`; Vite uses
  `http://localhost:5173/api/v1/auth/callback`.
* Behind a proxy in production, `OAUTH_REDIRECT_URI` must be the public `https://` URL.

### AADSTS65001: consent required / "Need admin approval"

The user (or tenant) has not consented to the requested scopes, often because user consent is
restricted. An administrator grants consent on the app registration (**API permissions** →
**Grant admin consent for &lt;tenant&gt;**) or on the enterprise application. For multi-tenant apps,
consent is needed in each tenant. See [setup-microsoft-entra.md](setup-microsoft-entra.md#5-consent).

### Other sign-in errors

| Error | Usual cause |
|---|---|
| `AADSTS7000215` invalid client secret | The secret **ID** was copied instead of the **Value**, or the secret was deleted |
| `AADSTS7000222` expired client secret | Create a new secret, update `ENTRA_CLIENT_SECRET`, restart |
| `AADSTS700016` application not found | Wrong `ENTRA_CLIENT_ID`, or wrong tenant for a single-tenant app |
| `AADSTS50020` user account from another tenant | Single-tenant registration; use a multi-tenant registration and `ENTRA_ALLOWED_TENANT_IDS` |
| Signed in but rejected by NUVEXA | The user's tenant is not in `ENTRA_ALLOWED_TENANT_IDS` |
| OAuth state / binding error on callback | The sign-in took longer than 10 minutes, was replayed, or started in another browser/origin; start again |

### Mailbox shows "needs reauth"

"Microsoft 365 authorization expired or was revoked. Reconnect the mailbox." The refresh token is no
longer valid (password reset, revoked sessions, consent removed, Conditional Access). Reconnect the
mailbox on the Integrations page.

### Delta token expired → full resync

Graph delta links expire (HTTP `410`, `syncStateNotFound`, `resyncRequired`). NUVEXA detects this,
logs `mail_delta_expired_full_resync`, and starts a new round bounded by `MAIL_SYNC_INITIAL_DAYS` /
`MAIL_SYNC_MAX_MESSAGES`. Messages already ingested are not duplicated (unique immutable message ids).
No action needed unless it happens on every sync.

## Azure DevOps

### HTTP 203 / "Azure DevOps returned a sign-in page"

The PAT or token was not accepted: expired, revoked, scoped to another organization, a global PAT
after retirement, or malformed. Replace the PAT (organization-scoped, **Work Items: Read & write**).
See [setup-azure-devops.md](setup-azure-devops.md#invalid-pat-symptom-http-203-with-an-html-page).

### Test connection fails but the PAT is valid

**Test connection** lists projects, which Microsoft documents under **Project and Team: Read**
(`vso.project`). Add that read scope, or enter the project manually.

### `401` / `403` / `404` on create

Authentication (`401`), missing permission on the project or area path (`403`), or a project/type
that does not exist (`404`). These are never retried. Use `POST /api/v1/integrations/azure-devops/validate`
with `server_side: true` to check a payload, and the tenant allow-lists on the Settings page.

### Action is `uncertain` → Reconcile

The outcome of a create is unknown (timeout after the request was sent, and the tag lookup also
failed). NUVEXA never blindly retries. Open the action and choose **Reconcile**: it looks the
`nuvexa-…` tag up again; found → `completed` (linked to the existing item), confirmed absent →
`failed` (safe to retry with the still-valid approval). If Azure DevOps is still unreachable,
reconcile later.

## LLM provider

| Symptom | Cause / fix |
|---|---|
| `agent_output_error` (502), failure category `llm_output` | Refusal, truncated or schema-invalid structured output. Not retried automatically. Use a model/deployment that supports strict structured outputs and function calling; on Azure use a recent GA `AZURE_OPENAI_API_VERSION`; very long emails are truncated at `LLM_MAX_EMAIL_CHARS`. Content filters can refuse some emails — handle those manually or re-analyze |
| Category `authorization` on an LLM step | The provider rejected the credentials (HTTP 401/403): check `OPENAI_API_KEY` / `AZURE_OPENAI_API_KEY` or the managed identity's role on the Azure OpenAI resource. Not retried |
| Category `configuration` on an LLM step | Another 4xx (wrong deployment/model name, API version, endpoint): fix the settings. Not retried |
| `agent_provider_error`, category `llm_provider` | Network, `429` or `5xx` from the provider; retried at job level (`LLM_MAX_RETRIES`, `JOB_MAX_ATTEMPTS`). Check quota, endpoint, `LLM_TIMEOUT_SECONDS` |
| `401` from Azure OpenAI with `AZURE_OPENAI_USE_ENTRA_ID=true` | Identity lacks **Cognitive Services OpenAI User**; with a user-assigned identity set `AZURE_CLIENT_ID`; locally run `az login` |
| `temperature` rejected | Leave `LLM_TEMPERATURE` unset (it is then omitted) |

## Rate limits (`429`)

* `429 rate_limited` from NUVEXA's own API: the in-process limiter on auth, sync, analyze, approve and
  execute routes. Wait and retry; it is per replica. `RATE_LIMIT_ENABLED=false` only for local load
  testing.
* `429` from Graph, Azure DevOps or the LLM provider is retried with backoff (honouring
  `Retry-After`). Persistent throttling: lower `WORKER_CONCURRENCY`, `MAIL_SYNC_MAX_MESSAGES` or the
  poll frequency.

## Jobs

### Analysis or sync never finishes

Sync, analysis and execution are queued jobs processed by the worker.

* Is a worker running? Compose: `docker compose ps worker`. On the host, `nuvexa api` alone does not
  process jobs; run `nuvexa worker` (or use `nuvexa demo`, which embeds one).
* Inspect the queue:

  ```sql
  SELECT id, job_type, status, attempts, max_attempts, locked_by, locked_at, run_after, last_error
  FROM jobs WHERE status IN ('queued', 'running', 'failed', 'dead') ORDER BY created_at DESC LIMIT 20;
  ```

  (`docker compose exec postgres psql -U nuvexa -d nuvexa`.)
* `run_after` in the future: a retry is scheduled with backoff.
* `dead`: attempts exhausted (`JOB_MAX_ATTEMPTS`); fix the cause in `last_error`, then re-trigger from
  the UI (Analyze / Re-analyze / Execute).

### Stuck `running` jobs and lease expiry

A worker that crashed or was killed leaves its jobs `running`. After `JOB_LEASE_SECONDS` (default
300) any worker re-queues them (`last_error = 'lease expired; re-queued'`, log
`jobs_recovered_after_lease_expiry`) or marks them `dead` when attempts are exhausted. Running jobs
refresh their lease every `JOB_LEASE_SECONDS / 3` (heartbeat), so long LLM calls are not re-queued
while their worker is alive. On shutdown the worker waits up to 30 s for in-flight jobs; anything
interrupted is recovered by lease expiry, and an interrupted external write is reconciled, never
blindly re-sent.

## Token encryption keys

| Message | Meaning / fix |
|---|---|
| `TOKEN_ENCRYPTION_KEYS is not configured` | A real mailbox or delegated Azure DevOps connection needs keys: `uv run nuvexa gen-key` |
| `TOKEN_ENCRYPTION_KEYS contains an invalid Fernet key` | Each comma-separated entry must be a 44-character url-safe base64 Fernet key; no quotes or spaces |
| `Stored secret cannot be decrypted with the configured keys` | The key that encrypted the stored token (`InvalidToken`) is no longer in the list. Put the old key back (after the new first key) or have users reconnect |

Rotation: prepend the new key (`<new>,<old>`), restart API and worker, and remove the old key only
when nothing depends on it. See
[setup-microsoft-entra.md](setup-microsoft-entra.md#generating-and-rotating-token_encryption_keys).
