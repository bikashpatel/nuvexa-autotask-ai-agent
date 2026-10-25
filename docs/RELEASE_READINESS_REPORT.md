# Release readiness report — NUVEXA-AutoTask-AI-Agent

| | |
|---|---|
| Date | 2026-10-25 |
| Code reviewed | `main` at `2a66558` (merged from branch `prompt5`; local verification on `a73afd7`/`a5bd61d`, later commits are docs, test assertions and scanner configuration only), previous release `bf19680` |
| Scope | Backend (FastAPI, LangGraph workflow, worker, PostgreSQL/SQLite), React dashboard, nginx image, CI pipeline |
| Method | Independent adversarial testing in five areas (AI and data exposure, approval and execution integrity, access control, input and recovery, frontend), fixes test-first, then full regression, scans and system E2E |
| Recommendation | **READY WITH DOCUMENTED RISKS** — for a supervised pilot with sandbox credentials. **Not** for production mailboxes until the conditions in §10 are met |

This report does not claim the software is free of defects. It states what was verified, what was
found and fixed, what remains uncertain, and what must be monitored. Every result quoted here comes from
a run whose output was observed; anything not run is listed as not run.

## 1. Architecture and trust boundaries

```
Browser ──HTTPS──▶ nginx (frontend container) ──▶ FastAPI API ──▶ PostgreSQL (business data, jobs,
   ▲  session cookie + CSRF header           │                     LangGraph checkpoints, audit)
   │                                          └──▶ worker ──▶ LLM provider (Azure OpenAI / OpenAI)
   │                                                     ├──▶ Microsoft Graph (mail, delegated, read-only)
Microsoft Entra ID ◀── OAuth code + PKCE ──── API         └──▶ Azure DevOps REST (work items)
```

The LLM proposes; deterministic code disposes. The only external write is an Azure DevOps work-item
create, performed by `ExecutionService` after a human approved the exact payload
(`action_id, version, payload_hash`). Mail is read with `Mail.Read` only; replies are stored as drafts
and never sent.

| Boundary | Untrusted input | Primary controls (verified in this review) |
|---|---|---|
| Browser → API | all request data, cookies, headers | session auth, CSRF bound to the session on every unsafe method, RBAC + tenant/owner scoping (404 for invisible), Pydantic validation, NUL rejection, body/offset limits, rate limits, sanitized error envelopes with correlation ids |
| Edge proxy → API | `X-Forwarded-*` | nginx overwrites `X-Forwarded-For`; CSP and security headers on the SPA |
| Microsoft → API (OAuth) | code, state, ID token | single-use server-side state + browser binding, PKCE, verified ID token (signature, iss, aud, lifetime, tenant, nonce); identity = (`tid`, `oid`), never the e-mail claim |
| Email → LLM | subject, body, names, attachment names | delimited data block, linear-time injection screening of every field (default-ignorable characters removed for matching, hidden-character markers kept), strict schemas, grounding, no tool execution |
| LLM → application | structured output, tool calls | per-call validation, 4 allow-listed tools, `ActionPolicy` (destination, fields, recipients fixed by configuration; `nuvexa-` tags reserved), human approval |
| Application → Azure DevOps / Graph | outgoing requests | one reviewed write path (source-level gate), idempotency tags, bounded retries, write budget below the job lease, upstream error text reduced to codes |
| Database | rows written by any compromised component | append-only HMAC-signed audit, strict checkpoint deserialization, statement/lock timeouts (engine and checkpointer), lease-fenced job queue |

Details: [architecture.md](architecture.md), [security-model.md](security-model.md),
[SECURITY_AUDIT.md](SECURITY_AUDIT.md), [CI_SECURITY_GATES.md](CI_SECURITY_GATES.md).

## 2. Tests executed

All commands from the repository root (macOS arm64, Python 3.14.6, Node 24, Docker), on the final code.

