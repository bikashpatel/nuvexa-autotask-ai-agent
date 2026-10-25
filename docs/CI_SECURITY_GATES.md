# CI/CD quality and security gates

The pipeline in `.github/workflows/ci.yml` stops vulnerable or inadequately tested changes from merging
or being released. This document describes what each stage checks, which conditions block a release,
the exception policy, the pipeline's own security practices, and how to reproduce every job locally.

A green pipeline reduces risk. It does not prove the application is bug-free or invulnerable: see
[What the gates do not cover](#9-what-the-gates-do-not-cover).

## 1. Pipeline overview

`CI gate` is the single required status check. It needs every job below and fails if any of them
failed, was cancelled, or was skipped when it should have run. `tdd-guard` runs only on pull requests
and `e2e` only on request.

| Job | Stages (prompt numbering) | Blocks when |
|---|---|---|
| `tdd-guard` (pull requests) | change policy | production code (`app/`, `frontend/src/`) changes without a test change, unless the PR description has `TDD-Exempt: <reason>` |
| `backend` | 1 locked install · 2 ruff lint + format · 3 mypy strict · migrations on PostgreSQL and SQLite · 4 unit · 5 integration (PostgreSQL service) · 6 security regression · AI evaluation · 7–8 coverage report and gate | any lint, type, migration or test failure; line + branch coverage below `fail_under` (99 %) |
| `backend-sqlite` | the whole suite on SQLite (the demo-mode database) with the coverage gate | as above |
| `frontend` | 1 `npm ci` · 2 Prettier + ESLint · 3 `tsc` · tests with coverage thresholds · 12 build | any failure; coverage below `frontend/vite.config.ts` thresholds (statements 99, branches 98, functions 100, lines 100) |
| `dependency-audit` | 9 pip-audit, npm audit, npm registry signatures · 15 SBOMs | a known vulnerability in any locked Python package (all groups, hash-verified); a high/critical npm advisory; a package failing registry signature verification |
| `secret-scan` | 10 gitleaks over every commit | any leak; a scan that saw fewer commits than HEAD has |
| `sast` | 11 semgrep (Python, TypeScript/React, OWASP Top 10, Dockerfile, GitHub Actions, secrets) + bandit | any semgrep finding (`--error`); any bandit finding at any severity |
| `containers` | 12 image builds (never pushed) · 13 Trivy vulnerability + secret scan · 15 image SBOMs and metadata | any HIGH/CRITICAL vulnerability with a published fix; any secret in an image |
| `pipeline-config` | 13 Trivy misconfiguration scan · actionlint · zizmor · exception register | any misconfiguration (LOW and above); any workflow lint or workflow-security finding; an unregistered, unknown, uncited or expired exception |
| `e2e` (opt-in) | 14 system E2E: isolated Compose stack (nginx, API, worker, PostgreSQL), Playwright UI | any failure |
| `ci-gate` | joins all of the above | any blocking result |

Stage 15 (test reports and artifact metadata): every job uploads its JUnit XML, coverage, SARIF/JSON
scan results and SBOMs as artifacts (14–30 days), and writes coverage and scan summaries to the run's
job summary. The `containers` job records each image's ID, creation time, size and runtime user.

`tests/security/test_ci_pipeline_contract.py` checks the pipeline itself. Removing a stage, letting a
scanner or test soft-fail (`continue-on-error`, `|| true`, `--exit-zero`), dropping a job from the gate
or lowering a coverage threshold fails that test, so such a change cannot slip through as a plain edit.

## 2. Release-blocking conditions

| Condition | Enforced by |
|---|---|
| Tests or critical security assertions fail | `backend`, `backend-sqlite`, `frontend`, `e2e` (when run); `tests/security/` covers the security invariants |
| Coverage thresholds not met | `coverage report` (`fail_under = 99`, line + branch); Vitest thresholds; the contract test forbids lowering either |
| New critical/high vulnerability | `dependency-audit`, `containers` (policy in §3; exceptions in §4) |
| Secret in tracked files or history | `secret-scan` |
| Type checking, linting or build validation fails | `backend`, `frontend`, `containers` |
| Security-sensitive configuration missing or unsafe | `pipeline-config` (Trivy config, actionlint, zizmor); `tests/security/test_supply_chain_configuration.py` (pinning, least-privilege tokens, no secrets reachable from PR code, non-root images, cooldowns, npm install scripts disabled, precise secret allowlists); production settings validation tests (`tests/unit/core`, `tests/security`) |
| Unauthorized or unapproved external-write path | `tests/security/test_external_write_paths.py`: only reviewed modules may reach external systems, the only non-GET requests are the reviewed Azure DevOps calls, the work-item create has exactly one caller (the approval-gated `ExecutionService`), and Microsoft Graph access is `Mail.Read` only (no send/reply/move capability) |

## 3. Vulnerability severity policy

| Source | Blocks | Reported, not blocking | Triage |
|---|---|---|---|
| Python packages (pip-audit, PyPI/OSV advisories) | every known vulnerability: advisories carry no reliable severity, so all block | — | fix or upgrade; otherwise register an exception |
| npm packages (`npm audit`) | high, critical | moderate, low (listed in the log and artifact) | moderate within 30 days, low at the next dependency update |
| Container images (Trivy) | HIGH, CRITICAL with an available fix; any secret | every other finding, including HIGH without a fix, in `trivy-<image>.json` and the job summary | rebuild when a fix appears (the gate starts failing on its own) |
| semgrep, bandit, gitleaks, Trivy config, actionlint, zizmor | every finding | — | fix, or register an exception for a verified false positive |

## 4. Exceptions: never silent

A scanner result is never suppressed silently. Accepted exceptions live in
[`security-exceptions.toml`](../security-exceptions.toml), and each entry records:

* the scanner, rule and scope;
* the suppression mechanism;
* an owner;
* the justification and compensating controls;
* an expiry date.

`scripts/check_security_exceptions.py` (in `pipeline-config`) scans every tracked file and fails when:

* a suppression marker cites no registered id. Markers are inline bandit, semgrep, gitleaks and Trivy
  ignore comments, ruff `S…` noqa codes, scanner flags such as `--ignore-unfixed`, `--ignore-vuln` and
  `--exclude-rule`, `continue-on-error: true`, `.gitleaks.toml` allowlists, and scanner ignore files.
  The id goes on the marker's line or on the comment line directly above it;
* a marker cites an unknown id;
* an entry is incomplete, cited nowhere, or **expired**. Entries expiring within 14 days are warned about.

Suppressed findings stay visible. Semgrep's SARIF artifact lists them as `inSource` suppressions, and
Trivy's full report includes the unfixed vulnerabilities that the gate skips.

| Id | Scanner / rule | Scope | Owner | Expires |
|---|---|---|---|---|
| SEC-EX-001 | Trivy: HIGH OS-package CVEs **without a published fix** (F-10) | backend base image `python:3.14-slim-trixie` (44 HIGH at the last scan) | Platform owner | 2026-12-31 |
| SEC-EX-002 | bandit B608 / ruff S608 (false positive: WIQL, escaped and format-checked, not SQL) | `AzureDevOpsClient.find_by_idempotency_tag` | Backend lead | 2027-10-31 |
| SEC-EX-003 | semgrep `unverified-jwt-decode` | `IdTokenValidator.validate` reads `tid` before verification to select the tenant's keys; only verified claims are returned (tested against tokens edited after signing) | Backend lead | 2027-10-31 |
| SEC-EX-004 | semgrep `uv-missing-dependency-cooldown` (F-21) | `[tool.uv]`: a 7-day `exclude-newer` today would force 27 packages back, including FastAPI 0.162 → 0.143. Dependabot's cooldown applies meanwhile | Backend lead | **2026-11-02**: enable `exclude-newer = "7 days"` |
| SEC-EX-005 | gitleaks `jwt`, `generic-api-key` | 4 synthetic test fixtures, each allowlisted by rule + file + exact line | QA lead | 2027-10-31 |

The owners are roles; assign named people before production. To add an exception:

1. Confirm the finding (reproduce it, read the code).
2. Prefer a fix.
3. Otherwise add an entry to the register and cite its id at the suppression.
4. Get the change reviewed.

Renewing an entry needs a fresh review of the finding, not just a new date. **Never allowlist a real
credential**: rotate and revoke it at the provider (deleting it from the file does not remove it from
history), then purge it from history if needed.

## 5. Secure pipeline practices

* **Least privilege, short-lived credentials.** The workflow token is read-only (`permissions: contents:
  read` at the top; no job asks for write scopes, which a test enforces). There are no repository
  secrets; the only credential is the job-scoped `GITHUB_TOKEN`, which expires with the job. A future
  deployment job must authenticate with OIDC federation, not stored cloud keys.
* **Protected environments and production secrets.** CI deploys nothing and holds no production secrets.
  A future deploy or sandbox job must use a GitHub Environment with required reviewers and
  environment-scoped secrets. It must also run only on `main` or `workflow_dispatch`, never on pull
  requests. The configuration tests fail when `secrets.` appears in a workflow, which forces that review.
* **Pinned, reviewed dependencies and actions:**
  * actions are pinned to full commit SHAs, scanner images to `tag@sha256` digests
    (`scripts/security-scan.sh`), and base and service images to digests (Dockerfiles, Compose, the CI
    PostgreSQL service);
  * Python installs are hash-verified from `uv.lock` (`uv sync --locked`) and npm installs come from
    `package-lock.json` (`npm ci`);
  * Dependabot proposes updates only after a 7-day cooldown (14 days for majors); npm resolves only
    releases at least 7 days old (`frontend/.npmrc`);
  * npm never runs dependency install scripts (`ignore-scripts=true`), including in the image build.
* **Untrusted pull-request code.** The workflow uses `pull_request` and never `pull_request_target` or
  `workflow_run`, so fork code runs without secrets and with a read-only token. Checkouts do not persist
  the token. Untrusted text (the PR description) reaches scripts only through environment variables, and
  a test and zizmor reject `${{ github.event.* }}` inside `run:`.
* **Separate environments.** CI tests use throwaway service containers (PostgreSQL), SQLite files, mock
  providers and synthetic data only. No job can reach staging or production. The opt-in E2E job runs an
  isolated Compose project with mock integrations.
* **Software inventories.** CycloneDX SBOMs are produced for the Python runtime and all groups, npm
  runtime dependencies, and both container images (artifacts `dependency-audit-and-sbom` and
  `containers-trivy`).
* **Useful logs without secrets or personal data.** gitleaks runs with `--redact`; jobs never print the
  environment; the application redacts tokens, secrets and email bodies from logs and audit records
  (tests in `tests/unit/observability`, `tests/unit/services/test_audit.py`); tests use synthetic data.

## 6. Branch protection (repository setting — not applied)

The gates block merges only once `CI gate` is a required status check. Repository settings are outside
the repository and were **not changed**. To apply them (repository admin):

```bash
gh api -X PUT repos/bikashpatel/nuvexa-autotask-ai-agent/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": {"strict": true, "contexts": ["CI gate"]},
  "enforce_admins": true,
  "required_pull_request_reviews": {"required_approving_review_count": 1, "dismiss_stale_reviews": true},
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

Also recommended:

* enable secret-scanning push protection and private vulnerability reporting under *Settings → Code
  security*;
* restrict Actions to the pinned actions (*Settings → Actions → Allow specified actions*).

## 7. Reproducing the jobs locally

Requirements: Docker, uv 0.12.1, Node 24 / npm ≥ 11.10, git.

| Job | Command (repository root) |
|---|---|
| `backend` | `uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run mypy app` then, against a throwaway PostgreSQL (`docker run -d --name nuvexa-pg-test -p 127.0.0.1:55433:5432 -e POSTGRES_USER=nuvexa -e POSTGRES_PASSWORD=nuvexa -e POSTGRES_DB=nuvexa_test postgres:18-alpine`): `export TEST_DATABASE_URL=postgresql+psycopg://nuvexa:nuvexa@127.0.0.1:55433/nuvexa_test; for layer in unit integration security evaluation; do uv run coverage run --append -m pytest -m $layer \|\| break; done; uv run coverage report` (delete `.coverage` first). **Never point `TEST_DATABASE_URL` at a database you care about: the tests drop its tables.** |
| `backend-sqlite` | `uv run pytest --cov=app` |
| `frontend` | `cd frontend && npm ci && npm run format:check && npm run lint && npm run typecheck && npm run test:coverage && npm run build` |
| `dependency-audit` | `scripts/security-scan.sh deps && scripts/security-scan.sh sbom` |
| `secret-scan` | `scripts/security-scan.sh secrets` |
| `sast` | `scripts/security-scan.sh sast` |
| `containers` | `IMAGE_TAG=local scripts/security-scan.sh images` (scans `nuvexa-backend:local` / `nuvexa-frontend:local`, building them if missing) |
| `pipeline-config` | `scripts/security-scan.sh config` (zizmor runs offline unless `GH_TOKEN` is set) |
| all scanners | `scripts/security-scan.sh all` (runs every stage and lists the failed ones) |
| `e2e` | `scripts/e2e.sh` |
| exception register only | `git ls-files -z \| python3 scripts/check_security_exceptions.py` |

CI runs the same `scripts/security-scan.sh` stages, so a local run uses the same scanner versions and
flags. Reports are written to `reports/security/` (ignored by git). Registry-based semgrep rules and the
Trivy vulnerability database are fetched at run time, so a later run can find issues an earlier run did
not.

## 8. Evidence of runs

| Date | Where | Commit | Result |
|---|---|---|---|
| 2026-10-25 | local (macOS arm64) | working tree before the Prompt 4 commit | see the table below |
| 2026-10-25 | GitHub Actions | Prompt 4 commit | recorded in docs/TESTING_STRATEGY.md §8 after the run |

| Gate | Local result (2026-10-25) |
|---|---|
| Backend, SQLite, coverage gate | 3,143 passed, 12 skipped, 5 strict xfail; 99.57 % line + branch |
| Backend, PostgreSQL 18, layered as in CI | unit 2,659 · integration 308 · security 138 · evaluation 44 passed (2 skipped, 5 strict xfail); combined coverage 99.61 % |
| Frontend | fresh `npm ci` (install scripts disabled), format, lint, typecheck; 1,167 tests; 99.93 % statements, 98.55 % branches, 100 % functions and lines; build |
| pip-audit (136 locked packages, hashes required) | no known vulnerabilities |
| npm audit / signatures | 0 vulnerabilities; 294 packages with verified registry signatures, 123 with verified attestations |
| gitleaks (8 commits) | no leaks (the 4 synthetic fixtures are allowlisted precisely; planted secrets in the same files are still caught) |
| semgrep (315 rules, 287 files) / bandit | 0 findings (2 cited `inSource` suppressions in SARIF) / 0 issues |
| Trivy images | backend: 0 fixable; 44 HIGH, 58 MEDIUM, 61 LOW, 1 UNKNOWN without a fix (SEC-EX-001). Frontend: 0 |
| Trivy config, actionlint, zizmor (offline), exception register | clean; 8 suppressions, 5 exceptions, SEC-EX-004 expiry warning |

## 9. What the gates do not cover

* Live Microsoft Entra, Microsoft Graph, Azure DevOps and LLM providers: tests use faithful fakes only.
  A sandbox job would need sandbox credentials in a protected environment (§5); none exists yet.
* Dynamic testing (DAST, e.g. OWASP ZAP), penetration testing, load testing and performance.
* Repository settings (§6) and the runtime cloud environment (none has been created).
* Semgrep parses about 99.9 % of lines. It does not understand Python 3.14's unparenthesized
  `except A, B:` (PEP 758), which ruff's formatter enforces, so those few clauses are only partially
  analysed. Bandit and ruff's `S` rules use CPython's own parser and cover them.
* Trivy does not parse Docker Compose files. `docker-compose.yml` is covered by the configuration tests
  instead: digest-pinned images, and the demo stack binds only to localhost.
* Scanners only know about published advisories and their rule sets: a clean scan does not mean there
  is no vulnerability.
