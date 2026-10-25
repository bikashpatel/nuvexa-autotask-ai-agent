# Security audit — NUVEXA-AutoTask-AI-Agent

| | |
|---|---|
| Date | 2026-10-25 |
| Scope | Whole repository at the audit commit: Python API/worker (`app/`), LangGraph workflow, Microsoft Graph / Entra / Azure DevOps adapters, React dashboard (`frontend/`), Dockerfiles, Compose, CI |
| Method | Repository and architecture review, threat-driven manual code review, dependency/secret/SAST/container scanning, and test-driven remediation (every fix has a regression test that failed before the fix) |
| Environments | Local only. Synthetic data, mock Azure DevOps, fake Microsoft identity platform. **No real tenant, mailbox, Azure DevOps organization or LLM endpoint was used.** |

This report states what was verified and what was not. Passing tests and clean scans reduce risk; they
do not prove the application is secure.

## 1. System and trust boundaries

```
Browser ──HTTPS──▶ nginx (frontend container) ──▶ FastAPI API ──▶ PostgreSQL (business data, jobs,
   ▲  session cookie + CSRF header           │                     LangGraph checkpoints, audit)
   │                                          └──▶ worker ──▶ LLM provider (Azure OpenAI / OpenAI)
   │                                                     ├──▶ Microsoft Graph (mail, delegated)
Microsoft Entra ID ◀── OAuth code + PKCE ──── API         └──▶ Azure DevOps REST (work items)
```

| Boundary | Untrusted input | Primary controls |
|---|---|---|
| Browser → API | all request data, cookies, headers | session auth, CSRF token bound to session, RBAC + tenant/owner scoping, Pydantic validation, size/rate limits, sanitized errors |
| Edge proxy → API | `X-Forwarded-*` headers | nginx overwrites `X-Forwarded-For` (F-01); API trusts proxy headers only from the proxy network |
| Microsoft → API (OAuth callback) | code, state, ID token | server-side single-use state + browser binding cookie, PKCE, nonce, **verified ID token** (F-03) |
| Email → LLM | subject/body/sender/attachment names | treated as data: delimited block, injection screening, strict schemas, grounding, policy validation, no tool execution |
| LLM → application | structured output, tool calls | Pydantic schemas, 4 allow-listed proposal tools, `ActionPolicy` (destination/fields/recipients fixed by config), approval required |
| Application → Azure DevOps / Graph | outgoing writes and reads | only `ExecutionService` writes, after re-verifying approval (version + payload hash); idempotency tags; SSRF guards |
| Database | rows written by any compromised component | append-only audit trail with HMAC (F-05), strict checkpoint deserialization (F-07), statement timeouts (F-11) |

## 2. Findings

Severity uses impact × likelihood for this deployment model (internet-facing dashboard, delegated
Microsoft access). Status **Fixed** means a regression test exists and passed after the fix.

