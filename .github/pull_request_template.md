## What and why

<!-- The requirement or defect this change addresses. Link the issue. -->

## Test-driven evidence (docs/TESTING_STRATEGY.md)

- **RED** — test(s) written first and the failure observed for the intended reason:
  <!-- e.g. tests/security/test_rate_limiting.py::test_random_session_cookies… failed: "login limit never applied" -->
- **GREEN** — smallest change that makes them pass:
- **Regression suite** — commands run and results:
  - [ ] `uv run pytest --cov=app` (coverage gate) — result:
  - [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy app`
  - [ ] Frontend (if touched): `npm run lint && npm run typecheck && npm run test:coverage && npm run build`
  - [ ] PostgreSQL (if persistence/concurrency changed): `TEST_DATABASE_URL=… uv run pytest`
  - [ ] System E2E (if behaviour across containers changed): `scripts/e2e.sh`

## Checklist

- [ ] New logic has positive **and** negative assertions (invalid input, failures, authorization denials)
- [ ] Security-sensitive changes include negative authorization tests in `tests/security/`
- [ ] Every confirmed bug has a regression test that failed before the fix
- [ ] No assertions weakened, tests skipped, files excluded or thresholds lowered to make CI pass
- [ ] Docs/config updated where behaviour or settings changed
- [ ] Unverified behaviour (e.g. live Microsoft/LLM calls) is stated below

## Unverified / risks

<!-- Anything not exercised by tests, and why. -->

<!-- If production code changes without test changes are genuinely justified, add a line:
TDD-Exempt: <reason>
The CI TDD guard prints the reason for reviewers. -->
