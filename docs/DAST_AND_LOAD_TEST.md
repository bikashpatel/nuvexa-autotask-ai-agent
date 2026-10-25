# DAST and load smoke test

Dynamic application security testing (OWASP ZAP) and a load smoke test (k6) of the Docker Compose demo
stack: nginx serving the dashboard and proxying `/api`, the FastAPI API, the worker and PostgreSQL, in demo
mode (synthetic mailbox, heuristic agent, mock Azure DevOps). The release report
([RELEASE_READINESS_REPORT.md](RELEASE_READINESS_REPORT.md) §7) listed both as not performed; this
document records the first runs.

**Result (2026-10-25, commit `933273e` plus this change):** no confirmed vulnerability. ZAP raised two
High alerts in some runs — SQL injection (low confidence) on an unconfigured sign-in route that always answers
a documented `500`, and "PII disclosure" on a random request id — both verified false positives; every other
alert is a false positive, an accepted design decision or informational (§2.4). No DEFECT test was needed (§4). Under the smoke load (25 VUs, about 18 requests per
second) every endpoint group answered in under 35 ms at p95 with no errors, no database pool pressure and
worker jobs done in about 2 s (§3.3). **Nothing here changes the release recommendation** (§5).

## 1. Safety and scope

Only isolated, throwaway stacks on this machine were scanned or loaded:

| | DAST (`scripts/dast.sh`) | Load (`scripts/load-smoke.sh`) |
|---|---|---|
| Compose project | `nuvexa-dast` (removed with `down -v` afterwards) | `nuvexa-load` (removed with `down -v`) |
| Host ports (127.0.0.1 only) | 38080 / 38000 / 35432 | 39080 / 39000 / 39432 |
| Images | `nuvexa-backend:isolated`, `nuvexa-frontend:isolated` ([`loadtest/compose.isolated.yml`](../loadtest/compose.isolated.yml)): building them never retags the `:local` images of a running demo stack | same |
| Scanner/load generator network | an `--internal` Docker network holding only that stack's `frontend` and `api` containers: no route to anything else, including the internet | an `--internal` network holding only that stack's `frontend` |
| Telemetry | ZAP `-silent` (no update check, no call-home, no add-on updates) | k6 `--no-usage-report` |

Both scripts refuse the Compose projects `nuvexa` and `nuvexa-e2e`, refuse a non-loopback `*_BASE_URL`
unless `*_ALLOW_NON_LOOPBACK=1`, and refuse the developer-demo and E2E ports (8080/8000, 18080/18000,
28080/28000) unless `*_ALLOW_SHARED_PORT=1`. Active scans and the load flows write data (approve, execute,
edit settings), so they must never point at a stack someone uses. The demo stack on port 18080, the E2E
stack and every external host were not touched. All data is synthetic (`example.com` personas and mail).

## 2. DAST with OWASP ZAP

### 2.1 How the scans work

| Step | What happens |
|---|---|
| Stack | built from the working tree with `docker-compose.yml` defaults: `APP_ENV=development`, `DEMO_MODE=true`, so the OpenAPI document is served (`/api/v1/openapi.json`) and demo sign-in is enabled |
| Targets ([`prepare_targets.py`](../loadtest/zap/prepare_targets.py)) | signs in every persona (`POST /api/v1/auth/demo-login`), syncs the member's demo mailbox and analyzes three emails, then writes one OpenAPI file per persona: `servers` = the URL ZAP uses; FastAPI's OpenAPI 3.1 `anyOf: [X, null]` reduced to `X` and enums inlined (ZAP otherwise sends `?status=&category=`, every list call is a 422 and the filters are never attacked); real ids, the action's current `version` + `payload_hash` and the tenant settings as examples for every parameter and body. `DELETE /integrations/connections/{id}` always targets a connection the persona does not own |
| Authentication ([`nuvexa_hook.py`](../loadtest/zap/nuvexa_hook.py)) | ZAP replacer rules add `Cookie: nuvexa_session=…; nuvexa_csrf=…` and `X-CSRF-Token` to every request, except `/auth/logout*` and `/auth/demo-login*` (both end the presented session). The hook checks `GET /auth/me` is 200 before the scan and records whether the session survived it |
| SPA crawl ([`spa_crawl.py`](../loadtest/zap/spa_crawl.py)) | ZAP's own browser crawlers (client spider, AJAX spider) cannot start Firefox in the ZAP container on Docker Desktop for Apple silicon (OOM kill, "Failed to read marionette port"). The locked Playwright Chromium (e2e group) signs in through the real login page, opens all 13 dashboard routes and detail pages, and records a HAR; the hook imports it into ZAP, so passive rules see what the dashboard really loads (240–276 requests per persona) |
| Baseline (passive) | per persona (anonymous, viewer, member, approver, admin): `zap-baseline.py` on the SPA (spider + imported crawl) and `zap-api-scan.py -S` on the API (OpenAPI import, no attacks) |
| Active | `zap-api-scan.py -c` [`api-scan-rules.conf`](../loadtest/zap/api-scan-rules.conf): Default Policy, all 53 active and 60 passive rules at WARN, nothing ignored; anonymous, member and admin; 4 threads per host |
| Supplementary active pass | the same active scans with the API's in-process rate limits switched off ([`compose.no-rate-limits.yml`](../loadtest/zap/compose.no-rate-limits.yml), isolated stack only). With the shipped limits about 19,000 of the 74,000 scan requests were answered 429 before the handler ran, mostly on `PATCH /actions/{id}`, `/approve`, `/execute`, `/reject`, `/cancel`, `/integrations/azure-devops/validate` and `/emails/sync` — exactly the security-relevant write routes |