| ID | Severity | Finding | Affected components | Status |
|---|---|---|---|---|
| F-01 | High | Client IP spoofable via `X-Forwarded-For`: nginx appended client-supplied values and the API trusted all forwarded headers, so attackers could reset IP rate limits and forge audit IPs | `frontend/nginx.conf`, uvicorn proxy headers | Fixed |
| F-03 | High | ID tokens not fully validated: MSAL only *logs* expired/not-yet-valid tokens, cannot pin the issuer for multi-tenant authorities and does not verify signatures; the app trusted MSAL's decoded claims | `app/integrations/microsoft_graph/oauth.py` | Fixed |
| F-08 | High (scanner) | Backend image shipped pip's vendored urllib3/msgpack/setuptools with known CVEs (not used at runtime) | `Dockerfile` | Fixed |
| F-09 | High (scanner) | Frontend image: `brotli-libs` CVE with an available fix | `frontend/Dockerfile` | Fixed |
| F-04 | Medium | `CORS_ALLOWED_ORIGINS=*` (or malformed origins) accepted; Starlette reflects any origin with credentials when `*` is combined with credentials | `app/core/config.py` | Fixed |
| F-05 | Medium | Audit trail mutable: no database-level protection and no tamper evidence | `app/db/models.py`, `app/services/audit.py`, migration | Fixed |
| F-06 | Medium | Rate limits bypassable: buckets keyed by an *unverified* session-cookie prefix; rotating random cookies reset the login limit | `app/api/deps.py` | Fixed |
| F-07 | Medium | LangGraph checkpoint deserializer permissive by default (arbitrary types; code execution if the checkpoint tables are writable by an attacker) | `app/workflows/checkpointer.py` | Fixed |
| F-14 | Medium | Account re-connection seeded the new connection's token cache with the previous account's refresh tokens | `app/services/auth_service.py` | Fixed |
| F-15 | Medium | Viewer role could call Azure DevOps test/projects/validate endpoints that use the service credentials | `app/api/routes/integrations.py` | Fixed |
| F-16 | Medium | Concurrent first use of a tenant's settings raised `IntegrityError` (failed workflow step / HTTP 500) | `app/services/settings_service.py` | Fixed |
| F-02 | Low | Demo-login audit events had no client IP | `app/services/auth_service.py`, `routes/auth.py` | Fixed |
| F-11 | Low | No PostgreSQL statement/lock timeouts (resource exhaustion) | `app/db/session.py`, `config.py` | Fixed |
| F-12 | Low | Login did not revoke the session the browser already held (session fixation hygiene) | demo login route | Fixed |
| F-13 | Low | Browser `Host` value forwarded as `X-Forwarded-Host` (unused; removed attack surface) | `frontend/nginx.conf` | Fixed |
| F-17 | Low | API listened on all interfaces by default for local runs | `app/cli.py` | Fixed |
| F-18 | Low | Unexpected mailbox-sync exceptions left the checkpoint RUNNING with no audit record | `app/services/email_sync_service.py` | Fixed |
| F-19 | Low | Malformed workflow resume payloads raised `ValueError` and wedged the graph node | `app/workflows/graph.py`, `engine.py` | Fixed |
| F-20 | Low | `IntegrationNotConfigured` recorded as INTERNAL instead of CONFIGURATION | `app/services/execution_service.py` | Fixed |
| F-21 | Low (supply chain) | Dependabot/uv lack a release cooldown | `.github/dependabot.yml`, `pyproject.toml` | Partly fixed (CI/CD gates): Dependabot 7-day cooldown (14 for majors) and npm `min-release-age=7`; uv `exclude-newer` deferred as SEC-EX-004 (expires 2026-11-02, see `security-exceptions.toml`) |
| F-22 | Info (accepted) | semgrep `unverified-jwt-decode`: the ID-token validator decodes without verification to read `tid`. Reviewed: the value only selects the tenant's keys and issuer, and only claims from the verified decode are returned. The Prompt 1 semgrep run predates `id_token.py` and did not cover it | `app/integrations/microsoft_graph/id_token.py` | Accepted as SEC-EX-003; regression test for tokens edited after signing (`test_claims_edited_after_signing_are_rejected_even_for_an_allowed_tenant`) |
| F-23 | Medium (process) | CI ran no dependency, secret, SAST, image or configuration scans, and did not enforce the frontend coverage thresholds or Prettier check (`npm test` only), although `vite.config.ts` said uncovered code "fails CI" | `.github/workflows/ci.yml` | Fixed (docs/CI_SECURITY_GATES.md) |
| F-24 | Low (supply chain) | Base images, the CI PostgreSQL service and the Compose database image referenced mutable tags; npm ran dependency install scripts; `scripts/e2e.sh` installed the E2E group without `--locked` | Dockerfiles, `ci.yml`, `docker-compose.yml`, `frontend/.npmrc`, `scripts/e2e.sh` | Fixed; pinned by `tests/security/test_supply_chain_configuration.py` |
| F-25 | Low (test process) | A test outside the four layer directories would have been selected by none of the per-layer CI steps (none existed) | `tests/conftest.py` | Fixed: collection fails for such a test |
| F-10 | Medium (accepted) | Debian base-image packages with HIGH CVEs and **no fixed version available** (util-linux libs, ncurses, systemd libs, perl-base, libacl) | `python:3.14-slim-trixie` | Accepted with expiry — see §5 |

### Defects found by the test-hardening work (fixed, each with a regression test)

