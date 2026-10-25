# Testing strategy

NUVEXA is developed **test-first**. Every feature, bug fix, refactor, security change and integration
follows Red → Green → Refactor, and a change is only "done" when the evidence below exists. This document
is the reference for how tests are organized, written, run and enforced.

## 1. The mandatory cycle

1. Understand the requirement; read the existing code and tests.
2. Write down expected behaviour, invariants, boundaries and failure cases.
3. **RED** — write or update tests *before* production code; run them and confirm they fail **for the
   intended reason** (an assertion about the missing behaviour, not an import typo).
4. **GREEN** — implement the smallest change that makes them pass.
5. **Refactor** with the tests green.
6. Run focused tests → affected suites → full regression suite.
7. Run formatting, linting, type checks and coverage.
8. Record what ran and what did not (pull request template).

Never skip RED for ordinary work. If a test cannot run, say so; never report a test as passing unless its
run was observed. When an existing test already fails before a change, record that baseline first.

## 2. Test layers

The layer marker is applied automatically from the directory (`tests/conftest.py`), so every test belongs to
exactly one layer and layers can be run independently.

| Layer | Location | Select with | Tests (2026-10-25) | Runtime | What it proves |
|---|---|---|---|---|---|
| Unit | `tests/unit/` | `uv run pytest -m unit` | 3,223 | ~60 s | Each module in isolation (services, workflow nodes, adapters with respx, agents, schemas, state machine) |
| Integration | `tests/integration/` | `uv run pytest -m integration` | 310 | ~60 s | Real API → services → LangGraph → worker → DB in-process; every endpoint, RBAC matrix, OAuth flows, REST adapters against mocked HTTP |
| Security | `tests/security/` | `uv run pytest -m security` | 446 | ~40 s | Security invariants, regression tests for every audit finding, release gates (egress inventory / single write path, supply-chain configuration, CI pipeline contract) and the adversarial suites (`test_adversarial_*.py`) |
| Evaluation | `tests/evaluation/` | `uv run pytest -m evaluation` | 44 | ~5 s | Offline AI baseline on the labelled synthetic dataset |
| PostgreSQL | same files | `TEST_DATABASE_URL=postgresql+psycopg://… uv run pytest` | all + `postgres`-marked concurrency tests | ~5–9 min | Triggers, row locks, `SKIP LOCKED`, partial unique indexes, timeouts |
| Frontend | `frontend/src/**/*.test.ts(x)` | `npm test` / `npm run test:coverage` | 1,329 | ~20 s | Every component/page/hook/API function; a11y (axe); no optimistic writes; XSS inertness |
| System E2E | `e2e/` | `scripts/e2e.sh` | 41 | ~1–2 min + build | Real containers (nginx, API, worker, PostgreSQL), restarts, DB invariants, Playwright UI |

The default `uv run pytest` runs unit + integration + security + evaluation on SQLite with no network and
no paid calls. E2E needs Docker and is run separately (and in the opt-in CI `e2e` job).

CI runs the four layers as separate steps on PostgreSQL 18 (`coverage run --append -m pytest -m <layer>`, one
JUnit report each), then enforces the combined coverage gate; a second job runs the whole suite on SQLite.
Collection fails for a test outside the four layer directories, so no test can fall between the steps.

## 3. Writing tests

* **Arrange–Act–Assert**, one behaviour per test, names that state the behaviour
  (`test_rejected_action_never_executes_via_service`).
* **Observable behaviour**, not private details: HTTP responses, persisted rows, audit events, state
  transitions, external requests made. Private attributes are touched only to verify configuration wiring.
* **Positive and negative cases**: invalid input, boundaries (exact limits, leeway edges), exceptions,
  timeouts, retries, concurrency and authorization denials.
* **Determinism**: injected clocks (`IdTokenValidator(clock=…)`, `HttpJwksFetcher(clock=…)`), fixed
  hypothesis seeds where needed, `sleep` injected into retry code, fake timers in the frontend, no real
  network (respx / fetch mocks), no live LLM (heuristic agent, scripted fakes).