Two harness bugs were found and fixed during the runs and are pinned by
`tests/unit/tooling/test_dast_tooling.py`: the empty optional parameters above, and a session that the active
scan logged out halfway through (it requested `/auth/logout/`; FastAPI redirects that to `/auth/logout` and ZAP
followed the redirect with the session cookie). Only the runs after both fixes are reported below.

### 2.2 Commands

```bash
scripts/dast.sh                              # isolated stack: baseline + active scans, then tear down (~10 min)
DAST_PHASES=active DAST_DISABLE_RATE_LIMITS=1 DAST_REPORTS=reports/dast-no-limits scripts/dast.sh
DAST_PHASES=baseline scripts/dast.sh         # passive scans only
DAST_KEEP=1 scripts/dast.sh                  # keep the stack for manual verification
```

Reports (ignored by git): `reports/dast/{baseline,active}-<scan>-<persona>.{html,json,log}`, the per-scan
session check, `summary.md` (alerts grouped by rule and URL pattern) and `compose.log`. The script exits 1 when a
scan fails or any **High** alert is reported. The final default run exited 0; the earlier default run (SQL
injection) and the second supplementary pass (PII disclosure) exited 1 on the false positives in §2.4. The gate is deliberately not weakened (no rule is
ignored): a High alert always needs the triage recorded here. Knobs: `DAST_PERSONAS`,
`DAST_ACTIVE_PERSONAS`, `DAST_ZAP_HEAP` (1g), `DAST_ZAP_MEMORY` (3g), `DAST_ZAP_PSCAN_THREADS` (2: with one
passive-scan thread per core ZAP was OOM-killed on the busy Docker Desktop VM), `DAST_ACTIVE_MAX_MINUTES` (45).

### 2.3 Scans performed

| Scan | Personas | URLs | Session kept |
|---|---|---|---|
| SPA baseline (spider + Playwright HAR) | anonymous, viewer, member, approver, admin | 72–106 per persona | yes (all) |
| API baseline (OpenAPI, passive) | anonymous, viewer, member, approver, admin | 58 operations each | yes (all) |
| API active, rate limits on | anonymous, member, admin | 58 operations + rule-generated paths; 73,930 API requests in the whole run (18,904 answered 429) | yes (all) |
| API active, rate limits off | anonymous, member, admin | as above, no 429 | yes (all) |

After the harness fixes there were two complete default runs and two supplementary passes, all on the same
application code. The final default run is the reference; alerts that appeared only in another run are marked.

### 2.4 Findings and triage

Counts are instances over all scans of the final default run unless marked.

