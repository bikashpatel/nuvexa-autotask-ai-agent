# CLAUDE.md — NUVEXA-AutoTask-AI-Agent

Agentic email-to-work automation (Elarxsys). Python 3.14 / FastAPI / LangGraph backend in `app/`,
React + TypeScript + Mantine dashboard in `frontend/`. Read `docs/architecture.md` and
`docs/security-model.md` before structural changes.

## Commands

```bash
uv sync                                   # install (uses uv.lock)
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run pytest                             # SQLite, mocks, heuristic agent — no network, no paid calls
uv run pytest tests/security              # security regression suite
scripts/e2e.sh                            # system E2E against an isolated Docker Compose stack
TEST_DATABASE_URL=postgresql+psycopg://nuvexa:nuvexa@localhost:5432/nuvexa_test uv run pytest  # PostgreSQL
uv run alembic upgrade head
uv run nuvexa api | worker | demo         # see app/cli.py
uv run python -m app.evaluation.run --provider heuristic
cd frontend && npm ci && npm run lint && npm run typecheck && npm test && npm run build
docker compose up --build
```

## Layout

`app/api` thin routes · `app/services` business logic · `app/workflows` LangGraph graph ·
`app/agents` LLM providers/prompts/injection screening · `app/integrations/{microsoft_graph,azure_devops}`
adapters · `app/repositories` tenant-scoped queries · `app/workers` job queue ·
`app/domain` enums + state machine · `app/schemas` Pydantic contracts · `app/db` models + migrations.

## Critical invariants (never break these)

1. **No external write without a current approval.** Only `ExecutionService.execute` performs
   writes; it re-checks RBAC, approval `(version, payload_hash)`, payload validity and idempotency.
2. **Approval is bound to the payload.** Editing bumps `version` and invalidates approvals.
3. **The LLM is never a security boundary.** Model output is validated (Pydantic + `action_policy`);
   it cannot choose URLs, methods, headers, credentials, organization, or unlisted projects/fields.
   LangGraph resume payloads are requests, not authorizations.
4. **Email content is untrusted data.** Keep it in the delimited data block; never execute it; never
   render it as HTML; never log bodies.
5. **Tenant isolation.** Every query filters by `tenant_id` (and owner for non-privileged roles).
   Return 404 for invisible resources.
6. **Idempotency.** Unique provider message ids; one active run per email; idempotency key per
   proposal; at most one succeeded execution per action; ADO tag lookup before create and after
   ambiguous failures. Never blindly retry a write that may have succeeded.
7. **Secrets.** Tokens are encrypted (`SecretBox`) in `credential_store`, never returned to the
   frontend, never logged. No real secrets in code, fixtures or docs.
8. **No chain-of-thought storage.** Store concise rationales, evidence quotes and operational traces only.
9. **Every status change goes through `app/domain/state_machine.py`** and emits an audit event for
   approvals, rejections, permission failures and external writes.
10. **Audit events are append-only and HMAC-signed.** Only `record_audit` creates them; never update or
    delete rows (PostgreSQL triggers enforce it; retention purge is the only sanctioned delete).
11. **Trust only verified identity claims.** Sign-in uses `IdTokenValidator` (signature, iss, aud, exp,
    tenant, nonce); never read MSAL's unverified `id_token_claims`.
12. **TDD:** write the failing test first (see `docs/TESTING_STRATEGY.md`); security changes need negative
    authorization tests in `tests/security/`.
13. **One external write path.** Only reviewed modules reach external systems and the only write is the
    approval-gated work-item create (`tests/security/test_external_write_paths.py`). A new egress, a
    non-GET request or a mail-write scope needs a security review and explicit authorization.
14. **No silent scanner suppressions.** Every `nosec`/`nosemgrep`/ruff `S…` noqa/allowlist/ignore flag
    cites a `SEC-EX-NNN` entry in `security-exceptions.toml` (owner, justification, controls, expiry);
    never weaken a CI gate or threshold (`docs/CI_SECURITY_GATES.md`).

## Conventions

* Async SQLAlchemy 2.x with typed `Mapped[...]`; UTC-aware datetimes (`app.core.timeutil.utcnow`).
* Schema changes require an Alembic migration (`uv run alembic revision --autogenerate -m "..."`,
  review the output; keep partial indexes for both PostgreSQL and SQLite).
* Raise `app.core.errors.*` from services; routes stay thin.
* Tests: deterministic fixtures, `respx` for HTTP, heuristic agent for LLM. Mark paid model calls
  `@pytest.mark.live_llm` (excluded by default).
* Don't claim an integration works unless it was exercised; mocks are labelled as mocks.