* **Fakes at third-party boundaries only**: `FakeEntra` (MSAL), signed test ID tokens
  (`tests/support/entra_tokens.py`), `MockAzureDevOpsClient` (shares the production create-at-most-once
  engine), respx for Graph/ADO HTTP. The application's own classes are exercised for real. Avoid mocks that
  let a test pass while the real implementation is broken.
* **Property-based tests** (hypothesis) for invariants: state-machine transitions, payload hashing,
  delimiter safety, redirect safety, validation oracles, idempotent-create sequences.
* **Contract tests**: `docs/api.md` ↔ routes (`test_api_contract.py`), frontend enums ↔ backend enums
  (`frontend/src/api/contract.test.ts`), `.env.example` ↔ `Settings`.
* **Every confirmed bug gets a regression test** that failed before the fix.
* **Security-sensitive changes** need negative authorization tests in `tests/security/`.
* Do not weaken assertions, skip failing tests, exclude difficult files or lower thresholds to make CI
  pass. Strict `xfail` is allowed only to document a known, accepted limitation (it fails when fixed).

## 4. Enforcement

| Mechanism | Where | Effect |
|---|---|---|
| Backend coverage gate | `[tool.coverage.report] fail_under = 99` (line + branch, `pyproject.toml`) | `pytest --cov=app` fails below 99 % (measured 99.57 %) |
| Frontend coverage gate | `frontend/vite.config.ts` thresholds, `npm run test:coverage` in CI | statements 99, branches 98, functions 100, lines 100 |
| TDD guard | `scripts/check_test_changes.py`, CI job `tdd-guard` (pull requests) | Fails a PR that changes `app/**.py` or `frontend/src/**.ts(x)` without changing any test. Exception: `TDD-Exempt: <reason>` in the PR description, printed in the log |
| PR template | `.github/pull_request_template.md` | RED/GREEN evidence, commands and unverified behaviour recorded per change |
| Layer selection | `tests/conftest.py` + registered markers (`--strict-markers`) | Fast feedback loops per layer |
| Static checks | ruff (lint + format), mypy strict, Prettier, ESLint, `tsc` | Required in CI |
| CI release gates | `.github/workflows/ci.yml`, `scripts/security-scan.sh`, single required check `CI gate` | Dependency, secret, SAST, image and configuration scans block merges (docs/CI_SECURITY_GATES.md) |
| Pipeline contract | `tests/security/test_ci_pipeline_contract.py` | Fails when a stage is removed, a scanner/test soft-fails, a job leaves the gate, or a coverage threshold is lowered |
| Exception register | `security-exceptions.toml`, `scripts/check_security_exceptions.py` | Every scanner suppression cites an owned, justified, expiring exception |

## 5. Mutation testing

Coverage shows code ran; mutation testing shows tests would **notice** if it were wrong. `mutmut` mutates
the security-critical modules and runs the relevant unit and security tests against each mutant:

```bash
uv run --with mutmut mutmut run --max-children 8   # config: [tool.mutmut] in pyproject.toml
uv run --with mutmut mutmut results                 # list surviving mutants
uv run --with mutmut mutmut show <mutant-name>      # inspect one
```