| Alert (ZAP rule) | Risk (confidence) | URL pattern | Verdict | Reasoning |
|---|---|---|---|---|
| SQL Injection (40018) | **High** (Low) | `GET /api/v1/auth/login?redirect_to='` (1 instance, member active scan of the first default run; not raised in the final run or the supplementary passes) | **False positive** | The only evidence is `HTTP/1.1 500`. With Entra ID not configured this route returns the same `500 configuration_error` "Microsoft sign-in is not configured." for every input (verified: `redirect_to=/inbox`, `redirect_to='` and no parameter give identical responses), because `EntraAuthService._check_login_policy` runs before `redirect_to` is used. When Entra is configured, `redirect_to` is checked by `safe_redirect_path` and stored through the ORM with bound parameters; there is no SQL built from input (semgrep/bandit clean). Root cause of the alert: the documented 500 ("A Server Error response code" below) |
| PII Disclosure (10062) | **High** (Medium) | `POST /api/v1/actions/{id}/reconcile` (1 instance, admin, supplementary pass only); evidence `569729163174`, "Maestro" | **False positive** | The 36 reconcile responses in that pass were 401/409 error envelopes, whose only digit-bearing fields are the random 32-hex-character `correlation_id` (also sent as `X-Request-ID`); a 12-digit run inside a random hex id passed the Luhn check. The stack holds only synthetic data, and the demo fixture contains no 12–19 digit number |
| A Server Error response code was returned (100000) | Low (High) | `GET /api/v1/auth/login[?…]`, `GET /api/v1/integrations/microsoft/connect[?…]` (only these two routes: 36 responses; about 1,100 in a pass without rate limits) | **True positive, by design** — no security impact | Documented in [api.md](api.md) (`500 configuration_error`, "Microsoft sign-in requested but Entra ID is not configured") and pinned by `tests/integration/test_api_contract.py::test_connect_endpoints_without_entra_configuration`. The body is the generic envelope (code, message, correlation id), no stack trace. Side effects in a demo deployment: anyone can cause 5xx responses and error-level log events (limited to 20/min per IP by the `auth` rate limit), which pollutes 5xx-based monitoring and is what produced the SQL-injection false positive. **Recommendation (product decision, not a DEFECT):** answer `409 integration_not_configured`, as the Graph mailbox factory already does for "Microsoft Entra ID is not configured" and as `/integrations/azure-devops/connect` does (`409 conflict`) when its mode needs no user connection, and log at warning level |
| Application Error Disclosure (90022) | Low (Medium) | same two routes | False positive | Triggered by the 500 status alone; the response discloses nothing beyond the documented error code and message |
| CSP: style-src unsafe-inline (10055) | Medium (High) | every SPA page (`/`, `/login`, `/inbox`, …) | **True positive, accepted** | Required by Mantine, which injects `<style>` elements and inline style attributes at runtime; a static nginx cannot issue per-request nonces. Documented in `frontend/nginx.conf`. Residual risk is CSS injection only: `script-src 'self'`, `object-src 'none'`, `frame-ancestors 'none'`, and the dashboard never renders untrusted HTML (email bodies are plain text) |
| Cookie No HttpOnly Flag (10010) | Low (Medium) | `POST /api/v1/auth/demo-login` → `nuvexa_csrf` | False positive (by design) | The CSRF double-submit cookie must be readable by the SPA, which echoes it in `X-CSRF-Token`; the server compares it with the hash stored for the session. The session cookie `nuvexa_session` is HttpOnly and SameSite=Lax |
| Unexpected Content-Type was returned (100001) | Low (High) | non-API paths probed by rules (`/trace.axd`, `/latest/meta-data/`, `/api` without the slash…) and dotfile probes under `/api/…/.env`, `/.htaccess` | False positive | Unknown non-API paths get the SPA's `index.html` (client-side routing); every dotfile path, also under `/api/`, gets nginx's own 404 and never reaches the API (`location ~ /\.`). Cosmetic: `/api` without a trailing slash is served by the SPA rather than the API |
| User Agent Fuzzer (10104) | Informational | `POST /actions/{id}/approve`, `/execute`, `/cancel` | False positive | Responses differed because the action's state changed between requests (200, then 409), not because of the User-Agent |
| A Client Error response code was returned (100000) | Informational | all routes | Expected | 401 without a session, 403 for roles without the permission (4,846 of them: the member's `PUT /settings`), 404 for invisible resources, 409 for stale versions and state conflicts, 422 for fuzzed input, 429 from the rate limits |
| Information Disclosure - Suspicious Comments (10027) | Informational | `/assets/index-*.js` | False positive | The word "query" inside a CSS media-query string of the minified bundle, not a comment |
| Modern Web Application (10109), Session Management Response Identified (10112) | Informational | SPA pages; demo-login | Informational | Correct detection of a single-page app and of the session cookies |

Rules that raised nothing include the injection rules (SQL incl. the time-based variants, OS command, server-side
code, template injection, XPath, XXE, CRLF), XSS (reflected, persistent, DOM), path traversal, remote file
inclusion, external redirect, cross-domain misconfiguration, cloud-metadata access, Log4Shell/Spring4Shell,
hidden files, and the missing-header rules (anti-clickjacking, X-Content-Type-Options, CSP): every response,
including errors and the proxied API, carries the nginx security headers. Not raised because the stack speaks
plain HTTP: the Secure-cookie and HSTS rules. Production sets `Secure` cookies (settings validation refuses
`COOKIE_SECURE=false` in production). HSTS was left to the TLS terminator, but the Container Apps ingress
in `infra/azure/` adds none; after this scan nginx sends `Strict-Transport-Security: max-age=31536000;
includeSubDomains` on every response (browsers ignore it over plain HTTP). Verify both on the real ingress.

### 2.5 What the DAST does not show

* ZAP has no model of the authorization rules: it cannot tell that a member reading another user's action is
  wrong. Object-level and role checks are covered by `tests/security/test_adversarial_access_control.py` and
  the E2E suite, not by this scan.
* Business logic (approval binding, idempotency, the single external write path) is out of reach of a
  generic scanner; see [SECURITY_CONTROLS.md](SECURITY_CONTROLS.md).
* Only the demo configuration was scanned: no Entra ID, Graph or Azure DevOps endpoints, no TLS. The
  `development` profile also serves the OpenAPI document and Swagger UI (`EXPOSE_API_DOCS` unset). If the
  publicly tunnelled demo uses the Compose defaults, it exposes the API description and issues cookies
  without `Secure` over its HTTPS tunnel; set `APP_ENV`/`COOKIE_SECURE`/`EXPOSE_API_DOCS` for any shared demo.
* Time-based rules ran on a busy laptop; none fired, but a loaded machine makes such rules noisy, so a
  finding of that kind would need a re-test.

## 3. Load smoke test with k6

### 3.1 Profile

[`loadtest/smoke.js`](../loadtest/smoke.js), run by [`scripts/load-smoke.sh`](../scripts/load-smoke.sh):

* **Data:** the three demo mailboxes are synced, then every synced email is copied 100 times in SQL
  (`LOAD_SEED_COPIES`), so the lists, search and dashboard run over 3,030 synthetic emails instead of 42.
* **Sessions:** `setup()` signs each persona in twice; VUs share those sessions (cookie + `X-CSRF-Token`),
  because demo sign-in is limited to 20/min per client IP. Per-user limits therefore apply to 4 shared users.
* **`browse` scenario (ramping VUs):** 0 → 10 VUs (1 min) → 25 VUs (2 min) → hold 3 min → 0 (1 min). Each
  iteration is one dashboard page view with the API calls that page makes — overview (dashboard summary +
  pending approvals), inbox list, inbox search, email detail, workflows, workflow detail, approvals, action
  detail + history, audit log, tasks — weighted member 40 %, approver 30 %, admin 20 %, viewer 10 %, with 1–3 s
  think time; 3 % of page views click "Sync", 1 % sign in and out again.
* **`flows` scenario:** 6 flows per minute for 6 minutes: an owner analyzes an unanalyzed email, an approver
  or admin opens the proposal, approves the exact `version` + `payload_hash`, executes it (mock Azure DevOps)
  and waits for the outcome.
* **Outcome accounting:** 429 is counted in `rate_limited` and kept out of the latency trends and of the
  error rate; 409 is an expected outcome of the flows (counted separately); anything else unexpected is
  `unexpected_responses`.
* **Server side:** `pg_stat_activity` sampled every 5 s, `docker stats` every 10 s, the jobs table queried
  for queue wait and end-to-end job latency, and the API/worker logs scanned for 5xx, pool timeouts
  (`QueuePool limit`, `TimeoutError`, "too many clients") and warning/error events.

```bash
scripts/load-smoke.sh                     # isolated stack, seed, run (~8 min of load, ~11 min in total), tear down
K6_PROFILE=quick scripts/load-smoke.sh    # 30-second check of the script, no measurement
LOAD_MAX_VUS=30 LOAD_FLOW_RATE=10 LOAD_HOLD=5m scripts/load-smoke.sh
```

Reports (ignored by git): `reports/load/k6-summary.json`, `k6.log`, `jobs.txt`, `pg-activity.csv`,
`docker-stats.csv`, `containers.txt`, `server.txt`, `summary.md`. Exit status: k6's (99 when a threshold
fails), or 1 when the logs show a 5xx or a pool timeout.

