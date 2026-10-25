# Security model

> Audit findings and remediation: `docs/SECURITY_AUDIT.md`. Control catalogue with test references:
> `docs/SECURITY_CONTROLS.md`.

## Assets

| Asset | Where | Protection |
|---|---|---|
| Microsoft refresh/access tokens | `credential_store.encrypted_token_cache` | Fernet (AES-128-CBC + HMAC-SHA256) with keys from `TOKEN_ENCRYPTION_KEYS` (Key Vault in production); never logged, never returned to the browser |
| Azure DevOps PAT (optional mode) | environment / Key Vault only | Never persisted in the database |
| LLM API key | environment / Key Vault | Prefer managed identity (`AZURE_OPENAI_USE_ENTRA_ID=true`) |
| Session tokens | cookie; SHA-256 hash in `user_sessions` | HttpOnly, Secure, SameSite=Lax, idle + absolute expiry |
| Email content | `email_messages.body_text` | Tenant-scoped; not logged; retention purge |
| Approval integrity | `action_approvals` | Bound to action version + SHA-256 payload hash |
| Audit trail | `audit_events` | Append-only (DB triggers) + per-event HMAC (`AUDIT_HMAC_KEY`) |

## Trust boundaries

```
 untrusted                         │ trusted (deterministic Python)
 ──────────────────────────────────┼──────────────────────────────────────────────
 email body, subject, headers,     │ settings, RBAC, approval records,
 attachment names, links           │ state machine, integration adapters,
 LLM output (incl. tool calls)     │ payload validation, idempotency, retries
 browser requests                  │
```

LLM output crosses the boundary only after Pydantic validation **and** policy validation
(`app/services/action_policy.py`). LLM output can never cause an external write on its own.

## Threats and controls

### Indirect prompt injection (email → LLM)

* System prompt declares email content as untrusted data and lists forbidden behaviours.
* Email text is wrapped in a block delimited by a per-request random boundary
  (`<<<EMAIL_DATA_{nonce}>>>`); any occurrence of the delimiter inside the email is neutralised.
* HTML is converted to text; scripts, styles and comments are dropped; length is capped.
* A deterministic screener (`app/agents/injection.py`) flags phrases such as "ignore previous
  instructions", "you are now", "approve this automatically", "reveal your system prompt",
  role-tag spoofing and encoded payloads. Flags are stored, shown to the reviewer, and reduce
  confidence. They never *grant* anything.
  * It screens the subject, body, sender and recipient names/addresses and attachment names, matching on
    text with Unicode default-ignorable characters removed (variation selectors, joiners, tag characters
    cannot split a phrase).
  * Invisible-character tricks (bidi overrides, tag-character smuggling, runs of zero-width characters)
    are removed at ingestion but leave a visible `[hidden characters removed]` marker in the stored
    subject, body, names and attachment names, which the screener reports as `hidden_text`.
  * Every pattern runs in linear time (regression tests at the 200,000-character screening limit), so a
    crafted email cannot stall a worker.
  * Limitation: English phrasing only (see the strict xfails in `tests/unit/agents/test_injection_corpus.py`).
* Proposed tags starting with `nuvexa-` are reserved for the system's idempotency tag: the model cannot
  propose one, so a work item cannot be made to look like another action's write during reconciliation.
* The planner may only call 4 registered proposal tools with strict schemas. Organization, project
  defaults, area/iteration path allow-lists, field allow-list and API endpoints come from
  configuration, not from the model. Unknown fields or projects are rejected.
* Email replies are **never sent**; the "reply" action stores a draft only. Recipients are forced to
  the original sender.
* Evidence snippets must be verbatim substrings of the email; ungrounded ones are dropped and warned.
* No LLM output is ever executed as code or shell.

### Approval bypass

* The only way to execute is `ExecutionService.execute`, which reloads the action, checks RBAC,
  requires a valid approval row (not invalidated, same version, same recomputed payload hash) and
  performs a conditional status transition.
* Approve requests must carry the `version` and `payload_hash` the reviewer saw (prevents approving
  a payload changed by someone else in the meantime).
* Editing an approved action bumps the version and invalidates the approval.
* LangGraph resume payloads are treated as *requests*, not authorizations.
* There is no endpoint that executes arbitrary tools or payloads.

### Cross-tenant / cross-user access

