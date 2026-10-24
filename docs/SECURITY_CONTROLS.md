# Security controls

Each control lists where it is implemented and which automated tests enforce it. Findings and evidence
are in `docs/SECURITY_AUDIT.md`; the threat model is in `docs/security-model.md`.

## Authentication and sessions

| Control | Implementation | Tests |
|---|---|---|
| Every non-public endpoint requires a session | `app/api/deps.py` (`get_resolved_session`, `ActorDep`) | `tests/integration/test_api_contract.py` (every documented route without a session → 401), `tests/integration/test_rbac_matrix.py` |
| Opaque session tokens, stored as SHA-256, absolute + idle expiry, revocation | `app/services/auth_service.py` `SessionService` | `tests/integration/test_auth_flows.py`, `tests/unit/services/test_auth_service.py` |
| Cookies `HttpOnly`, `SameSite=Lax`, `Secure` outside development | `app/api/routes/auth.py` | `test_auth_flows.py::test_demo_login_sets_hardened_cookies`, `test_cookies_are_secure_when_configured` |
| CSRF token bound to the session (hash compared in constant time) on every unsafe method | `deps.py`, `SessionService.csrf_valid` | `tests/unit/api/test_deps.py`, `test_auth_flows.py::test_csrf_token_is_bound_to_its_session` |
| Session rotation on login (old session revoked) | `routes/auth.py`, `SessionService.revoke_token` | `tests/security/test_session_and_attribution.py` |
| OAuth 2.0 auth-code + PKCE; state stored server-side, single use, 10-min expiry, bound to a browser cookie | `EntraAuthService`, `OAuthState` | `test_auth_flows.py` (replay, wrong binding, expiry) |
| ID token validated: RS256 signature (tenant JWKS, rotation), issuer, audience, `exp`/`nbf`/`iat`, required claims, tenant allow-list, nonce | `app/integrations/microsoft_graph/id_token.py`, `oauth.py` | `tests/security/test_id_token_validation.py` (28 tests incl. wrong audience/tenant/issuer, expired, alg confusion, unknown key) |
| Multi-tenant authority requires an explicit tenant allow-list | `EntraAuthService._check_login_policy` | `test_auth_flows.py::test_multi_tenant_authority_without_allow_list_is_refused` |
| Open-redirect protection (relative paths only, `\Z` anchored) | `safe_redirect_path` | `test_auth_flows.py::test_post_login_redirect_is_always_same_site`, hypothesis tests in `tests/unit/services/test_auth_service.py` |
| Demo login only when `DEMO_MODE` and not production | `Settings.demo_login_enabled` | `tests/unit/services/test_auth_service.py` |

## Authorization and tenant isolation

| Control | Implementation | Tests |
|---|---|---|
| Role → permission matrix (viewer, member, approver, admin) | `app/services/authz.py` | `tests/unit/services/test_authz.py`, `tests/integration/test_rbac_matrix.py` (87 cases) |
| Every query scoped to tenant; non-privileged users also to owner; invisible ⇒ 404 | `app/repositories/scoping.py` | `tests/unit/db/test_scoping.py`, `tests/integration/test_security.py::test_cross_tenant_access_is_invisible`, RBAC matrix |
| Denials audited in a separate transaction | `record_permission_denied` | `tests/unit/services/test_audit.py`, `test_security.py` |
| Service credentials (Azure DevOps PAT) usable only by members and above | `routes/integrations.py` | `tests/security/test_hardening_regressions.py::test_viewers_cannot_use_the_azure_devops_service_credentials` |
| Identity, roles, tenant and approval state never taken from the client | server-side session → `Actor`; approval rows in DB | RBAC matrix; forged-resume test below |

## Human approval and execution safety

| Control | Implementation | Tests |
|---|---|---|
| Only `ExecutionService.execute` performs writes | `app/services/execution_service.py` | `tests/unit/services/test_execution_service.py` |
| Approval bound to `(action_id, version, payload_hash)`; approve/execute must present the version/hash the reviewer saw | `ApprovalService.approve`, `ExecutionService.preflight/execute` | `test_end_to_end.py::test_edit_invalidates_approval_and_requires_reapproval`, `test_workflow_lifecycle.py` |
| Edit ⇒ new version, approvals invalidated | `ApprovalService.update` | same + `tests/unit/services/test_approval_service.py` |
| Payload integrity re-verified at execution (recomputed hash) | `execute()` | `test_security.py::test_tampered_payload_is_refused` |
| Pending/rejected/cancelled/superseded never execute; terminal states final | `app/domain/state_machine.py` (every transition pair tested) | `tests/unit/domain/test_state_machine.py` (250 pairs), `test_security.py` |
| LangGraph resume payloads are requests, not authorizations | graph `execute_action` → `ExecutionService` | `test_security.py::test_forged_graph_resume_cannot_execute_unapproved_action` |
| TOCTOU / concurrent execution: row lock + conditional `UPDATE … WHERE status AND version` + partial unique indexes | `execute()`, `uq_action_executions_one_*` | `tests/integration/test_postgres_concurrency.py` |
| Idempotency: provider message ids, one active run per email, proposal keys, ADO tag lookup before create and after ambiguous failures, never blind retries | models + `azure_devops/idempotency.py` | `test_idempotency_state_machine.py` (20,000+ sequences), `test_failures.py` |
| Fail closed: missing approval ⇒ 403 + audit; unknown outcome ⇒ `uncertain` until reconciled | `execute()`, `_reconcile_locked` | `test_failures.py`, `test_resilience.py` |