| Suite | Command | Result |
|---|---|---|
| Lint, format, types | `uv run ruff check . && uv run ruff format --check . && uv run mypy app` | clean (strict mypy, 96 source files) |
| Backend, SQLite, coverage gate | `uv run pytest --cov=app` | **4,006 passed**, 12 skipped (PostgreSQL-only), 5 strict xfail; coverage **99.69 %** (gate 99 %) |
| Backend, PostgreSQL 18 | `TEST_DATABASE_URL=postgresql+psycopg://…@127.0.0.1:55433/nuvexa_test uv run pytest --cov=app` (throwaway container) | 4,015 passed, 2 skipped, 5 xfail, **1 failed** — a test-helper limitation (checkpoint dump assumed the SQLite saver), fixed in `005aab9`; that file then passed on PostgreSQL (66 passed). Coverage 99.68 %. Final clean run: §2.1 |
| Frontend | `cd frontend && npm ci && npm run format:check && npm run lint && npm run typecheck && npm run test:coverage && npm run build` | **1,329 passed** (68 files); build OK |
| Adversarial suites (subset of the above) | `uv run pytest tests/security/test_adversarial_*.py`; `npx vitest run src/test/adversarial-*.test.tsx src/test/prototype-keys.test.tsx` | 281 backend tests in 5 files, frontend adversarial XSS/UI-state/prototype-key suites — all pass after the fixes (49 failed before them) |
| AI evaluation baseline | `uv run pytest -m evaluation` | 44 passed (unchanged by the screening changes) |
| Security scans | `scripts/security-scan.sh all` (pinned scanner images) | §5 — all stages pass after one fixture allowlist |
| System E2E | `scripts/e2e.sh` (isolated Compose stack: nginx, API, worker, PostgreSQL 18; Playwright Chromium) | §2.2 |
| Mutation testing | `uv run --with mutmut mutmut run --max-children 8` | §2.3 |

Backend test layers: unit 3,223 · integration 310 · security 446 · evaluation 44 (collection fails for a test
outside these directories, so the CI layer steps cover the whole suite).

### 2.1 Final PostgreSQL run

On commit `a5bd61d` (all fixes): **4,016 passed**, 2 skipped (SQLite-only), 5 strict xfail; coverage
**99.68 %**. The PostgreSQL-specific fixes — per-message savepoints when PostgreSQL rejects NUL or over-long
values, row locks on workflow cancel, lease-fenced queue updates, checkpointer timeouts — ran against real
PostgreSQL 18 here.

### 2.2 System E2E

`scripts/e2e.sh` on commit `a5bd61d`, images built from that code: **37 passed** (41.6 s) — API black-box
through nginx (sessions, CSRF, RBAC, `X-Forwarded-For` spoofing, rate limits), full email → analysis →
approval → execution flow with the mock Azure DevOps adapter, worker and API restarts (queued jobs and an
interrupted workflow resume from the PostgreSQL checkpoint), database invariants after the suite (at most one
succeeded execution per action, one active run per email, no execution without an approval, no plaintext
tokens) and Playwright Chromium UI flows.

### 2.3 Mutation testing

`mutmut run --max-children 8` on the security-critical modules (`state_machine`, `authz`, `approval_service`,
`execution_service`, `action_policy`, `id_token`). A first run on macOS reported 164 "segfault" mutants (the
forked test process crashed); the authoritative run is the one in a Linux container (the backend's
`python:3.14-slim-trixie` base image, commit `933273e`, 52 minutes):

| Result | Mutants |
|---|---|
| Killed | 2,338 → **2,342** after four follow-up assertions (`4ad56ea`, `b015c48`) |
| Survived | 309 → **305** |
| Inconclusive (process crashed) | 19 |
| No test | 1 |
| **Score** | **88.4 %** of conclusive mutants (2,342 / 2,648); 87.8 % counting the 19 crashes as undetected (previous release review: 87.8 %) |

The macOS "93.3 %" figure published earlier in this report was too optimistic: most mutants that crashed on
macOS survive on Linux. `state_machine` and `authz` have no survivors. Triage of the survivors (sampled diffs):

* `id_token` (80): error-message wording only (`IdTokenError(None)`, changed case) — accepted; tests assert error
  types and categories, not text.
* `execution_service` (136, mostly `_dispatch` and `execute`), `approval_service` (42), `action_policy` (51):
  log event names and fields, span attribute names, latency arithmetic, error-message wording, clipping bounds
  off by one (`[:500]` → `[:501]`), equivalent defaults, and database-lock mutants (`for_update` flipped; only
  observable on PostgreSQL and covered by `tests/integration/test_postgres_concurrency.py`). None of the sampled
  survivors changes what is authorized or written.
