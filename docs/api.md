# API reference

Base path: `/api/v1`. Interactive OpenAPI docs: `/api/v1/docs` (enabled by default outside production; `EXPOSE_API_DOCS` overrides).
Response/request models are defined in [`app/schemas/api.py`](../app/schemas/api.py); action payloads
in [`app/schemas/actions.py`](../app/schemas/actions.py); tenant settings in
[`app/schemas/settings.py`](../app/schemas/settings.py).

## Conventions

* **Auth:** session cookie `nuvexa_session` (HttpOnly). All endpoints except those marked *public*
  require it (`401 authentication_required` otherwise).
* **CSRF:** for `POST`/`PUT`/`PATCH`/`DELETE`, send header `X-CSRF-Token` with the value of the
  readable `nuvexa_csrf` cookie (`403 csrf_failed` otherwise). Public auth POSTs are exempt.
* **Correlation:** send optional `X-Request-ID`; every response echoes `X-Request-ID`.
* **Pagination:** `?limit=` (1–100, default 25) and `?offset=` (0–1,000,000; larger → `422`); response
  `Page<T>` = `{ "items": T[], "total": n, "limit": n, "offset": n }`.
* **Text input:** a NUL character anywhere in a request body string, the query string or the path is
  rejected with `422 validation_failed` (`details[].type = "nul_character"`); the value is never echoed.
* **Errors:** every non-2xx response has the shape

  ```json
  { "error": { "code": "stale_version", "message": "…", "correlation_id": "…", "details": [] } }
  ```

  | Status | Codes |
  |---|---|
  | 400 | `bad_request` |
  | 401 | `authentication_required` |
  | 403 | `permission_denied`, `csrf_failed` |
  | 404 | `not_found` (also returned for resources in another tenant / not visible) |
  | 409 | `conflict`, `stale_version`, `invalid_state_transition`, `integration_not_configured` |
  | 500 | `configuration_error` (e.g. Microsoft sign-in requested but Entra ID is not configured), `internal_error` |
  | 413 | `request_too_large` (declared or streamed body over `MAX_REQUEST_BYTES`) |
  | 422 | `validation_failed` (`details` lists field errors) |
  | 429 | `rate_limited` |
  | 502 | `integration_error`, `agent_error` — Azure DevOps errors carry only stable identifiers (`TFnnnnnn`, exception type, field reference), never the upstream message text |

* **Async work:** sync, analysis, re-analysis and execution are queued jobs (`202`). Poll the
  workflow/action resource. Workflow statuses `received…action_proposed` and `executing`, and action
  status `executing`, are in-flight.

## Endpoints

### Health (public)
| Method | Path | Response |
|---|---|---|
| GET | `/health/live` | `HealthOut` |
| GET | `/health/ready` | `HealthOut` (checks the database; 503 when not ready) |

### Auth
| Method | Path | Notes |
|---|---|---|
| GET | `/auth/config` | *public* → `AuthConfigOut` |
| POST | `/auth/demo-login` | *public*, only when demo login enabled. Body `DemoLoginRequest {persona}` → `MeOut`, sets cookies |
| GET | `/auth/login?redirect_to=/inbox` | *public*, browser navigation → 302 to Microsoft Entra |
| GET | `/auth/callback` | *public*, OAuth redirect URI → 302 back to the dashboard |
| POST | `/auth/logout` | 204, revokes the session |
| GET | `/auth/me` | `MeOut {user, permissions[]}` |

### Integrations
| Method | Path | Notes |
|---|---|---|
| GET | `/integrations` | `IntegrationsOut` (Microsoft connections + sync state, Azure DevOps mode/config, LLM provider). `azure_devops.connection` is the caller's own delegated Azure DevOps connection (`ConnectionOut`, including `needs_reauth` ones) or `null` |
| GET | `/integrations/microsoft/connect?redirect_to=/integrations` | requires `email:sync`; browser navigation → 302 to consent (`Mail.Read`) |
| GET | `/integrations/azure-devops/connect?redirect_to=/integrations` | requires `action:execute`; `entra_delegated` mode only → 302 |
| DELETE | `/integrations/connections/{id}` | 204, deletes stored tokens, stops sync (mailbox or Azure DevOps connection; owner only, others → 404) |
| GET | `/integrations/azure-devops/test` | `AdoConnectionTestOut` |
| GET | `/integrations/azure-devops/projects` | `AdoProjectsOut` |
| GET | `/integrations/azure-devops/projects/{project}/work-item-types` | `AdoWorkItemTypesOut` |
| POST | `/integrations/azure-devops/validate` | Body `AdoValidateRequest {payload, server_side}` → `AdoValidateOut` |

### Emails
| Method | Path | Notes |
|---|---|---|
| GET | `/emails` | `Page<EmailListItem>`; filters `q` (subject/sender), `category`, `priority`, `analysis_status`, `connected_account_id` |
| GET | `/emails/{id}` | `EmailDetail` (body is plain text; render as text, never HTML) |
| POST | `/emails/sync` | Body `SyncRequest {connected_account_id?}` → 202 `JobAccepted`. An id that is not the caller's → 404; the caller's own unusable connection (`needs_reauth`, disconnected, not a mailbox) → 409. Mailboxes of deactivated users are never synced |
| POST | `/emails/{id}/analyze` | 202 `StartAnalysisOut` (`created=false` when an active run already exists) |