### 3.2 Environment and caveats

Apple M5 Pro (15 cores, 24 GB), macOS 26.6.2, Docker Desktop 29.8.2 with a 7.7 GiB / 15-CPU Linux VM that
was shared with other projects' containers during the runs (at one point it had under 1 GB free). k6 ran in
the same VM as the stack, so the generator competes with the system under test, and Docker Desktop's
virtualised networking and disk are not a cloud runtime. One API process (uvicorn, no workers), one worker
(`WORKER_CONCURRENCY=4`), database pool 5 + 5 overflow per process, heuristic agent and mock Azure DevOps (no
LLM or Azure DevOps latency). **Treat the numbers as a relative baseline for this profile, not as capacity
or production latency.** Two runs were recorded; the second is reported, the first agreed within a few ms.

### 3.3 Results (run of 2026-10-25, 7.5 min of load)

Client side (k6, milliseconds, 429s excluded from latency):

| Endpoint group | Requests | p50 | p95 | p99 | max | 429 |
|---|---|---|---|---|---|---|
| dashboard summary | 740 | 8.4 | 16.7 | 19.4 | 34.2 | 0 |
| inbox list | 1,251 | 5.8 | 12.7 | 14.4 | 19.8 | 0 |
| inbox search (`q=`) | 396 | 10.3 | 20.6 | 23.0 | 24.9 | 0 |
| email detail | 409 | 3.0 | 5.2 | 6.4 | 7.5 | 0 |
| workflows list | 515 | 4.9 | 10.6 | 11.9 | 13.7 | 0 |
| workflow detail | 279 | 6.1 | 11.0 | 13.5 | 14.8 | 0 |
| actions list (approvals) | 1,443 | 9.8 | 32.2 | 40.3 | 50.3 | 0 |
| action detail | 511 | 4.5 | 10.3 | 15.0 | 17.3 | 0 |
| action history | 292 | 2.7 | 4.6 | 6.0 | 8.5 | 0 |
| audit log | 154 | 5.0 | 11.6 | 12.8 | 12.9 | 0 |
| tasks | 72 | 3.8 | 8.7 | 10.0 | 10.2 | 0 |
| auth/me | 1,141 | 3.2 | 7.8 | 10.1 | 15.7 | 0 |
| demo sign-in / sign-out | 53 / 45 | 5.5 / 2.9 | 8.9 / 4.4 | 10.3 / 6.6 | 11.1 / 7.6 | 0 |
| sync (POST) | 102 | 6.8 | 11.2 | 12.5 | 14.3 | **7** |
| analyze (POST) | 37 | 10.9 | 17.9 | 19.0 | 19.3 | 0 |
| approve (POST) | 37 | 10.7 | 14.6 | 19.4 | 21.6 | 0 |
| execute (POST) | 37 | 7.3 | 10.8 | 11.3 | 11.3 | 0 |
| **all requests** | **7,521** (17.9/s) | 5.9 | 19.9 | 32.1 | 50.3 | 7 |

