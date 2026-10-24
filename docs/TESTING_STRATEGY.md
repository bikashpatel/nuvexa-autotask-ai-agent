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
| Unit | `tests/unit/` | `uv run pytest -m unit` | 2,615 | ~60 s | Each module in isolation (services, workflow nodes, adapters with respx, agents, schemas, state machine) |
| Integration | `tests/integration/` | `uv run pytest -m integration` | 308 | ~60 s | Real API → services → LangGraph → worker → DB in-process; every endpoint, RBAC matrix, OAuth flows, REST adapters against mocked HTTP |
| Security | `tests/security/` | `uv run pytest -m security` | 78 | ~5 s | Security invariants and regression tests for every audit finding |
| Evaluation | `tests/evaluation/` | `uv run pytest -m evaluation` | 44 | ~5 s | Offline AI baseline on the labelled synthetic dataset |
| PostgreSQL | same files | `TEST_DATABASE_URL=postgresql+psycopg://… uv run pytest` | all + `postgres`-marked concurrency tests | ~5–9 min | Triggers, row locks, `SKIP LOCKED`, partial unique indexes, timeouts |
| Frontend | `frontend/src/**/*.test.ts(x)` | `npm test` / `npm run test:coverage` | 1,167 | ~20 s | Every component/page/hook/API function; a11y (axe); no optimistic writes; XSS inertness |
| System E2E | `e2e/` | `scripts/e2e.sh` | 37 | ~1–2 min + build | Real containers (nginx, API, worker, PostgreSQL), restarts, DB invariants, Playwright UI |

The default `uv run pytest` runs unit + integration + security + evaluation on SQLite with no network and
no paid calls. E2E needs Docker and is run separately (and in the opt-in CI job, see the CI/CD gates work).

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
| Frontend coverage gate | `frontend/vite.config.ts` thresholds | statements 99, branches 98, functions 100, lines 100 |
| TDD guard | `scripts/check_test_changes.py`, CI job `tdd-guard` (pull requests) | Fails a PR that changes `app/**.py` or `frontend/src/**.ts(x)` without changing any test. Exception: `TDD-Exempt: <reason>` in the PR description, printed in the log |
| PR template | `.github/pull_request_template.md` | RED/GREEN evidence, commands and unverified behaviour recorded per change |
| Layer selection | `tests/conftest.py` + registered markers (`--strict-markers`) | Fast feedback loops per layer |
| Static checks | ruff (lint + format), mypy strict, ESLint, `tsc` | Required in CI |

## 5. Mutation testing

Coverage shows code ran; mutation testing shows tests would **notice** if it were wrong. `mutmut` mutates
the security-critical modules and runs the relevant unit and security tests against each mutant:

```bash
uv run --with mutmut mutmut run --max-children 8   # config: [tool.mutmut] in pyproject.toml
uv run --with mutmut mutmut results                 # list surviving mutants
uv run --with mutmut mutmut show <mutant-name>      # inspect one
```

Scope: `state_machine`, `authz`, `approval_service`, `execution_service`, `action_policy`, `id_token`.

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

| Measure | Result |
|---|---|
| Backend tests, SQLite | 3,028 passed, 12 skipped, 5 strict xfail (English-only injection screener limits) |
| Backend tests, PostgreSQL 18 | 2,991 passed, 1 skipped, 5 xfail |
| Backend coverage (line + branch) | 99.57 % |
| Frontend | 1,167 tests; 99.93 % statements, 98.55 % branches, 100 % functions and lines |
| System E2E | 37 passed |
| Mutation testing, first run | 2,492 mutants: 2,077 killed, 414 survived, 1 untested — 83.4 %; `state_machine` and `authz` 100 % |
| Mutation testing, after hardening | 2,180 killed + 7 timeouts (detected), 304 survived, 1 untested — **87.8 %**. Remaining survivors: message/log wording (accepted), database-lock mutants (PostgreSQL concurrency tests), unreachable defaults |
