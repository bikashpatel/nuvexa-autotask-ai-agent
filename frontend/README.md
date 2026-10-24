# NUVEXA AutoTask — web dashboard

React + TypeScript + Mantine dashboard for **NUVEXA AutoTask** (by Elarxsys). It is the human
review surface of the platform: reviewers see the original email, the AI analysis, and the
proposed actions, edit the exact payload, and approve, reject or execute it. Nothing is written to
an external system without an approval bound to the payload's version and SHA-256 hash.

The API contract lives in [`../docs/api.md`](../docs/api.md) and `../app/schemas/*.py`; the
TypeScript mirror is [`src/api/types.ts`](src/api/types.ts).

## Quick start

```bash
cd frontend
npm ci
npm run dev            # http://localhost:5173 — proxies /api → http://localhost:8000
```

Point the dev proxy elsewhere with `NUVEXA_API_PROXY_TARGET=http://host:port npm run dev`.
With the API in demo mode (`DEMO_MODE=true`), the login page offers demo personas
(admin / approver / member / viewer).

## Scripts

| Script                            | What it does                                                                            |
| --------------------------------- | --------------------------------------------------------------------------------------- |
| `npm run dev`                     | Vite dev server on port 5173 (strict), `/api` proxied to the API (same origin, no CORS) |
| `npm run build`                   | `tsc -b` type check, then `vite build` → `dist/`                                        |
| `npm run preview`                 | Serve `dist/` on port 4173 (also proxies `/api`)                                        |
| `npm run lint`                    | ESLint 10 flat config, type-aware, `--max-warnings=0`                                   |
| `npm run typecheck`               | `tsc -b` (app + node configs)                                                           |
| `npm test`                        | Vitest (jsdom), single run                                                              |
| `npm run test:watch`              | Vitest watch mode                                                                       |
| `npm run test:coverage`           | Vitest + V8 coverage (text, `lcov`, `json-summary` → `coverage/`); thresholds enforced  |
| `npm run format` / `format:check` | Prettier                                                                                |

## Structure

```
src/
  api/            client.ts (typed fetch: CSRF, X-Request-ID, error schema, 401), endpoints.ts
                  (one function per documented endpoint), types.ts (contract mirror), queryKeys.ts
  components/     shared UI: StatusBadge, domain badges, ErrorState (with support reference),
                  EmptyPanel, InjectionWarning, ConfidenceIndicator, ExternalLink, PlainText, …
  features/
    auth/         session context, RequireAuth guard, LoginPage
    shell/        AppLayout (header, nav, theme toggle, account menu), navigation
    overview/     KPIs, distributions, LLM usage, recent activity
    inbox/        InboxPage, EmailDetailPage, EmailParts, sync/analyze/re-analyze hooks
    approvals/    approval queues (pending / approved / needs attention)
    actions/      ActionReviewPage (core screen), ActionCard, ActionEditors, ActionControls,
                  AnalysisPanel, payloadForms.ts (form model + validation mirroring actions.py)
    workflows/    WorkflowsPage, WorkflowDetailPage (stepper, event timeline, audit trail, metrics)
    integrations/ Microsoft 365, Azure DevOps, LLM provider, diagnostics
    settings/     TenantSettings form (admin write, others read-only)
    audit/        filterable audit log with expandable details
    tasks/        follow-up tasks and reply drafts
  lib/            formatting, notifications, query client, URL/confidence helpers
  routes/         route table (code-split pages), route error boundary
  theme/          Mantine theme, global CSS, status.ts (single source of status colours/labels)
  test/           setup, render helpers, fetch mock, fixtures
```

## Versions and decisions

Exact versions are pinned in `package.json` (no ranges) and `package-lock.json` is committed.

| Area                                              | Version                                                                                             |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| React / React DOM                                 | 19.3.1                                                                                              |
| Vite / @vitejs/plugin-react                       | 8.3.5 / 6.1.2                                                                                       |
| Mantine (core, hooks, notifications, form, dates) | 9.7.3 (+ postcss-preset-mantine 1.18.0)                                                             |
| TanStack Query                                    | 5.104.5                                                                                             |
| React Router                                      | 8.5.0 (`react-router`, data mode: `createBrowserRouter` + `RouterProvider` from `react-router/dom`) |
| @tabler/icons-react · dayjs                       | 3.49.0 · 1.11.23                                                                                    |
| ESLint · typescript-eslint · react-hooks plugin   | 10.13.0 · 8.72.0 · 7.1.1                                                                            |
| Vitest · jsdom · Testing Library (react)          | 5.0.5 · 30.1.3 · 16.3.3                                                                             |
| @vitest/coverage-v8 · axe-core                    | 5.0.5 (matches Vitest exactly) · 4.14.0                                                             |
| **TypeScript**                                    | **6.0.3** (see below)                                                                               |
| Node (build image)                                | 24.22.1 (React Router 8 needs ≥ 22.22)                                                              |