* **Errors:** 0 unexpected responses (0.000 %), 0 failed requests, no 5xx (API log: 7,293 × 200,
  176 × 202, 45 × 204, 7 × 429). 0 conflicts in the flows.
* **429s:** 7, all on `POST /emails/sync` — the 10/min per-user limit, reached because 25 VUs share four
  users (first run: 10). Expected and not an error.
* **Flows:** 37 of 37 completed (analysis → proposal → approval → execution → `completed`).
  POST analyze → proposal ready: p50 1.0 s, p95 1.0 s, max 1.5 s. POST execute → outcome: p50 2.0 s,
  p95 2.1 s.
* **Worker jobs** (jobs table, created during the run):

  | Job | n | queue wait p50 / p95 (s) | queued → done p50 / p95 / p99 (s) |
  |---|---|---|---|
  | `analyze_email` | 37 | 0.54 / 0.95 | 0.58 / 1.00 / 1.03 |
  | `resume_workflow/refresh` (after approval) | 37 | 0.71 / 0.93 | 0.72 / 0.94 / 0.95 |
  | `resume_workflow/execute` | 37 | 1.71 / 1.94 | 1.73 / 1.97 / 1.99 |
  | `sync_mailbox` | 97 | 0.52 / 0.98 | 0.53 / 0.98 / 1.01 |

  Jobs are dominated by queue wait: the worker polls every second when idle
  (`WORKER_POLL_INTERVAL_SECONDS=1`), and an execute job waits for the run's preceding `refresh` job (same
  `concurrency_key`). Processing itself takes tens of milliseconds with the heuristic agent and mock Azure
  DevOps.
