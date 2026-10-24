# infra/azure — reference notes

Nothing in this repository provisions Azure resources. The reference architecture, secrets list,
rollout order and an illustrative (unverified) `az` CLI sequence are in
[docs/deployment.md](../../docs/deployment.md).

When you codify it (Bicep or Terraform with the AzAPI/AzureRM providers), keep these properties:

* One user-assigned managed identity with **AcrPull**, **Key Vault Secrets User** and
  **Cognitive Services OpenAI User**; apps and the migrate job reference Key Vault secrets through it.
* `frontend` is the only external ingress; `api` internal (target port 8000); `worker` no ingress.
* The `migrate` job (manual trigger, `nuvexa migrate`) runs with the same backend image tag before
  `worker` and `api` are updated.
* API probes: startup/liveness `GET /api/v1/health/live`, readiness `GET /api/v1/health/ready`.
* Worker termination grace period above 30 s; `JOB_LEASE_SECONDS` above the longest job.
* Traces: OTLP/HTTP to an OpenTelemetry Collector app (the managed Container Apps agent is
  gRPC-only).
* PostgreSQL Flexible Server 18 with private access and TLS; application role distinct from the
  server administrator.

`infra/postgres/initdb/` is used only by the local `docker-compose.yml`.