- **TypeScript 6.0.3, not 7.0.** TypeScript 7.0.2 is the current release, but `typescript-eslint`
  8.72.0 (latest) declares `typescript: ">=4.8.4 <6.1.0"`. Type-aware linting is part of the quality
  gate, so the newest TypeScript inside that range — 6.0.3 — is pinned. Move to 7.x once
  typescript-eslint declares support.
- **Mantine 9 specifics:** `Grid` uses `gap` (not `gutter`), `Collapse` uses `expanded`, and the new
  `EmptyState` and `DataList` components are used for empty states and fact lists.
  `MantineProvider env="test"` is used in tests (no portals/transitions).
- **No chart library.** Distributions are thin, accessible bar lists (`DistributionBars`): labels
  and values are always text, bars are decorative, so colour is never the only signal.
- **Data mode routing with lazy pages.** Each page is a separate chunk; login and the shell are in
  the main chunk.

## How the API rules are implemented

- **Same origin, cookies, CSRF.** All calls go to `/api/v1` with `credentials: "same-origin"`.
  `POST/PUT/PATCH/DELETE` send `X-CSRF-Token` = `nuvexa_csrf` cookie (`src/api/client.ts`).
- **Correlation.** Every request carries a generated `X-Request-ID`; error toasts and panels show
  the server `correlation_id` (or the echoed request id) as a _support reference_.
- **Errors.** Every non-2xx is parsed into `ApiError` (`code`, `message`, `correlation_id`,
  `details`). 401 → full-page redirect to `/login?redirect_to=<current page>` (except the session
  probe); 409 `stale_version` → a "this action changed — reload" banner with a reload button; 422 →
  field errors mapped onto form inputs (FastAPI `{loc,msg}` and `{field,message}` are both accepted,
  discriminated-union kind prefixes are stripped).
- **No optimistic writes.** Approve / reject / cancel / execute / reconcile / save show a loading,
  disabled button and update the UI only from the server response. Approve and execute send the
  `version` and `payload_hash` currently displayed. Writes are never retried automatically.
- **Approval invalidation is explicit.** After a PATCH the card shows "Saved as version vN, new
  payload hash …" and, if an approval existed, that it was invalidated and a new approval is needed.
  Invalidated approvals are struck through in the history.
- **Queued jobs.** Execute and reconcile return `202` while the job waits for a worker; the card
  shows "Queued", blocks re-submission and polls until the action changes.
- **Polling.** TanStack Query `refetchInterval` (2 s) while a workflow is in flight
  (`received…action_proposed`, `executing`) or an action is `executing`; it stops otherwise.
- **Untrusted content.** Email bodies, AI rationale, evidence and drafts render as plain text
  (`white-space: pre-wrap`). `dangerouslySetInnerHTML` and `innerHTML` are banned by an ESLint rule.
  Only `http(s)` URLs become links (`target="_blank" rel="noopener noreferrer"`); `mock://` URLs show
  as text with a **Mock** badge; any other scheme is plain text. References extracted from emails
  are never clickable.
- **Permissions.** Controls follow the per-action `permissions` flags from the API, and
  `MeOut.permissions` for page-level actions (sync, analyze, settings write, …).

## Container image

`Dockerfile` is multi-stage: `node:24.22.1-alpine` builds `dist/`, then
`nginxinc/nginx-unprivileged:1.30.6-alpine` (uid 101, port 8080) serves it.

```bash
docker build -t nuvexa-frontend frontend
docker run --rm -p 8080:8080 -e API_UPSTREAM=http://host.docker.internal:8000 nuvexa-frontend
```

- `nginx.conf` is an **envsubst template**, copied to `/etc/nginx/templates/default.conf.template`
  and rendered at start by the image's entrypoint. Only `API_UPSTREAM` and `NGINX_LOCAL_RESOLVERS`
  are substituted (`NGINX_ENVSUBST_FILTER`), so nginx's own `$variables` are safe. `API_UPSTREAM`
  defaults to `http://api:8000` (docker compose); on Azure Container Apps use the internal app URL
  (e.g. `http://nuvexa-api`). No path and no trailing slash.
- The upstream is resolved **per request** (`resolver` from `/etc/resolv.conf`, via the image's
  `NGINX_ENTRYPOINT_LOCAL_RESOLVERS`, `valid=10s`): the container starts even when the API is not up
  yet (`/api` returns 502 until it is) and follows the API to a new address after it is recreated.
  Absolute upstream redirects (`http://api:8000/…`) are rewritten to relative ones.