* Every query goes through repository helpers that require a `tenant_id`; non-privileged users are
  additionally scoped to `owner_user_id`.
* IDs are random UUIDv4; missing and forbidden resources both return 404 (reads, and ids named in
  request bodies such as `SyncRequest.connected_account_id`) to avoid enumeration; explicit denials on
  writes are audited (`permission_denied`).
* Viewers are read-only: they cannot store delegated tokens (connecting a mailbox needs `email:sync`,
  connecting Azure DevOps needs `action:execute`).

### OAuth / session

* Auth-code flow with PKCE, `state` and `nonce` (MSAL). Flow stored server-side, single use,
  10-minute expiry, bound to a random browser-binding cookie (`nuvexa_oauth_bind`) → login CSRF
  protection.
* Redirect URI comes from configuration and is validated against `OAUTH_REDIRECT_URI`; post-login
  redirects are restricted to relative paths by an allow-list on the server and, in the browser, an
  allow-list plus a same-origin check (`safeRedirectPath`).
* **Identity is the verified (`tid`, `oid`) pair**, never the `email`/`preferred_username` claim, which
  is mutable and unverified ("nOAuth"). An e-mail match only binds a pre-provisioned account that no
  Entra identity owns yet, in the same tenant; any other identity is refused and audited
  (`login`, `outcome=denied`). Deactivated users are refused the same way (Entra and demo sign-in).
* Tenant restriction: `ENTRA_ALLOWED_TENANT_IDS`. `BOOTSTRAP_ADMIN_EMAILS` grants admin only when an
  account is created or first bound, never on later sign-ins (a demoted admin stays demoted). Residual
  risk: until the named administrator has signed in once, another principal in an allowed tenant whose
  directory e-mail equals a listed address could claim it — sign the administrator in right after
  deployment and then clear the list.
* Session cookie `HttpOnly`, `Secure` (except local dev), `SameSite=Lax`; CSRF double-submit on
  unsafe methods; session rotation on login; logout revokes server-side.

### Transport and input limits

* Request body limit (`MAX_REQUEST_BYTES`, default 1 MiB): `413` for declared and streamed bodies;
  pagination offsets are bounded; NUL characters in bodies, query strings and paths are rejected
  (`422`) because PostgreSQL cannot store them. Graph fields are cleaned and clipped to their columns on
  ingestion; a message the database still rejects is skipped and recorded, never blocking the mailbox. A
  sync round is bounded by messages, pages, time (`MAIL_SYNC_ROUND_SECONDS`) and body bytes
  (`MAIL_SYNC_MAX_BODY_BYTES`) and resumes from the pending page.
* Rate limiting on auth, sync, analyze, approve and execute routes (`429 rate_limited` with
  `Retry-After` in whole seconds). Buckets are keyed by the verified user, or by client IP on sign-in
  routes — never by an unverified cookie. `RATE_LIMIT_BACKEND` selects the store:
  * `memory` (default): exact sliding window **per API process** — with N replicas every limit is N×.
    A production API started with it logs `rate_limits_are_per_process` (the replica count cannot be
    known from inside the process, and a single replica is valid, so it warns rather than refusing).
  * `database` — **required for more than one replica** (or a shared limit at the ingress / gateway):
    counters in `rate_limit_counters`, shared by every replica on the same database
    (`app/services/rate_limit.py`). Sliding-window counter over two fixed windows: admitted while
    `current + previous × (1 − elapsed/window) < limit`. Within a window the limit is exact under any
    concurrency (one conditional `INSERT … ON CONFLICT DO UPDATE … WHERE hits < cap RETURNING`);
    the weighting smooths window boundaries (worst case ≈ 2× in one sliding window when all earlier
    hits fell at the end of the previous window; a plain fixed window allows 2× within an instant);
    requests crossing a boundary while previous-window requests are in flight can each admit at most
    one extra request; boundaries follow each replica's wall clock (keep NTP). Denied requests are not
    counted. Each check uses its own short transaction and connection, never the request's session.
  * Stored keys are HMAC-SHA256 digests (key derived from `AUDIT_HMAC_KEY`, which replicas share) of
    limit name, `user:<id>`/`ip:<address>` and window length: no raw IP or user id is stored, and the
    IPv4 space cannot be enumerated without the key. Rows expire two windows after their window starts
    and are deleted by the limiter at most once a minute per replica while it serves requests.
  * If the counter store fails (database error), sign-in routes **fail closed** (`429`,
    `Retry-After: 5`; anonymous brute-force target, and they need the database anyway) and
    authenticated routes **fail open** (session just verified; RBAC, approval binding and idempotency
    still apply); both are logged as `rate_limit_backend_unavailable` without key or IP.