| Area | Defect |
|---|---|
| Auth | `safe_redirect_path` accepted a trailing newline (`$` vs `\Z`) — header-injection precursor |
| Policy | Allow-listed reserved Azure DevOps fields crashed inside the write path and were recorded as *uncertain* |
| Sync | Expired-object access after rollback meant Graph failures were never recorded (no re-auth state, stuck checkpoint) |
| Tenancy | `disconnect_account` returned 409 instead of 404 for foreign ids (enumeration signal) |
| Observability | Library log messages bypassed token redaction; OpenTelemetry exported exception messages/stack traces (possible email content) |
| Errors | Unhandled-exception 500s lacked correlation id and security headers |
| AI | Partial delimiter re-assembly in the From header; forged data-block delimiters were neutralized but not flagged; "not urgent" was scored as urgent; valid deadlines dropped by timezone conversion |
| Workflow | Cancellation during an LLM call failed the run; stale error fields after a successful retry; optimistic `executing` status not reverted |
| API | LIKE search did not escape `\`; dashboard counted deleted emails |
| Frontend | Double-submit of approve/execute; status strings like `constructor` crashed badges; several axe serious/critical accessibility violations |
| ID tokens (introduced and caught during this audit) | The first wiring compared the raw flow nonce, but MSAL sends `sha256(nonce)`; tests modelled MSAL correctly and failed before release |

## 3. Remediation summary

* **F-01** — nginx sets `X-Forwarded-For $remote_addr` (overwrite). Deployment guide documents `set_real_ip_from` behind another trusted proxy.
* **F-03** — `app/integrations/microsoft_graph/id_token.py`: RS256 signature verified against the tenant JWKS (cached, one forced refresh for key rotation, tenant id must be a GUID before it is used in a URL), issuer `{authority}/{tid}/v2.0`, audience, `exp`/`nbf`/`iat` with 120 s leeway against an injectable clock, required claims, tenant allow-list, nonce (MSAL's SHA-256 form). `complete_flow` builds the identity only from verified claims and fails closed without a raw ID token.
* **F-04** — origins must be exact `scheme://host[:port]`; `*`, `null`, paths, credentials and wildcards rejected; production requires `https` origins.
* **F-05** — PostgreSQL triggers block `UPDATE`, `TRUNCATE` and `DELETE` on `audit_events` (deletes allowed only inside the retention purge, which sets `nuvexa.audit_purge` for its transaction); SQLite blocks `UPDATE`. Every event carries `integrity_hash = HMAC-SHA256(AUDIT_HMAC_KEY, canonical event)`; `GET /api/v1/audit-events/integrity` (approver/admin) recomputes all hashes. `AUDIT_HMAC_KEY` is mandatory in production.
* **F-06** — limiter keys: verified user id for authenticated routes, client IP otherwise; sign-in routes always per IP.
* **F-07** — checkpointers use `JsonPlusSerializer(allowed_msgpack_modules=None)` (strict allow-list).
* **F-08/F-09** — runtime images apply OS security updates at build time; pip removed from the backend runtime image.
* **F-11** — `statement_timeout=30s`, `lock_timeout=10s` per PostgreSQL connection (configurable).
* **F-14** — connection flows always start from an empty token cache.
* **F-15** — Azure DevOps endpoints require `action:edit` (member and above).
* **F-16** — settings row created inside a savepoint; a lost race re-reads the committed row.

## 4. Verification evidence

All commands were run from the repository root on macOS (arm64), Python 3.14.6, Node 24.21.