- `/api/` is proxied with `Host $proxy_host` (matches the upstream, as Container Apps requires),
  `X-Forwarded-Host/-Proto/-For`, `X-Real-IP`, and `X-Request-ID` (passed through, or nginx's
  `$request_id`). Body limit 2 MiB (above the API's 1 MiB, so the API returns its JSON 413).
- SPA fallback to `index.html`; `/assets/*` (content-hashed) are `public, max-age=31536000,
immutable`; `index.html` and routes are `no-cache`; `/healthz` for probes; dotfiles are denied.
- Security headers on every response: strict CSP, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: same-origin`, `X-Frame-Options: DENY` + `frame-ancestors 'none'`,
  `Permissions-Policy`, COOP/CORP. Duplicate API headers are hidden. HSTS belongs on the TLS
  terminator.
- **CSP and `style-src 'unsafe-inline'`.** Mantine injects `<style>` elements at runtime (theme CSS
  variables, responsive style props) and sets inline `style` attributes. Mantine supports nonces,
  but a static nginx deployment cannot mint a per-request nonce. Scripts stay `'self'` only (the
  build has no inline scripts), and the app never renders untrusted HTML, so the residual risk is
  CSS injection only.
- Gzip for static assets only; API responses are not compressed (BREACH-style side channels).
- Read-only root filesystem: mount writable tmpfs volumes at `/tmp` and `/etc/nginx/conf.d`.

## Tests

Vitest 5 + Testing Library (jsdom). Every module in `src/` has a co-located `*.test.ts(x)`; no
backend or network is needed. `npm run test:coverage` enforces the coverage thresholds.

**Tooling**

- **Fetch mock at the client boundary.** `mockFetch(routes)` in `src/test/utils.tsx` records every
  request (method, URL, headers, JSON body) and fails unmatched requests loudly with HTTP 599.
  Helpers: `errorResponse(status, code, …)` (API error schema with a correlation id), `callsTo`,
  `queryOf` (repeated query params), `deferred` (keep a request in flight), `createWrapper` (for
  `renderHook`), and `renderWithProviders(ui, { session, path, route, withNotifications, extraRoutes })`,
  which returns the memory `router` for navigation assertions.
- **Fixtures** (`src/test/fixtures.ts`) follow `app/schemas/api.py` exactly. They include personas whose
  permission sets mirror `app/services/authz.py` (`PERSONAS.viewer/member/approver/admin`) and
  hostile strings (`XSS_IMG`, `XSS_SCRIPT`).
- **Fake timers** drive everything time-based: 2 s polling, the 120 s queued-job watch, 350 ms
  search debounce, the 3 s/8 s post-sync refresh, 15 s/30 s list refreshes. No test waits on real
  time.
- **Accessibility.** `axe-core` (`src/test/axe.ts`, `expectNoSeriousA11yViolations`) runs on every
  page and shared component, in idle, loading, error and open-dialog states. Any `serious` or
  `critical` violation fails the test. `color-contrast` is disabled because jsdom does no layout.
  Queries are role-based with accessible names. Tests also cover dialog focus trap and Escape, and
  keyboard activation.
- **Contract test** (`src/api/contract.test.ts`) compares every enum, status map and label map
  with a snapshot of `app/domain/enums.py`. When the repository is checked out, it also compares
  them with the live file (and `Perm` with `app/services/authz.py`).

**Guarantees covered by tests**

- **Client:** every endpoint function in `endpoints.ts` is checked for:
  - method and exact URL (encoded ids);
  - query params, including repeated `status`;
  - JSON body;
  - `X-CSRF-Token` on unsafe methods only, and `X-Request-ID` on every request;
  - `skipAuthRedirect` for the health and auth endpoints;
  - error mapping to `ApiError`: code, message, correlation id, field errors.
- **401:** the user is sent to `/login?redirect_to=<path+search>`, so they return to the same page.
  Hostile `redirect_to` values fall back to `/`.
- **No optimistic writes:** approve, reject, cancel, execute, reconcile, save, task status,
  disconnect and demo sign-in each keep the shown state until the mocked server answers. Buttons are
  disabled while pending, and a double-click sends exactly one request.
- **Approval binding:** approve and execute send exactly the displayed `version` + `payload_hash`.
  After a `PATCH`, the save notice and the approval history show the invalidated approval, and
  Execute is hidden until a new approval.
- **Errors:**
  - 409 `stale_version` shows a "changed — reload" banner with a reload button (actions and settings).
  - 422 field errors are mapped onto the matching form fields; unknown fields are listed.
  - Other errors show a toast with the code and support reference.
- **Untrusted content:** HTML and script strings are injected into email bodies, AI rationale,
  evidence, references, drafts, audit details and titles. Tests assert that no element is created
  and the text stays visible.
- **Links:**
  - `mock://` and other non-http(s) URLs are never links.
  - External links use `target="_blank" rel="noopener noreferrer"`.
  - References copied from email are never clickable.
- **Permissions:**
  - Per-action `permissions` flags drive each control.
  - The viewer persona sees no mutating controls.
  - Settings are read-only for non-admins.

## Known gaps

- No end-to-end browser tests (Playwright) against a running API yet.
- Approval-queue confidence comes from `GET /workflows/{id}/analysis` per row (cached per run),
  because `ActionOut` carries no confidence field.
- Theme preference lives in `localStorage`; a brief light→dark flash is possible on first paint
  because the Mantine colour-scheme inline script is omitted to keep `script-src 'self'`.