* Security headers (CSP on the frontend, `X-Content-Type-Options`, `Referrer-Policy`,
  `X-Frame-Options: DENY`, `Strict-Transport-Security` for a year: the Container Apps ingress adds none).
* CORS disabled by default (same-origin through the reverse proxy); explicit allow-list otherwise.

### Logging and telemetry

* A structlog processor redacts keys matching `token|secret|password|authorization|cookie|body|
  api_key|client_secret|code_verifier`, and truncates long strings.
* Upstream error text is not trusted: Azure DevOps errors are reduced to stable identifiers (`TFnnnnnn`,
  exception type, field reference) before they are stored, returned or logged, because providers can
  echo credentials back.
* Tracebacks are rendered before redaction, redacted and capped at 16 KiB. Value redaction also covers
  `Basic …` credentials, `sk-…` API keys and Azure DevOps PAT shapes. HTTP-client request logging
  (`httpx`, `httpx2`, `httpcore2`) is at WARNING, so request URLs are not logged.
* An unexpected exception in an analysis step is re-raised with a fixed message ("Analysis step '<step>'
  failed (<Type>)…"), so its text never reaches LangGraph checkpoint writes.
* Email bodies are not logged (`LOG_EMAIL_CONTENT=false` default). Spans carry IDs, sizes,
  categories and token counts — not content.
* Every exported span (ours and the FastAPI/httpx/SQLAlchemy auto-instrumentations') passes through
  `SanitizingSpanExporter`: URL attributes keep scheme, host and path only (query strings carry OAuth codes,
  search terms and Graph delta tokens; userinfo is removed), captured headers and bodies are dropped, and
  exception events keep the exception type only (no message, no stack trace).
* No chain-of-thought is requested or stored. Only concise rationales and evidence references.

### Data retention

* `retention.email_body_days` purges bodies (metadata stays) — default 90.
* `retention.audit_days` — default 365.
* Disconnecting an account deletes its credential row and stops sync; optional purge of its email.

## Idempotency

| Layer | Mechanism |
|---|---|
| Ingestion | `UNIQUE(connected_account_id, provider_message_id)` + upsert; Graph `Prefer: IdType="ImmutableId"` |
| Analysis | Job `dedupe_key = analyze:{email_id}:{content_hash}`; at most one active run per email |
| Proposals | `idempotency_key = sha256(tenant, email, action_type, slot)`; partial unique index |
| Execution | Conditional transition; partial unique index on one succeeded execution per action |
| Azure DevOps | Tag `nuvexa-<first 16 hex of idempotency key>` + WIQL lookup before create and after ambiguous failures |

**Azure DevOps limitations:** ADO offers no server-side idempotency key. Duplicate detection relies
on the tag; if a user deletes the tag or the work item between a timeout and the reconcile lookup,
or WIQL indexing lags, a duplicate is theoretically possible. Such cases surface as `uncertain` and
require a human to reconcile; the system never blindly retries an ambiguous write.

Interrupted or unrecorded writes converge instead of sticking:

* An Azure DevOps create is bounded to 80 % of `JOB_LEASE_SECONDS` (default 240 s of 300 s); a create
  that runs out of time is recorded `uncertain`, so an execution older than its lease always belongs to a
  dead worker. Such an `executing` action can be reconciled (tag lookup, never a second create).
* Job-queue `complete`/`fail`/`heartbeat` are fenced on the lease holder and attempt, so a stalled worker
  whose lease was taken over cannot release or re-queue another worker's job. Job rows with a type this
  version does not know stay queued for a newer worker instead of blocking the queue.
* A workflow cannot be cancelled while a write is in flight or uncertain, and execution re-checks that the
  parent run is not cancelled or rejected.

## Least-privilege permissions

| Integration | Permission | Why |
|---|---|---|
| Graph (sign-in) | `openid profile email offline_access User.Read` | Identify the user |
| Graph (mailbox) | `Mail.Read` (delegated) | Delta query + body text. `Mail.ReadBasic` excludes the body, so it is insufficient |
| Azure DevOps (`entra_delegated` mode) | `499b84ac-1321-427f-aa17-267ca6975798/user_impersonation` | Work items created as the approving user |
| Azure DevOps (`pat` mode) | PAT scope `vso.work_write` (Work Items: Read & write); optionally `vso.project` (Project and Team: Read) for project listing / *Test connection* | Service identity; use an organization-scoped PAT |

### Database roles

PostgreSQL deployments use two roles (risk R-4), so a compromised API or worker process cannot alter or
drop the schema, disable the audit triggers or rewrite the audit trail:

| Role | Connects | Privileges |
|---|---|---|
| Owner (migration) role — `POSTGRES_USER` locally, `nuvexa_owner` in Azure | `nuvexa migrate` only | Owns the schema objects; runs Alembic, LangGraph's checkpoint `setup()` and the grants step |
| Runtime role `nuvexa_app` (`DATABASE_RUNTIME_ROLE`) | `nuvexa api`, `nuvexa worker` | `CONNECT`, `USAGE` on the schema; `SELECT`, `INSERT`, `UPDATE`, `DELETE` on the application and LangGraph checkpoint tables; `USAGE`, `SELECT` on sequences. `audit_events`: `SELECT`, `INSERT`, `DELETE` only. `alembic_version`, `checkpoint_migrations`: `SELECT` only |

* **Grants are code, applied on every migration.** `nuvexa migrate` (`app/db/privileges.py`) revokes everything
  the runtime role holds on the schema's tables and sequences and grants exactly the set above, plus
  `ALTER DEFAULT PRIVILEGES FOR ROLE <owner>` so tables from later migrations are covered. Manual drift is
  undone by the next run. The role itself (LOGIN, password) is created once by an operator
  (`infra/postgres/initdb/` locally, [deployment guide](deployment.md#database) in Azure), because that needs
  `CREATEROLE`, which the owner role should not have.
* **The runtime never runs DDL.** The LangGraph checkpointer that `api`/`worker` open does not run `setup()`
  (whose `CREATE TABLE IF NOT EXISTS` already needs `CREATE` on the schema); only `nuvexa migrate` does.
* **What PostgreSQL refuses to the runtime role** (`tests/security/test_database_least_privilege.py`, and against
  the Compose stack in `e2e/test_database_roles_system.py`): `CREATE`/`ALTER`/`DROP` of tables, indexes, views,
  schemas and functions (including replacing the audit trigger function), `TRUNCATE` of any table, `UPDATE` of
  `audit_events`, disabling or dropping triggers, `SET session_replication_role`, taking ownership, changing
  role attributes and writing the migration bookkeeping tables — each with `insufficient_privilege` from the
  privilege system, before any trigger runs.
* **Defence in depth for the audit trail.** The runtime role has neither `UPDATE` nor `DELETE` on `audit_events`.
  The retention purge calls `nuvexa_purge_audit_events(tenant, days)`, a `SECURITY DEFINER` function owned by the
  owner role (pinned `search_path`, not executable by PUBLIC) that deletes only that tenant's rows older than the
  retention period and refuses anything younger than 30 days — a compromised API or worker cannot erase recent
  audit history. The append-only trigger still refuses every other delete.
* **Production safety net.** With `APP_ENV=production`, `api` and `worker` check their own role at startup and
  log a `database_role_overprivileged` WARNING with `findings` (`superuser`, `createrole`, `owns_schema`,
  `owns_tables`, `create_in_schema`, `create_in_database`, `truncate_tables`, `update_audit_events`,
  `delete_audit_events`, `not_runtime_role`). They do not refuse to start; alert on the event.

## Residual risks / roadmap

* In-process rate limiting (see above).
* A fully compromised API/worker can still shorten audit retention for its tenants to the 30-day floor (the
  retention period is a tenant setting it can write), so audit rows older than 30 days are not protected against it;
  rows younger than that cannot be deleted at runtime. A superuser can delete anything (R-3).
* Webhook (Graph change notifications) ingestion is designed but not enabled; polling/manual sync is used.
* Attachment content is never downloaded in the MVP (metadata only).
* LLM classification can still be wrong; humans approve every action.
