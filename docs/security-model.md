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
  ingestion; a message the database still rejects is skipped and recorded, never blocking the mailbox.
* In-process rate limiting on auth, sync, analyze, approve and execute routes. **Limitation:** limits
  are per replica; for multi-replica deployments configure a shared limit at the ingress / API
  gateway.
* Security headers (CSP on the frontend, `X-Content-Type-Options`, `Referrer-Policy`,
  `X-Frame-Options: DENY`).
* CORS disabled by default (same-origin through the reverse proxy); explicit allow-list otherwise.

### Logging and telemetry

* A structlog processor redacts keys matching `token|secret|password|authorization|cookie|body|
  api_key|client_secret|code_verifier`, and truncates long strings.
* Upstream error text is not trusted: Azure DevOps errors are reduced to stable identifiers (`TFnnnnnn`,
  exception type, field reference) before they are stored, returned or logged, because providers can
  echo credentials back.
* Email bodies are not logged (`LOG_EMAIL_CONTENT=false` default). Spans carry IDs, sizes,
  categories and token counts — not content.
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

## Residual risks / roadmap

* In-process rate limiting (see above).
* Webhook (Graph change notifications) ingestion is designed but not enabled; polling/manual sync is used.
* Attachment content is never downloaded in the MVP (metadata only).
* LLM classification can still be wrong; humans approve every action.