## Audit trail

| Control | Implementation | Tests |
|---|---|---|
| Approvals, rejections, edits, denials, external writes, logins, syncs audited | `record_audit` call sites | `test_end_to_end.py`, `test_workflow_lifecycle.py` (audit completeness) |
| Append-only in PostgreSQL (UPDATE/DELETE/TRUNCATE blocked; delete only during retention purge) | triggers in `app/db/models.py` + migration `7c3e2a9b41d0` | `tests/security/test_audit_integrity.py` (PostgreSQL) |
| Tamper evidence: per-event HMAC-SHA256 with `AUDIT_HMAC_KEY` (required in production); admin verification endpoint | `app/services/audit.py`, `GET /api/v1/audit-events/integrity` | `test_audit_integrity.py` |
| Login events record the client IP; IP cannot be spoofed through the proxy | `routes/auth.py`, nginx `X-Forwarded-For $remote_addr` | `test_session_and_attribution.py`, `e2e/test_resilience_system.py::test_forwarded_for_header_cannot_spoof_the_client_ip` |

## AI and prompt-injection defences

| Control | Implementation | Tests |
|---|---|---|
| Email content isolated in a nonce-delimited data block; delimiter look-alikes neutralized | `app/agents/prompts.py` | `tests/unit/agents/test_prompts_properties.py` (hypothesis: forged delimiters impossible) |
| Deterministic injection screener (11 rules, flags shown to reviewers, lowers confidence) | `app/agents/injection.py` | `test_injection_corpus.py` (59 attacks flagged, 48 benign emails unflagged) |
| Strict structured output; malformed output flagged, never silently accepted | `openai_agent.py`, `validation.py` | `test_openai_agent.py`, `test_validation_properties.py` |
| Evidence must be verbatim; owners/deadlines/organizations never invented | `validate_analysis` | hypothesis oracle tests |
| Model limited to 4 proposal tools; destination, assignee, recipients and fields fixed by `ActionPolicy` | `app/services/action_policy.py` | `tests/unit/services/test_action_policy.py`, `tests/unit/test_action_policy.py` |
| No model output is executed; every action needs a human approval | architecture | end-to-end and security tests |

## Web, API and data protection

| Control | Implementation | Tests |
|---|---|---|
| Request size limit (1 MiB API, 2 MiB proxy) | `RequestSizeLimitMiddleware`, nginx | `tests/unit/api/test_middleware.py`, `test_security.py`, E2E |
| Rate limits keyed by verified user or client IP | `deps.rate_limit` | `tests/security/test_rate_limiting.py`, E2E rate-limit test |
| CORS disabled by default; only exact origins; https in production | `config.py` | `tests/security/test_cors.py` |
| Security headers (API + nginx CSP `script-src 'self'`, `frame-ancestors 'none'`) | middleware, `frontend/nginx.conf` | `test_middleware.py`, `e2e/test_api_system.py` |
| Parameterized queries only (SQLAlchemy); LIKE wildcards escaped | routes/services | `test_api_contract.py::test_email_search_escapes_like_wildcards` |
| SSRF: Graph paging links restricted to the configured origin/path; ADO URLs from config with validated segments; JWKS URL only for GUID tenants | adapters, `id_token.py` | adapter unit tests, `test_id_token_validation.py` |
| Safe deserialization of workflow checkpoints | `checkpoint_serializer()` | `tests/security/test_runtime_hardening.py` |
| Untrusted text rendered as text in the UI (no `dangerouslySetInnerHTML`, lint-enforced) | frontend | `InjectionWarning.test.tsx`, ActionCard XSS tests |
| Sanitized errors with correlation ids; no stack traces/SQL/secrets | `app/api/errors.py` | `tests/unit/api/test_errors.py` |
| DB statement/lock timeouts | `postgres_connect_args` | `test_runtime_hardening.py` |
| No file uploads; attachment content never downloaded | design | n/a (no upload endpoint exists) |

## Secrets, privacy and logging

| Control | Implementation | Tests |
|---|---|---|
| Tokens encrypted at rest (Fernet, key rotation), separate table, never returned to the browser | `SecretBox`, `CredentialStore` | `test_token_store*.py`, `test_auth_flows.py::test_connect_mailbox_stores_encrypted_tokens` |
| Connection flows never inherit another account's tokens | `EntraAuthService.complete` | `test_hardening_regressions.py` |
| Log redaction of secrets/tokens/bodies, incl. library log messages | `app/observability/logging.py` | `tests/unit/observability/test_logging.py` |
| Traces carry no content or exception text | `tracing.start_span` | `tests/unit/observability/test_tracing.py` |
| Retention purge for bodies, events, audit | `retention_service.py` | `test_retention_service.py`, `test_audit_integrity.py` |
| Secrets only via environment / Key Vault; production requires encryption + audit keys | `Settings` validators, `.env.example` placeholders | `tests/unit/core/test_config.py` |

## Supply chain and runtime

| Control | Implementation | Evidence |
|---|---|---|
| Locked dependencies (`uv.lock`, `package-lock.json`); CI installs with `--locked` / `npm ci` | CI | `.github/workflows/ci.yml` |
| Containers run as non-root; OS updates applied at build; pip removed | Dockerfiles | Trivy results in the audit |
| CI actions pinned to commit SHAs | `ci.yml` | actionlint |