Scope: `state_machine`, `authz`, `approval_service`, `execution_service`, `action_policy`, `id_token`.
Mutants run against `tests/unit` and `tests/security`, except the tests of repository configuration (CI
workflow, Dockerfiles, nginx, git-tracked files — absent from mutmut's copy of the tree) and the source-level
egress gate (it would read mutmut's generated function copies as new call sites); none of them exercises the
mutated modules' behaviour (`[tool.mutmut]` in `pyproject.toml`).

Triage rules for survivors:

* **Kill** logic mutants (changed conditions, removed query filters, flipped flags, boundary operators).
  These revealed real gaps on the first run: queries could lose their `action_id` filter, the execution
  claim could lose its status guard, audit events could lose their attribution, the JWKS fetcher could
  follow redirects or share a cache across tenants, and a duplicate tool call could drop later proposals.
  `tests/unit/services/test_cross_record_isolation.py` and the mutation-hardening section of
  `tests/security/test_id_token_validation.py` now pin them down.
* **Accept as equivalent** mutants that only change human-readable messages or log fields (tests
  deliberately assert error types/codes, not wording) and mutants unobservable by design (e.g. a default
  that cannot be reached because settings validation guarantees a complete mapping).
* **Database-specific** mutants (e.g. removing `for_update=True`) cannot be killed on SQLite; they are
  covered by the PostgreSQL concurrency tests (`tests/integration/test_postgres_concurrency.py`).

Results are recorded in §8. Run mutation testing before releases and after changes to these modules.

## 5a. Adversarial testing

Before a release, independent testers try to break the application rather than confirm it
(`tests/security/test_adversarial_*.py`, `frontend/src/test/adversarial-*.test.tsx`): malicious email and a
fully compromised model, forged/stale/replayed approvals, payload tampering, concurrency and ambiguous
writes, cross-tenant access and session abuse, hostile and oversized input, database failures and crashes
mid-workflow, provider outages, secret leakage into logs/spans/checkpoints/API responses, XSS and UI-state
bugs. A test that exposes a defect asserts the correct behaviour and is marked `# DEFECT:` (never xfail);
the fix is made test-first and the comment becomes `# Regression (Prompt 5): …`. Results and the defect
list: docs/RELEASE_READINESS_REPORT.md.

## 6. Flaky tests

A test that fails intermittently is a defect. Reproduce it (repeat runs, load, fixed seeds), find the
non-determinism (time, ordering, shared state, real concurrency), fix it, and keep the test. Never
"retry until green" in CI and never silently skip. Observed once: a frontend test failed while two full
suites ran concurrently on one machine; it did not reproduce in four subsequent runs — tracked as an open
item.

## 7. What automated tests do not verify

* Live Microsoft Entra sign-in, Microsoft Graph mailboxes, Azure DevOps organizations and LLM providers.
  These are covered by faithful fakes only. Live checks require sandbox credentials and are opt-in
  (`@pytest.mark.live_llm`, the opt-in sandbox E2E job).
* Visual regressions, real-browser performance, load and external penetration testing.

## 8. Baseline and results (2026-10-25)

Scanner and CI-gate results: docs/CI_SECURITY_GATES.md §8.


| Measure | Result |
|---|---|
| Backend tests, SQLite (release review, `a73afd7`) | 4,006 passed, 12 skipped, 5 strict xfail (English-only injection screener limits); coverage 99.69 % |
| Backend tests, PostgreSQL 18 (release review) | see docs/RELEASE_READINESS_REPORT.md §2 |
| Backend coverage gate | 99 % line + branch (`fail_under`) |
| CI, Prompt 2 commit `dad3218` (run 40144764111) | PostgreSQL job: 3,038 passed, 2 skipped, 5 xfailed; coverage 99.61 %; all jobs green |
| CI with the release gates, `aaef287` (runs 40148542307 and 40149198842) | every gate green; PostgreSQL layers 2,663 / 308 / 138 / 44 passed; SQLite 3,143 passed (99.57 %); frontend 1,167; system E2E on the runner 37 passed |
| Frontend (release review) | 1,329 tests; 99.93 % statements, 98.62 % branches, 100 % functions and lines |
| Mutation testing, release review (Linux) | 2,667 mutants: 2,342 killed, 305 survived, 19 inconclusive (process crashed), 1 untested — **88.4 %** of conclusive mutants (87.8 % counting crashes as undetected); triage in docs/RELEASE_READINESS_REPORT.md §2.3 |
| Adversarial review (Prompt 5) | 5 independent suites; 49 tests failed on first run (35 defects; P5-36 and P5-37 came from follow-up checks), all fixed test-first — docs/RELEASE_READINESS_REPORT.md §6 |
| Mutation testing, first run | 2,492 mutants: 2,077 killed, 414 survived, 1 untested — 83.4 %; `state_machine` and `authz` 100 % |
| Mutation testing, after hardening | 2,180 killed + 7 timeouts (detected), 304 survived, 1 untested — **87.8 %**. Remaining survivors: message/log wording (accepted), database-lock mutants (PostgreSQL concurrency tests), unreachable defaults |
