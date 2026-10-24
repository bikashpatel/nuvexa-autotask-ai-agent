# Design: Microsoft Graph change notifications (optional next stage)

**Status:** designed, not implemented. The MVP uses manual sync plus optional polling
(`MAIL_POLL_INTERVAL_SECONDS`), both driven by Graph delta queries. Webhooks reduce latency; they do not
replace delta sync — a notification only tells us *that* the inbox changed, and the worker still runs
the same idempotent delta round.

## Flow

```mermaid
sequenceDiagram
    participant G as Microsoft Graph
    participant API as NUVEXA API (/api/v1/webhooks/graph)
    participant DB as PostgreSQL (jobs)
    participant W as Worker
    W->>G: POST /subscriptions (resource me/mailFolders('inbox')/messages, changeType created,updated, clientState=random secret, expirationDateTime ≤ provider max)
    G->>API: POST ?validationToken=… (subscription validation)
    API-->>G: 200 text/plain echo of validationToken (within 10 s)
    G->>API: POST notification batch {value:[{subscriptionId, clientState, resource, changeType}]}
    API->>API: constant-time compare clientState with stored hash; reject unknown subscription
    API->>DB: enqueue sync_mailbox (dedupe_key sync:{account}) — no Graph call in the request
    API-->>G: 202 Accepted (fast)
    W->>G: delta round from the stored deltaLink (existing code path)
```

## Data model additions

`graph_subscriptions`: `id`, `tenant_id`, `connected_account_id`, `subscription_id` (unique),
`client_state_hash`, `resource`, `expires_at`, `status`, `last_notification_at`, `last_error`.

## Security

* `clientState` is a random 32-byte secret per subscription; only its SHA-256 hash is stored;
  comparison is constant-time. Notifications for unknown subscription ids are dropped and counted.
* The endpoint is public (Graph cannot authenticate with our session) but does no work beyond
  enqueuing a deduplicated sync job, so it cannot be used to read data or amplify load. Apply a
  request-size limit and rate limit; respond within Graph's timeout.
* Rich notifications (with resource data) are **not** used, so no message content transits the
  webhook and no certificate-based payload decryption is required.
* Validation responses echo the token as `text/plain` only after URL-decoding and length checks.

## Lifecycle

* Worker creates the subscription after a successful mailbox connection; renews it before expiry
  (scheduler, e.g. at 75 % of lifetime); deletes it on disconnect.
* Handles `lifecycleNotifications` (`reauthorizationRequired`, `subscriptionRemoved`,
  `missed`) by re-authorizing, re-creating, or forcing a delta round.
* Duplicate or out-of-order notifications are harmless: sync jobs are deduplicated and ingestion is an
  idempotent upsert keyed by immutable message id.

## Deployment requirements

A publicly reachable HTTPS notification URL (Azure Container Apps ingress), configured as
`GRAPH_NOTIFICATION_URL`; local development needs a tunnel. Until then, polling covers the use case.