* **Database pool:** at most 12 connections to the `nuvexa` database (API + worker + the sampler), at most 2
  active at once, none waiting on a lock; no `QueuePool limit`/timeout, no "too many clients", no warning
  or error log events in the API or worker.
* **Resources:** API CPU p50 10.9 % / max 23.6 % of one core, ~107 MiB; PostgreSQL 4.7 % / 10.2 %, ~114 MiB;
  worker < 5 %, ~123 MiB; nginx < 1 %. The smoke load uses a small fraction of one API process.

### 3.4 Thresholds (baseline, not an SLA)

Set in `loadtest/smoke.js` at roughly ten times the observed p95, so they catch order-of-magnitude
regressions (an N+1 query, a lost index, pool exhaustion, a blocking call on the event loop) without
failing on laptop noise:

| Metric | Threshold |
|---|---|
| `unexpected_responses` (error rate excluding 429 and documented 409s) / `http_req_failed` | < 1 % / < 1 % |
| all requests | p99 < 500 ms |
| auth/me, email detail, action history | p95 < 100 / 150 / 150 ms |
| dashboard, inbox list, inbox search, workflows, workflow detail, action detail, audit log, tasks, sign-in, sync | p95 < 250 ms |
| actions list, analyze, approve, execute (POST) | p95 < 300 ms |
| analyze → proposal ready / execute → outcome | p95 < 10 s / < 15 s |
| completed flows | ≥ 10 (≥ 1 for `K6_PROFILE=quick`) |

The heaviest read is the approvals list (`GET /actions?status=pending_approval&status=uncertain`, p95
32 ms over 3,030 emails), which builds per-action permissions; it is the first query to watch as data grows.
A real capacity test needs a production-like runtime, realistic data volumes and audit-log sizes, several
API replicas behind the ingress (with the shared rate limit, risk R-1) and the real LLM provider latency.

## 4. DEFECT tests

None. No alert or load observation is a confirmed defect: the only true positives are documented and
tested design decisions (the `500 configuration_error` for an unconfigured Entra ID, the Mantine CSP
exception, the readable CSRF cookie). Changing the 500 to `409 integration_not_configured` is recommended
above; it would replace the expectation in `test_connect_endpoints_without_entra_configuration` and the
row in [api.md](api.md) rather than fix a broken behaviour.

The harness itself is covered by `tests/unit/tooling/test_dast_tooling.py`: every parameter and body of the
scan specification has a valid example and no `X or null` schema is left, the DELETE example never targets
the scanning persona's own mailbox, and the session replacer skips every variant of the session-ending
routes.

## 5. Effect on the release recommendation

None. The recommendation in [RELEASE_READINESS_REPORT.md](RELEASE_READINESS_REPORT.md) §10 (ready with
documented risks for a supervised pilot) stands. Items to carry into the report: DAST and a load smoke test
have now been performed (§7 "Not performed" can be narrowed to penetration, soak and production-like load
testing); the open recommendation to return 409 instead of 500 for an unconfigured Microsoft sign-in; any
shared demo should not run the `development` profile (§2.5); and HSTS (now sent by nginx) and `Secure`
cookies are to be verified on the real TLS ingress.

## 6. Tools and versions

| Tool | Version / image | Notes |
|---|---|---|
| OWASP ZAP | `zaproxy/zap-stable:2.17.0@sha256:5ccfff9a411743ab2615136a2edd7416bec9ce41572fa94e935df38c14adb2b6` | image published 2026-10-16 (≥ 7 days before use); multi-arch index; add-ons as bundled (`-silent`: no updates) |
| k6 | `grafana/k6:2.2.0@sha256:9bd01d6941fca969cb61bb57d2da5ee9b385fe2aa8881df3798c196564d6ace6` | published 2026-08-10; 2.3.0 and 1.8.2 (2026-10-24) were inside the 7-day cooldown |
| Playwright (SPA crawl) | 1.64.1 from `uv.lock` (e2e group), Chromium headless shell | the same locked dependency as the E2E suite |
| shellcheck | `rhysd/actionlint:1.7.12@sha256:b1934ee5…` (pinned in `scripts/security-scan.sh`) | both scripts are clean |
| System under test | FastAPI 0.162.0 on uvicorn, PostgreSQL 18 (`postgres:18-alpine@sha256:898c60cb…`), nginx-unprivileged 1.30.6 | images built from this commit |

Update the pinned images deliberately, after the 7-day release cooldown
([CI_SECURITY_GATES.md](CI_SECURITY_GATES.md) §5), and re-run both scripts.