* Real gaps found and closed with assertions: reconciliation building the Azure DevOps client without the
  acting user, the crashed attempt losing its `ambiguous` category (`8e010c5`), the audit targets of
  rejection denials, superseded actions and status repairs (`4ad56ea`), and the tenant of the
  `payload_integrity_failure` audit event.

Uncertainty: 19 mutants crash the test process even on Linux and are not counted as detected.

### 2.4 CI (GitHub Actions)

| Run | Commit | Result |
|---|---|---|
| [40166551446](https://github.com/bikashpatel/nuvexa-autotask-ai-agent/actions/runs/40166551446) | `ee6503f` | **Blocked by the secret scan** — this report quoted a secret-shaped synthetic fixture (§5); cancelled when the fix was pushed |
| [40167197528](https://github.com/bikashpatel/nuvexa-autotask-ai-agent/actions/runs/40167197528) | `2a66558` | **All gates green**, `CI gate` passed. PostgreSQL layers: unit 3,217 (1 skipped, 5 strict xfail), integration 310, security 445 (2 skipped), evaluation 44; coverage gate passed (8,141 statements / 13 missed, 1,730 branches / 17 partial); SQLite suite, frontend, dependency audit, secret scan, SAST, container and configuration scans all passed |

## 3. Coverage

| Area | Line + branch | Gate |
|---|---|---|
| Backend (SQLite run) | 99.69 % — 8,141 statements, 14 missed; 1,730 branches, 17 partial | `fail_under = 99` (line + branch) |
| Backend (PostgreSQL run) | 99.68 % | same gate in CI |
| Frontend | statements 99.93 %, branches 98.62 %, functions 100 %, lines 100 % | 99 / 98 / 100 / 100 |

Coverage shows code ran, not that it is correct; §4 and §2.3 address correctness.

## 4. Critical business logic and security invariants

Each invariant below is covered by behaviour-level tests (API → services → workflow → worker → DB), many
of them adversarial. "Prompt 5" marks tests added or strengthened in this review.

| Invariant | Evidence |
|---|---|
| No external write without a current approval of the exact payload | approval binding and tamper tests (`test_security.py`, `test_end_to_end.py`); Prompt 5: forged/stale version-hash pairs, DB-tampered payloads, queued v1 job never sending edited v2, every closed state × 6 execution paths (24 cases), cancelled workflows (defect P5-02 fixed) |
| One external write path | `tests/security/test_external_write_paths.py` (AST gate: reviewed egress modules, only reviewed non-GET calls, one caller of the create, Graph `Mail.Read` only) |
| The LLM is never a security boundary | Prompt 5: a fully compromised model (forbidden projects/fields/URLs/tools, self-approval, prompt leak of all configured secrets) changes nothing and leaks nothing; hostile email through the heuristic agent |
| Email content is untrusted data | delimiter integrity under 16 hidden-injection vectors; XSS on every dashboard surface (no raw-HTML sinks, 21 URL tricks neutralized); bidi isolation |
| Tenant isolation, 404 for invisible | route walker over all 45 routes (every protected route needs a session and the right permission); foreign ids indistinguishable from unknown ids on 19 routes; list-scope widening attempts |
| Idempotency / never double-create | exhaustive idempotency state machine (20,000+ sequences); Prompt 5: replays, duplicate deliveries, redelivery during an in-flight write, two workers on one job, ambiguous timeouts → `uncertain` → reconcile, unrecorded outcomes converge via tag lookup |
| Audit append-only and signed | PostgreSQL triggers + HMAC integrity tests; refused sign-ins and blocked executions audited |
| Verified identity only | ID-token validation suite incl. tokens edited after signing; Prompt 5: e-mail-claim account takeover refused (P5-01) |
| Secrets never exposed | Prompt 5: sweeps of every DB table, API response, log line, OTel span and checkpoint table (SQLite and PostgreSQL) for LLM keys, PAT (raw and Basic), tokens, `AUDIT_HMAC_KEY`, encryption keys |
| Reliability (bounded retries, backoff + jitter, timeouts, recovery) | retry bounds and `Retry-After` capping; LLM/Graph/ADO/DB/checkpointer timeouts; lease recovery, fencing, poison-message isolation, crash after proposals / after the write |
| Rejected, cancelled and failed work never proceeds | state machine (every transition pair), closed-state matrix, cancel refusal while writes are in flight, execution refusal for cancelled/rejected runs |

## 5. Static analysis, dependency, secret and container scans

`scripts/security-scan.sh all` (the same pinned scanner images and flags as CI) on the final code, with
both images rebuilt from it:

| Scan | Result |
|---|---|
| pip-audit 2.10.1, every locked Python package (all groups, hashes required) | no known vulnerabilities |
| npm audit (fails on high/critical; all listed) | 0 vulnerabilities |
| npm registry signatures | 294 packages verified, 123 with attestations |
| gitleaks v8.30.1, full history | **1 finding on the first local run**: a synthetic redaction fixture added in this review (an `api-key` test value made of the hex digits in order, in `tests/unit/observability/test_logging.py`). It is not a credential; it is allowlisted by rule + file + exact line under SEC-EX-005, and a key planted elsewhere in the same file is still reported (negative control). **CI then flagged this report itself** (commit `ee6503f` quoted the value): reworded, and that one historical commit is allowlisted by commit + file + exact line (SEC-EX-006). Final scan, every commit on every ref with merge diffs (P5-37): **no leaks** |
| semgrep 1.181.0 (Python, TypeScript/React, OWASP Top 10, Dockerfile, GitHub Actions, secrets; 315 rules, 290 files) | 0 findings (the two cited `inSource` suppressions stay visible in SARIF) |
| bandit 1.9.4 (all severities) | no issues |
| Trivy 0.76.0, backend image | **0 fixable** HIGH/CRITICAL; without a published fix: 44 HIGH, 58 MEDIUM, 61 LOW, 1 UNKNOWN (SEC-EX-001); no secrets |
| Trivy 0.76.0, frontend image | 0 findings |
| Trivy config, actionlint 1.7.12, zizmor 1.31.1 | clean |
| Exception register | 10 cited suppressions, 6 registered exceptions, none expired (SEC-EX-004 expiry warning: 2026-11-02) |
| SBOMs | CycloneDX for the Python runtime and all groups, npm runtime and both images |

Static checks: ruff (lint and format) and strict mypy are clean; ESLint (`--max-warnings=0`), Prettier and
`tsc` are clean.

## 6. Defects found by adversarial testing (all fixed, each with a regression test)

Severity reflects impact in a production deployment. "Test" names the regression test that failed before
the fix (files under `tests/security/` unless noted).

| ID | Sev. | Defect | Fix | Test |
|---|---|---|---|---|
| P5-01 | High | Account takeover: a second Entra identity with the same `email` claim signed in as an existing user (incl. admins) and rebound the account ("nOAuth") | identity = (`tid`, `oid`); e-mail only binds an unbound pre-provisioned account; refusals audited; bootstrap admin only at creation/first binding | `test_adversarial_access_control.py::test_another_entra_identity_cannot_take_over_an_account_through_the_email_claim` |
| P5-02 | High | A cancelled workflow could still create a work item (an `uncertain` action kept its approval; reconcile → `failed` → execute) | cancel refused (409) while any action is executing/uncertain; execution refused for cancelled/rejected runs | `test_adversarial_approval_execution.py::test_cancelled_workflow_never_executes_…` |
| P5-03 | High | Azure DevOps error bodies were stored, returned and logged verbatim — including a PAT echoed by the server | only stable identifiers (`TFnnnn`, exception type, field) leave an error body | `test_adversarial_ai_and_data_exposure.py::test_azure_devops_error_echoing_the_pat_…` (2) |
| P5-04 | Medium (High with auto-analysis) | ReDoS: one crafted email (a run of `<`) blocked a worker's event loop for hours; two further super-linear patterns found in the audit | possessive/atomic patterns, linear encoded-payload scan; < 170 ms at 200k chars | `test_adversarial_input_and_recovery.py::test_injection_screening_is_linear_…`, `test_ai_layer_hardening.py` |
| P5-05 | Medium | Tracebacks were logged unredacted (tokens, email text); exception text was stored in LangGraph checkpoints | render tracebacks before redaction; re-raise a sanitized error from analysis steps | `test_unexpected_worker_exception_text_…`, `test_unexpected_api_exception_…` |
| P5-06 | Medium | A write whose outcome could not be recorded stayed `executing` forever (no reconcile path) | reconcile accepts stale executions (API, presenter flag, UI button) | `test_a_write_whose_outcome_could_not_be_recorded_…`, `test_db_failure_after_a_successful_work_item_create_is_recoverable` |
| P5-07 | Medium | A stalled worker could complete/fail/heartbeat a job another worker had taken over (two concurrent runners) | queue operations fenced on lease holder + attempt | `test_a_stale_worker_cannot_release_a_job_…` (2) |
| P5-08 | Medium | An Azure DevOps create could run ~1,870 s while executions count as live for 300 s, so an in-flight write could be judged dead | write budget = 0.8 × `JOB_LEASE_SECONDS`; timeout → `uncertain` | unit tests in `tests/unit/services/test_execution_service.py` |
| P5-09 | Medium | One message the database rejects blocked the whole mailbox; store/commit errors left the sync RUNNING unrecorded | per-message savepoints, skipped messages recorded, errors through the failure path | `test_one_message_rejected_by_the_database_…`, `test_database_error_while_storing_synced_mail_…` |
| P5-10 | Medium | A transient error during auto-analysis skipped newly synced emails for good | analysis queued in the sync transaction | `test_transient_error_during_auto_analysis_…` |
| P5-11 | Medium (PostgreSQL) | NUL bytes and over-long lowercased addresses from Graph reached PostgreSQL columns | every field cleaned and clipped after lowercasing | `test_normalized_graph_message_fits_postgres_column_constraints` |
| P5-12 | Medium | A job row of an unknown type (e.g. from a newer release) blocked the queue | claim only known types; unknown rows stay queued | `test_an_undecodable_job_row_does_not_block_the_queue` |
| P5-13 | Medium | Injection in attachment names and sender display names was not screened | header fields screened like the body | `test_injection_in_attachment_names_and_sender_name_…` (2) |
| P5-14 | Medium | Invisible marks (variation selectors, CGJ, …) hid instruction overrides; hidden-character tricks removed at ingestion were never reported | match on text without default-ignorables; visible `[hidden characters removed]` marker in subject, body, names, attachment names | `test_invisible_marks_…` (5), `test_invisible_character_tricks_removed_at_ingestion_…` (3), `tests/unit/integrations/test_normalize.py` |
| P5-15 | Medium | A model-proposed `nuvexa-` tag could imitate another action's idempotency tag and mislead its reconciliation | prefix reserved for the system | `tests/security/test_ai_layer_hardening.py` |
| P5-16 | Medium (demo/SQLite) | A failed request could still commit its job (a failed execute request still performed the write) | SQLite savepoints begin a real transaction | `test_failed_execute_request_queues_nothing`, `test_failed_analysis_request_leaves_no_orphan_run` |
| P5-17 | Medium | Mailboxes of deactivated users kept being polled with their stored tokens; demo login ignored `is_active` | owner must be active for polling, sync and demo login | `tests/unit/workers/test_runner.py`, `test_a_deactivated_demo_persona_cannot_sign_in` |
| P5-18 | Low | Viewers could store delegated tokens (connect flows had no permission check) | connect requires `email:sync` / `action:execute`; UI hides the buttons | `test_viewers_cannot_store_delegated_mailbox_or_azure_devops_tokens` (2) |
| P5-19 | Low | `offset=2**63` → HTTP 500 on six list endpoints | shared bounded `Offset` (≤ 1,000,000) | `test_huge_offsets_are_rejected_or_empty_never_a_server_error` |
| P5-20 | Low (PostgreSQL) | NUL in request bodies, query strings or paths → PostgreSQL error (500) | 422 `validation_failed` / `nul_character` for bodies and URLs | `test_nul_characters_in_review_text_…`, `tests/unit/api/test_middleware.py`, `tests/integration/test_api_contract.py` |
| P5-21 | Low | Streamed oversized body → 400 instead of 413 | body buffered up to the limit before the app runs | `test_streamed_oversized_body_is_rejected_with_413` |
| P5-22 | Low (PostgreSQL) | LangGraph checkpointer connections had no statement/lock timeout | same timeouts as the engine | `test_checkpointer_connections_get_statement_and_lock_timeouts` |
| P5-23 | Low | Cancelling a failed analysis never finalized the run (no `completed_at`, finalize event, `workflow_finished` audit) | cancel runs the graph's finalize | `test_cancelling_a_failed_analysis_finalizes_the_run` |
| P5-24 | Low | Provider 401 reported as bad model output; a non-JSON 200 escaped as `internal` | failure categories carried on agent errors | `test_rejected_provider_credentials_…`, `test_non_json_provider_response_…` |
| P5-25 | Low | One malformed tool call discarded every valid proposal of the plan | per-call validation (`create` instead of `parse`) | `test_one_malformed_tool_call_does_not_discard_the_valid_proposals` |
| P5-26 | Low | Model-chosen tag lists and tool-call floods stored unbounded in the timeline | clipped; overflow collapsed | `test_huge_tag_list_…`, `test_a_flood_of_tool_calls_…` |
| P5-27 | Low | Syncing with another user's connection id returned 409 instead of 404 (invariant 5) | 404 for invisible ids | `tests/integration/test_rbac_matrix.py`, `test_api_contract.py` |
| P5-28 | Low | The Azure DevOps delegated connection could not be found or disconnected in the UI | `azure_devops.connection` in `GET /integrations`; Disconnect in the UI | `test_the_azure_devops_delegated_connection_id_is_discoverable`, `IntegrationsPage.test.tsx` |
| P5-29 | Low | CORS origin validation accepted a trailing newline (`$` anchor) | `\Z` | `test_cors_origins_with_a_trailing_newline_are_rejected` |
| P5-30 | Low (latent) | The dashboard's post-login redirect helper was a deny-list; `/\t/evil.example` becomes `//evil.example` | allow-list + same-origin check | `frontend/src/test/adversarial-xss.test.tsx` |
| P5-31 | Low | After the queued-job watch expired the card still claimed to update and kept decisions disabled; search boxes went stale on Back/Forward | watch expiry; URL-synchronized filters | `frontend/src/test/adversarial-ui-state.test.tsx` |
| P5-32 | Low | Bidi overrides in untrusted names could reorder trusted UI sentences; prototype-key enum values could crash rendering | `<bdi>` isolation; own-key lookups | `adversarial-xss.test.tsx`, `prototype-keys.test.tsx` |
| P5-33 | Low | `/assets/` error responses were cached `immutable` for a year (CDN poisoning during a rolling deploy) | status-aware cache map (`no-store` for errors) | `test_adversarial_frontend_headers.py` |
| P5-34 | Low | `normalize_text` was not idempotent (`"e\x00́"`) | NFKC after removals | `tests/unit/core/test_text.py` |
| P5-35 | Low | Status repair bypassed the state machine and audit (invariant 9); a log field named `code` was redacted, hiding failure reasons; HTTP-client loggers logged LLM URLs; redaction missed Basic/`sk-`/PAT shapes | state-machine transitions + `execution_status_repaired` audit; renamed fields; WARNING level; extended patterns | `test_execution_service.py`, `test_dropped_execution_requests_…`, `tests/unit/observability` |
| P5-36 | Medium | With `OTEL_ENABLED=true`, the FastAPI/httpx auto-instrumentation exported OAuth authorization codes and Graph delta tokens (query strings), exception messages (bearer tokens, e-mail addresses) and stack traces to the trace backend (found in the follow-up OpenTelemetry check) | `SanitizingSpanExporter` wraps the OTLP exporter: URLs without query/userinfo, no captured headers/bodies, exception type only | `tests/security/test_trace_export_redaction.py` (real instrumentors) |
| P5-37 | Low | The secret scan never examined merge commits: `git log -p` shows no diff for a merge, so content written while resolving a conflict (three merges in this history) was unscanned. Its commit-count check compared against every commit on HEAD, merges included, which gitleaks cannot reach; it passed only while other fetched branches padded the count, and a shallow clone would also have passed | gitleaks runs with `--log-opts="--full-history --all -m"`; the count is checked against the commits git shows a diff for under the same options; a shallow clone is refused. Rescan of all 57 commits: no leaks | `tests/unit/tooling/test_secret_scan_history.py` (real git repository with an empty commit, a conflict-resolved merge and a clean merge), `test_the_secret_scan_covers_merge_commits_and_refuses_partial_history` |

Defects found in earlier phases (Prompt 1, F-01…F-25) are in [SECURITY_AUDIT.md](SECURITY_AUDIT.md).

## 7. Unresolved risks and accepted exceptions

### Accepted scanner exceptions (`security-exceptions.toml`, enforced in CI)

| Id | Exception | Expires |
|---|---|---|
| SEC-EX-001 | Trivy: HIGH OS-package CVEs **without a published fix** in the backend base image (fixable ones block CI) | 2026-12-31 |
| SEC-EX-002 | bandit B608 / ruff S608 false positive on the WIQL query | 2027-10-31 |
| SEC-EX-003 | semgrep `unverified-jwt-decode`: tenant pre-read before full verification | 2027-10-31 |
| SEC-EX-004 | semgrep uv dependency cooldown deferred (would force 27 downgrades) | **2026-11-02** — enable `exclude-newer = "7 days"` |
| SEC-EX-005 | gitleaks: five synthetic test fixtures, allowlisted by rule + file + exact line | 2027-10-31 |
| SEC-EX-006 | gitleaks: one historical commit of this report that quoted a SEC-EX-005 fixture (commit-pinned) | 2027-10-31 |
| SEC-EX-007 | Trivy config AZU-0013 false positive on the generated Azure ARM template's Key Vault | 2027-04-30 |

### Unresolved risks

| Risk | Impact | Mitigation / owner action |
|---|---|---|
| Live integrations never exercised (§8) | Behaviour of real Entra, Graph, Azure DevOps and LLM providers may differ from the fakes | Run §8 against sandbox tenants before production |
| Delegated (Entra) reconciliation uses the acting user's token; WIQL returns only items that user can read (not verified) | A reconciler who cannot read the target area path would see "absent" → `failed`; a retry could create a duplicate | Use the PAT/service connection for production, or ensure approvers can read the configured area paths; verify in §8 |
| R-1 Rate limiting — **mitigated (opt-in)** | With the default `RATE_LIMIT_BACKEND=memory` limits multiply with replicas (a production API logs `rate_limits_are_per_process`) | Set `RATE_LIMIT_BACKEND=database` (shared counters in PostgreSQL, keyed by HMAC digests) on every replica, or limit at the ingress |
| R-2 Injection screening is English-only (5 strict xfails) | Non-English or homoglyph overrides are not flagged | Screening never grants anything; humans approve every action; destinations fixed by policy |
| R-3 Audit HMAC detects modification, not deletion by a database superuser | Silent removal of audit rows by a DBA | Restrict superuser access; PostgreSQL audit logging and backups |
| R-4 Runtime database role — **mitigated** | API and worker now connect as a DML-only role (`nuvexa_app`; no DDL, no TRUNCATE, no UPDATE or DELETE on `audit_events` — the retention purge goes through an owner-defined function that keeps at least 30 days); migrations run as the owner; `nuvexa migrate` re-applies the grants on every release. Remaining: a compromised runtime could lower a tenant's audit retention to the 30-day floor | Production: create the runtime role and split the secrets as in [deployment.md](deployment.md) §Database |
| Bootstrap admin by e-mail before first sign-in | Another principal in an allowed tenant with a listed e-mail could claim admin before the named person signs in | Sign the administrator in right after deployment, then clear `BOOTSTRAP_ADMIN_EMAILS`; consider bootstrap by object id |
| No separation of duties | A member approves and executes their own proposals; approvers can edit and approve others' | Product decision; document in the operating procedure or add a "four-eyes" setting |
| Approvals survive the approver's demotion/deactivation | A queued execution may run on an approval whose approver lost the role | Product decision; re-approval on role change is a possible follow-up |
| Messages the database rejects are skipped (recorded), not retried | Such messages are not analyzed until they change in Graph | Visible as `mailbox_sync_failed` (`skipped_messages`) — monitor |
| Re-created Entra user (same e-mail, new object id) is refused | Sign-in blocked until an admin clears the old row (no UI) | Operations runbook ([setup-microsoft-entra.md](setup-microsoft-entra.md)) |
| Heuristic demo agent: 47 % category accuracy on the holdout set | Demo mode only — real use needs the OpenAI/Azure OpenAI provider, whose quality on real mail was not measured | Evaluate the configured model on representative, consented mail before go-live |

Not performed: dynamic application scanning (DAST), external penetration testing, load and soak testing,
review of a cloud deployment (none was created). Repository settings are not part of the code:
branch protection is **not** enabled yet ([CI_SECURITY_GATES.md](CI_SECURITY_GATES.md) §6), exception
owners are role placeholders, and five Dependabot pull requests are open (they need a rebase; #3 —
TypeScript 7 — fails the gates on its own).

## 8. Integration tests that need credentials or sandbox access (not run)

| Test | Needs | Covers |
|---|---|---|
| Microsoft Entra sign-in end to end (single- and multi-tenant, allow-list, consent, token refresh, revoked grant) | Entra app registration in a test tenant | real ID tokens, JWKS rotation, MSAL behaviour |
| Microsoft Graph mail sync (delta query, paging, `Retry-After`, hidden-character and HTML mail, large mailboxes) | test mailbox with `Mail.Read` consent | real delta semantics and throttling |
| Azure DevOps create, `validateOnly`, WIQL lookup and reconcile with a PAT and with delegated tokens (incl. the area-path read question above) | sandbox organization/project | real error bodies (identifier allow-list), tag lookup latency, permissions |
| LLM providers (`@pytest.mark.live_llm`): OpenAI and Azure OpenAI (key and Entra ID), structured outputs, content filters, 401/429/5xx | test API keys / deployment | real response shapes, the new `with_raw_response` path, categories |
| OTLP trace export to a real collector | collector | transport/auth (span content is sanitized and tested offline with the real instrumentors) |
| Deployment (Azure Container Apps or similar), TLS, HSTS at the ingress, managed identity | cloud subscription (not authorized) | runtime configuration |

All of these are exercised offline against faithful fakes (MSAL behaviour, signed ID tokens, respx/httpx2
mock transports); fakes cannot prove provider behaviour.

## 9. What must be monitored after deployment

* Workflow failures by `error_category` (`authorization`/`configuration` for the LLM mean credentials or
  deployment settings; `llm_output` spikes mean model drift), and runs stuck in in-flight states.
* Actions in `uncertain` or stale `executing`; `execution_blocked`, `execution_status_repaired` and
  `payload_integrity_failure` audit events (each should be rare and investigated).
* `mailbox_sync_failed` events (incl. `skipped_messages`), sync duration, `needs_reauth` connections.
* Refused sign-ins (`login`, `outcome=denied`, `reason=identity_mismatch|deactivated`) — repeated identity
  mismatches can indicate an attempted takeover.
* Injection flags (`hidden_text`, instruction overrides) per tenant; reviewer overrides of flagged proposals.
* Job queue: dead-lettered jobs, `job_lease_lost`, queue depth and age; worker event-loop lag.
* Duplicate work items per idempotency tag in Azure DevOps (should be zero).
* Audit integrity endpoint (`GET /audit-events/integrity`) on a schedule.
* Scanner exception expiries (CI fails on expiry; SEC-EX-004 on 2026-11-02) and Dependabot updates.

## 10. Recommendation

**READY WITH DOCUMENTED RISKS** for a **supervised pilot**: sandbox Entra tenant and Azure DevOps
project, consenting test mailboxes, a test LLM key, synthetic or non-confidential mail, and the
monitoring in §9.

Rationale: every defect found by adversarial testing and the follow-up checks (36, three of them high severity) is fixed with a
regression test that failed first; the full backend suite passes on SQLite and PostgreSQL with 99.7 %
line + branch coverage, the frontend with its thresholds; the critical invariants (§4) are covered by
behaviour-level and adversarial tests; the scans show no fixable vulnerability and no unregistered
suppression (§5); the system E2E suite passes against real containers (§2.2). The remaining risks (§7)
are documented, owned by a role, and mostly operational.

**Not ready for production mailboxes** until:

1. The live integration tests in §8 pass against sandbox tenants (in particular the Azure DevOps
   reconcile path under delegated tokens).
2. Branch protection requires `CI gate`; exception owners and the bootstrap administrator are named.
3. The runtime database role and secret split (R-4, implemented) are applied in the target environment and, for more than one replica, `RATE_LIMIT_BACKEND=database` is set (R-1).
4. The configured LLM's quality is evaluated on representative, consented mail.
5. SEC-EX-004 is closed (uv cooldown enabled) or renewed with review before 2026-11-02.