### Workflows
| Method | Path | Notes |
|---|---|---|
| GET | `/workflows` | `Page<WorkflowRunOut>`; filter `status`, `email_id` |
| GET | `/workflows/{id}` | `WorkflowDetail {run, analysis, actions[], events[]}` |
| GET | `/workflows/{id}/analysis` | `AnalysisOut` (latest attempt) |
| GET | `/workflows/{id}/actions` | `ActionOut[]` |
| GET | `/workflows/{id}/events` | `WorkflowEventOut[]` (timeline, tool calls, sanitized) |
| POST | `/workflows/{id}/reanalyze` | 202 `JobAccepted` — supersedes open proposals |
| POST | `/workflows/{id}/cancel` | `WorkflowRunOut` — cancels open proposals; `409 conflict` while any action is `executing` or `uncertain` (resolve it first) |

### Actions (approvals and execution)
| Method | Path | Notes |
|---|---|---|
| GET | `/actions` | `Page<ActionOut>`; filters `status` (repeatable, e.g. `?status=pending_approval&status=uncertain`), `action_type`, `workflow_run_id` |
| GET | `/actions/{id}` | `ActionDetail` (+ approvals, executions, per-user `permissions`) |
| PATCH | `/actions/{id}` | Body `ActionUpdateRequest {expected_version, payload}` → `ActionDetail`. Bumps `version`, recomputes `payload_hash`, **invalidates any approval** |
| POST | `/actions/{id}/approve` | Body `ApproveRequest {version, payload_hash, comment?}` — must match the current action → `ActionDetail` |
| POST | `/actions/{id}/reject` | Body `RejectRequest {reason}` → `ActionDetail` |
| POST | `/actions/{id}/cancel` | Body `CancelRequest {reason?}` → `ActionDetail` |
| POST | `/actions/{id}/execute` | Body `ExecuteRequest {version, payload_hash}` → 202 `ExecuteAccepted {action, job_id}`. Actions of a cancelled or rejected workflow → `409 invalid_state_transition` (audited `execution_blocked`) |
| POST | `/actions/{id}/reconcile` | For `uncertain` actions, and `executing` actions whose execution lease expired without a recorded outcome: queues an idempotency-tag lookup → 202 `ExecuteAccepted {action, job_id}`; found → `completed`, confirmed absent → `failed` (safe to retry). A live execution → `409 conflict`. `permissions.can_reconcile` follows the same rule |
| GET | `/actions/{id}/executions` | `ExecutionOut[]` |
| GET | `/actions/{id}/history` | `AuditEventOut[]` |

### Tasks, reply drafts
| Method | Path | Notes |
|---|---|---|
| GET | `/tasks` | `Page<FollowUpTaskOut>`; filter `status` |
| PATCH | `/tasks/{id}` | Body `TaskUpdateRequest {status}` |
| GET | `/reply-drafts` | `Page<ReplyDraftOut>` (drafts are never sent by the system) |

### Audit, settings, dashboard
| Method | Path | Notes |
|---|---|---|
| GET | `/audit-events` | `Page<AuditEventOut>`; filters `event_type`, `resource_type`, `resource_id`, `action_id`, `outcome` |
| GET | `/audit-events/integrity` | approver/admin (`audit:read_all`) → `AuditIntegrityOut {checked, tampered[], unsigned[], verified_at}`: recomputes each event's HMAC |
| GET | `/settings` | `SettingsOut` |
| PUT | `/settings` | admin only; Body `SettingsUpdateRequest {expected_version, settings}` |
| GET | `/dashboard/summary` | `DashboardSummaryOut` |

## Role permissions

| Permission | viewer | member | approver | admin |
|---|---|---|---|---|
| read own emails/actions | ✓ | ✓ | ✓ | ✓ |
| read tenant-wide emails/actions/audit | | | ✓ | ✓ |
| connect a mailbox / Azure DevOps (delegated tokens) | | ✓ | ✓ | ✓ |
| sync, analyze, edit/approve/reject/cancel/execute **own** actions | | ✓ | ✓ | ✓ |
| approve/reject/execute **others'** actions | | | ✓ | ✓ |
| change settings | | | | ✓ |

`MeOut.permissions` lists: `email:read`, `email:read_all`, `email:sync`, `workflow:run`, `action:edit`,
`action:approve`, `action:execute`, `action:approve_any`, `audit:read_all`, `settings:write`.

The Azure DevOps diagnostic endpoints (`/integrations/azure-devops/test|projects|…|validate`) use the
service credentials and require `action:edit` (members and above). Rate limits are keyed by the verified
user (authenticated routes) or client IP (sign-in routes).

Refused sign-ins (deactivated user, an Entra identity that does not own the matching account) are audited
as `event_type=login` with `outcome=denied`.