| Check | Command | Result |
|---|---|---|
| Security regression tests | `uv run pytest tests/security -q` | 63 passed, 3 skipped (PostgreSQL-only checks) |
| Full backend suite on PostgreSQL 18 (includes `tests/security`) | `TEST_DATABASE_URL=postgresql+psycopg://nuvexa:nuvexa@localhost:55433/nuvexa_test uv run pytest -q` | 2,991 passed, 1 skipped, 5 xfailed |
| Full backend suite (SQLite) | `uv run pytest -q --cov=app --cov-branch` | 2,980 passed, 12 skipped, 5 xfailed (documented English-only injection limits); **99 %** combined line/branch coverage |
| Lint / format / types | `uv run ruff check . && uv run ruff format --check . && uv run mypy app` | clean (96 source files, strict) |
| Frontend | `npm run lint && npm run typecheck && npm run test:coverage && npm run build` | 1,167 tests; 99.93 % statements, 98.55 % branches, 100 % functions/lines; thresholds enforced. One test failed once while two suites ran concurrently on the same machine; not reproduced in 4 subsequent runs (name not captured) |
| Python dependencies | `uv export --all-groups … \| uvx pip-audit -r - --no-deps` (pip-audit 2.10.1) | 110 packages, **0 known vulnerabilities** |
| npm dependencies | `npm audit` / `npm audit --omit=dev` | 318 packages, **0 vulnerabilities** |
| Secrets (history + tree) | `gitleaks git` / `gitleaks dir` (v8.30.1) | 5 hits, all synthetic test fixtures (hex idempotency keys, the public jwt.io sample token); **no real secrets** |
| SAST | `bandit -r app` (1.9.4) | 2 medium: B104 (fixed, F-17), B608 (false positive: WIQL literals are escaped and validated) |
| SAST | `semgrep scan` (1.181.1) packs python, owasp-top-ten, react, typescript, dockerfile, github-actions, secrets (313 rules, 229 files) | 8 findings: 7 supply-chain cooldown (F-21), 1 nginx host forwarding (fixed, F-13); 0 code vulnerabilities |
| Container images | `trivy image --severity HIGH,CRITICAL` (v0.76.1) | backend: 0 fixable, 44 OS findings with no available fix (F-10); frontend: **0** |
| IaC/config | `trivy config .` | 0 misconfigurations |
| Database triggers | psql against PostgreSQL 18: `UPDATE`/`DELETE`/`TRUNCATE` on `audit_events` | all rejected; delete allowed only with the purge flag |
| System E2E | `scripts/e2e.sh` (isolated Compose stack: nginx, API, worker, PostgreSQL 18; Playwright Chromium) | 37 passed (incl. `X-Forwarded-For` spoofing and rate limiting through the proxy, container restarts, DB invariants) |

### Re-verification with the CI/CD gates (2026-10-25)

The scans were re-run with the pinned versions the pipeline uses (`scripts/security-scan.sh`; cooldown-compliant
scanner releases). Results are in docs/CI_SECURITY_GATES.md §8:

* pip-audit: 136 locked packages, 0 known vulnerabilities.
* npm audit: 0 vulnerabilities; 294 packages pass registry signature verification.
* gitleaks: 0 leaks after four precise allowlists.
* semgrep (1.181.0) and bandit: 0 findings, 2 cited suppressions.
* Trivy 0.76.0: backend 0 fixable (44 HIGH without a fix, SEC-EX-001), frontend 0.
* Trivy config, actionlint and zizmor: clean.

## 5. Accepted risks and exceptions

Scanner exceptions (F-10, F-21 remainder, F-22 and two verified false positives) are maintained in
[`security-exceptions.toml`](../security-exceptions.toml). Every entry has an owner, justification,
compensating controls and an expiry date, and CI enforces citation and expiry
(docs/CI_SECURITY_GATES.md §4). The table below also lists non-scanner risks.

| ID | Risk | Owner | Justification / compensating controls | Expiry |
|---|---|---|---|---|
| F-10 | Debian base-image CVEs without upstream fixes | Platform owner (Elarxsys) | Packages are not used by the application process (no shell-outs); image runs as non-root with read-only code; rebuilt weekly via Dependabot; CI Trivy gate fails on any *fixable* HIGH/CRITICAL | 2026-12-31 — re-evaluate, consider a distroless runtime |
| R-1 | Rate limiting is per process | Platform owner | Configure a shared limit at the ingress/API gateway for multi-replica deployments (documented) | Before multi-replica production |
| R-2 | Prompt-injection screening is English-only (5 strict xfails document non-English/homoglyph gaps) | AI owner | Screening never grants anything; all actions need human approval; destinations/recipients fixed by policy | Before onboarding non-English mailboxes |
| R-3 | Audit HMAC detects modification, not deletion by a database superuser | Platform owner | Append-only triggers; restrict DB superuser access; enable PostgreSQL audit logging/backups | Review at production readiness |
| R-4 | Single database role for migrations and runtime | Platform owner | Documented in deployment guide as a production hardening step (separate DML-only runtime role) | Before production |

## 6. Checks not performed

* No live Microsoft Entra sign-in, Microsoft Graph mailbox, Azure DevOps organization or LLM provider
  was exercised; those paths are verified against faithful fakes (MSAL behaviour, signed ID tokens,
  respx-mocked REST) only.
* No external penetration test, no authenticated DAST scanner (e.g. OWASP ZAP) run, no load testing.
* No review of the Azure resources a production deployment would use (none were created).
* GitHub Actions secret-exposure review covered workflow files only; repository settings (branch
  protection, environment protection rules) are outside this repository and were not inspected or
  changed. docs/CI_SECURITY_GATES.md §6 gives the recommended settings.
