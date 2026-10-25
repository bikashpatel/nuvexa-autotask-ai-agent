# Deployment — Azure Container Apps reference architecture

> **Reference only.** This repository does not deploy anything and creates no cloud resources. The
> architecture below is codified in Bicep under [`infra/azure/`](../infra/azure/README.md). The
> template and every command in this document have **not been deployed or executed** by this
> repository: the template is compiled, linted, scanned and tested offline only
> (`scripts/security-scan.sh config`, `tests/security/test_infrastructure_as_code.py`). Review it
> against current Microsoft documentation and your network and security policies before use.

## Architecture

```mermaid
flowchart LR
    user([Browser]) -->|HTTPS| fe

    subgraph env[Container Apps environment — VNet integrated]
        fe[frontend app<br/>nginx :8080<br/>external ingress]
        api[api app<br/>nuvexa api :8000<br/>internal ingress]
        worker[worker app<br/>nuvexa worker<br/>no ingress]
        mig[[migrate job<br/>nuvexa migrate<br/>manual trigger]]
        otel[otel-collector app<br/>OTLP/HTTP :4318<br/>internal]
    end

    fe -->|/api/ proxy| api
    api --> pg[(Azure Database for PostgreSQL<br/>Flexible Server, private access)]
    worker --> pg
    mig --> pg
    worker --> graph[Microsoft Graph]
    worker --> ado[Azure DevOps]
    worker --> aoai[Azure OpenAI]
    api --> entra[Microsoft Entra ID]
    api -. OTLP .-> otel
    worker -. OTLP .-> otel
    otel --> appi[Application Insights]
    env -. console logs .-> law[Log Analytics]
    kv[Key Vault] -. secret references via managed identity .-> env
    acr[Container Registry] -. image pull via managed identity .-> env
```

| Resource | Purpose | Notes |
|---|---|---|
| Virtual network | Private network of the workload | Subnets: Container Apps infrastructure (delegated to `Microsoft.App/environments`), PostgreSQL (delegated to `Microsoft.DBforPostgreSQL/flexibleServers`), private endpoints; private DNS zones for PostgreSQL and Key Vault |
| Container Apps environment | Hosts apps and the job | Workload-profiles environment (Consumption profile) with VNet integration; logs to Log Analytics through diagnostic settings (no workspace key); peer-to-peer traffic encryption |
| `frontend` container app | nginx, static dashboard, proxies `/api/` | Only app with **external** ingress (HTTPS, HTTP redirected; custom domain + managed certificate) |
| `api` container app | `nuvexa api`, port 8000 | **Internal** ingress only; reached through the frontend proxy |
| `worker` container app | `nuvexa worker` | No ingress; scales 1..n |
| `otel-collector` container app | OpenTelemetry Collector (contrib), OTLP/HTTP :4318 | Internal ingress; exports traces to Application Insights |
| `migrate` Container Apps job | `nuvexa migrate` | Manual trigger, run before each rollout; holds only the migration credential |
| Azure Database for PostgreSQL Flexible Server | System of record, job queue, LangGraph checkpoints | PostgreSQL 18; private access (delegated subnet + private DNS zone); TLS required; zone-redundant HA |
| Azure Key Vault | Secrets | RBAC mode, purge protection, public network access disabled, private endpoint; apps and job read via managed identity (**Key Vault Secrets User**, per secret) |
| Azure Container Registry | Images | Existing registry (template parameter); pull via managed identity (**AcrPull**); admin user disabled |
| Log Analytics | Container console and system logs (structured JSON) | Environment-level diagnostic settings; shared-key ingestion disabled |
| Application Insights | Traces | Workspace-based; through an OpenTelemetry Collector (see [Telemetry](#telemetry)) |
| User-assigned managed identity of the apps | frontend, api, worker, collector | AcrPull (registry), Key Vault Secrets User on each secret the apps read (never on the vault), Cognitive Services OpenAI User (Azure OpenAI account); only the worker's code can obtain tokens for it |
| User-assigned managed identity of the job | `migrate` job only | AcrPull (registry), Key Vault Secrets User on `database-migrate-url` only; the apps' identity cannot read that secret |

The template is [`infra/azure/main.bicep`](../infra/azure/main.bicep) (see
[Infrastructure as code](#infrastructure-as-code-bicep)). Azure OpenAI and the container registry are
existing resources it references and assigns roles on; it does not create them.

## Images and configuration

* Build both images in CI (already done without push in `.github/workflows/ci.yml`), push to ACR
  with an immutable tag (git SHA). Deploy the **same backend tag** to `migrate`, `api` and `worker`
  (the template has one `backendImageTag` for all three).
* All configuration is environment variables (see `.env.example`). Production values (the template
  sets these on `api` and `worker`, plus the Entra, Azure OpenAI and Azure DevOps identifiers from its
  parameters):

| Variable | Value |
|---|---|
| `APP_ENV` | `production` (requires `TOKEN_ENCRYPTION_KEYS` and `AUDIT_HMAC_KEY`; secure cookies; demo login disabled; API docs off by default) |
| `DEMO_MODE` | `false` |
| `EXPOSE_API_DOCS` | `false` |
| `APP_BASE_URL` | `https://<your-dashboard-host>` |
| `OAUTH_REDIRECT_URI` | `https://<your-dashboard-host>/api/v1/auth/callback` (registered in Entra) |
| `MAIL_MODE` | `graph` |
| `LLM_PROVIDER` | `azure_openai` with `AZURE_OPENAI_USE_ENTRA_ID=true` and `AZURE_CLIENT_ID=<managed identity client id>` |
| `AZURE_DEVOPS_MODE` | `pat` or `entra_delegated` |
| `ENTRA_ALLOWED_TENANT_IDS`, `BOOTSTRAP_ADMIN_EMAILS` | set explicitly |
| `RUN_EMBEDDED_WORKER` | `false` (dedicated worker app) |
| `LOG_FORMAT` | `json` |
| `FORWARDED_ALLOW_IPS` (api) | the environment's infrastructure subnet CIDR (the template's default), or `*` when the api app has internal ingress only |
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT` | `true`, `http://<otel-collector-app>` (see Telemetry) |
| `DATABASE_RUNTIME_ROLE` | `nuvexa_app` (api, worker and the migrate job; see [Database](#database)) |
| `RATE_LIMIT_BACKEND` (api) | `database` whenever the api can run more than one replica (see [Scaling](#scaling-probes-and-job-leases)) |
| `MAIL_POLL_INTERVAL_SECONDS`, `DATABASE_POOL_SIZE`, `WORKER_CONCURRENCY`, `JOB_LEASE_SECONDS`, `LOG_LEVEL` | template parameters (defaults as in `.env.example`) |

The template gives the `migrate` job only `DATABASE_URL` (from `database-migrate-url`, the schema
owner), `DATABASE_RUNTIME_ROLE`, `DEMO_MODE=false`, `LOG_FORMAT` and `LOG_LEVEL`, and its own managed
identity. It deliberately runs without `APP_ENV=production`: production settings validation requires
the token and audit keys, which schema changes and grants do not need, and anyone allowed to start a
job can use its secrets.

### Secrets (Key Vault → container app secrets → `secretref:` env vars)

| Key Vault secret | Env var | Needed when |
|---|---|---|
| `database-url` | `DATABASE_URL` (api, worker) | always: the DML-only runtime role `nuvexa_app`, never the schema owner or the server administrator (`postgresql+psycopg://nuvexa_app:<url-encoded password>@<server>.postgres.database.azure.com:5432/nuvexa?sslmode=require`; see [Database](#database)) |
| `database-migrate-url` | `DATABASE_URL` (migrate job only) | always: the schema owner `nuvexa_owner` (same URL format). Only the job's identity can read it (see [Database](#database)) |
| `token-encryption-keys` | `TOKEN_ENCRYPTION_KEYS` | always in production |
| `audit-hmac-key` | `AUDIT_HMAC_KEY` | always in production (at least 32 characters; the API and worker refuse to start without it) |
| `entra-client-secret` | `ENTRA_CLIENT_SECRET` | Entra sign-in |
| `azure-devops-pat` | `AZURE_DEVOPS_PAT` | `AZURE_DEVOPS_MODE=pat` (the template references it only in that mode) |
| `appinsights-connection-string` | `APPLICATIONINSIGHTS_CONNECTION_STRING` (otel-collector) | tracing enabled (`enableTracing`) |
| `azure-openai-api-key` | `AZURE_OPENAI_API_KEY` | only if not using managed identity (not wired by the template) |
| `openai-api-key` | `OPENAI_API_KEY` | `LLM_PROVIDER=openai` (not wired by the template) |
| `otlp-headers` | `OTEL_EXPORTER_OTLP_HEADERS` | exporter needs auth, e.g. Langfuse directly (not wired: the apps export to the collector) |

Use versionless Key Vault secret URIs to pick up rotations: Container Apps fetches the latest version
within about 30 minutes and restarts revisions that reference it in env vars. Pin a version when you
need explicit control. PostgreSQL authentication uses a password (in `database-url`); Entra
token-based database authentication is not supported by the current configuration.

**Setting the values.** The template creates the vault and references secrets by name only; it never
writes a secret value. Every secret the template references must exist before the `migrate` stage:
that stage assigns *Key Vault Secrets User* on each secret individually (the apps' identity on the
apps' secrets, the job's identity on `database-migrate-url`), which needs the secret to exist, and a
container app revision fails on a missing Key Vault secret. Set them with a principal that holds
**Key Vault Secrets Officer** on the vault (the template grants it to nobody: assign it just in time),
from a network that reaches the vault's private endpoint, or temporarily with
`keyVaultPublicNetworkAccess=Enabled` plus your address in `keyVaultAllowedIpRanges` (then deploy
`Disabled` again).

## Frontend → API routing

The frontend image proxies `/api/` to `http://api:8000`, which matches Docker Compose. In Container
Apps an app with internal ingress is reached inside the environment at `http://<app-name>` (ingress
port 80 → target port 8000) or `https://<app-name>.internal.<environment-default-domain>`, not on
port 8000. The frontend image therefore needs a configurable upstream for this deployment (for
example an nginx template rendered from an environment variable), and the proxied request's `Host`
header must be the API app's name/FQDN so the environment's ingress routes it; forward the original
host in `X-Forwarded-Host`. Keep everything same-origin: the session cookie and the OAuth callback
live on the dashboard host. The image implements this with `API_UPSTREAM` (default `http://api:8000`).

The template sets `API_UPSTREAM=http://<api-app-name>`. An internal app's ingress redirects plain HTTP to
HTTPS unless `allowInsecure` is set, and the nginx proxy would hand that redirect to the browser, so the
api (and the collector) accept HTTP on their **internal** ingress only; the frontend's external ingress
keeps the redirect. With the environment's peer-to-peer encryption (`enablePeerTrafficEncryption`,
default on) calls to `http://<app-name>` are still TLS-encrypted between the apps.

**Client IP integrity.** The API uses the forwarded client address for login rate limiting and audit
records, so nginx *overwrites* `X-Forwarded-For` with `$remote_addr` instead of appending a
client-supplied value. When another proxy terminates traffic first (the Container Apps ingress),
`$remote_addr` is that proxy: nginx must trust that proxy's `X-Forwarded-For` (`set_real_ip_from`, `real_ip_header X-Forwarded-For`,
`real_ip_recursive on`) so `$remote_addr` becomes the real client before
it is forwarded, and keep `FORWARDED_ALLOW_IPS` on the API restricted to the frontend's network.
The frontend image does this with the realip module: set `NGINX_REAL_IP_FROM` to the proxy's CIDR (the
infrastructure subnet; `infra/azure/main.bicep` sets it). The image default `127.0.0.1/32` trusts no real proxy,
so without it the API would see the ingress address as the client of every request and login rate limits would
be shared by all users.

## Database

* Flexible Server, PostgreSQL 18, private access (delegated subnet in the same VNet) or a private
  endpoint; TLS required (`sslmode=require`).
* **Two database roles, and never the server administrator in an application URL** (risk R-4,
  [security model](security-model.md#database-roles)):

| Role | Example name | Used by | Privileges | Key Vault secret → env |
|---|---|---|---|---|
| Owner (migration) role | `nuvexa_owner` | `migrate` job only | Owns database `nuvexa` and every object in it; the only role that runs DDL | `database-migrate-url` → `DATABASE_URL` of the job |
| Runtime role | `nuvexa_app` | `api`, `worker` | `SELECT`/`INSERT`/`UPDATE`/`DELETE` on the application and LangGraph checkpoint tables, `USAGE`/`SELECT` on sequences; no `CREATE`, no ownership, no `TRUNCATE`, no `UPDATE` of `audit_events`; granted by `nuvexa migrate` | `database-url` → `DATABASE_URL` |

**One-time setup per environment** (before the first `migrate` run), as the server administrator in
`psql` over the private network. Generate both passwords, store them only in Key Vault, and keep
the runtime role free of any other attribute or membership (no `CREATEROLE`, no `azure_pg_admin`, no
membership in the owner role):

```sql
CREATE ROLE nuvexa_owner LOGIN PASSWORD '<owner-password>';
CREATE ROLE nuvexa_app LOGIN PASSWORD '<runtime-password>';
-- Hand database "nuvexa" (az postgres flexible-server db create) to the owner role. Since PostgreSQL 16 the
-- administrator must be able to SET ROLE to the new owner for this; drop that membership afterwards.
GRANT nuvexa_owner TO <admin-user>;
ALTER DATABASE nuvexa OWNER TO nuvexa_owner;
REVOKE nuvexa_owner FROM <admin-user>;
```

Nothing else is granted by hand: `nuvexa migrate` (next section) grants the runtime role its
privileges, on every run. To rotate a password: `ALTER ROLE nuvexa_app PASSWORD '<new>'` as the
administrator, then update the Key Vault secret (`database-url` or `database-migrate-url`).

* `database-url` (runtime): `postgresql+psycopg://nuvexa_app:<url-encoded password>@<server>.postgres.database.azure.com:5432/nuvexa?sslmode=require`;
  `database-migrate-url` (owner): the same with `nuvexa_owner`. Set `DATABASE_RUNTIME_ROLE=nuvexa_app` in
  the shared environment variables: the job grants that role, and `api`/`worker` use it for their startup
  check.
* **Startup check.** With `APP_ENV=production`, `api` and `worker` inspect their own database role at
  startup and log `database_role_least_privilege` (INFO) — or a `database_role_overprivileged` WARNING
  whose `findings` list what is wrong: `superuser`, `createrole`, `owns_schema`, `owns_tables`,
  `create_in_schema`, `create_in_database`, `truncate_tables`, `update_audit_events`,
  `not_runtime_role` (connected as another role than `DATABASE_RUNTIME_ROLE`). They still start; alert on
  the warning. `database_privilege_check_failed` means the check could not run (e.g. database
  unreachable at startup).
* Connections: each API/worker process uses a pool of `DATABASE_POOL_SIZE` (default 5) plus the same
  overflow, and the worker's checkpointer pool up to `WORKER_CONCURRENCY` (at least 2). Multiply by
  replicas, add the migrate job, and keep the total under the server's `max_connections` (small
  Burstable SKUs allow few connections).
* Enable automated backups/PITR; zone-redundant HA for production.

## Migrations as a Container Apps job

`nuvexa migrate` connects as the **owner** role and, in this order:

1. runs `alembic upgrade head`;
2. creates/updates the LangGraph checkpoint tables;
3. grants the runtime role (`DATABASE_RUNTIME_ROLE`) its privileges (`app/db/privileges.py`). The step is
   idempotent and convergent — it revokes whatever the role holds on the schema's tables and sequences, then
   grants DML only, revokes `UPDATE` on `audit_events` and writes on the migration bookkeeping tables, and sets
   `ALTER DEFAULT PRIVILEGES FOR ROLE <owner>` so tables created by later migrations are usable at once. A
   privilege granted by hand in between is removed again by the next run. The job fails with a clear error if
   the role does not exist or is the owner role itself, and logs `database_role_overprivileged` if the role
   still holds more (e.g. membership in the owner role). Without `DATABASE_RUNTIME_ROLE` the step is skipped
   with a warning.

`api` and `worker` never run DDL (their LangGraph checkpointer does not run its `setup()`), so they work with
the runtime role only after the job succeeded.

Configure the job with the owner secret instead of `database-url`:
`--secrets database-migrate-url=keyvaultref:$KVURI/database-migrate-url,identityref:<migrate-identity>` and
`--env-vars DATABASE_URL=secretref:database-migrate-url DATABASE_RUNTIME_ROLE=nuvexa_app DEMO_MODE=false
LOG_FORMAT=json LOG_LEVEL=INFO` — nothing else: the job needs no token-encryption, audit or provider secrets, and
anyone who can start a job can use the secrets it references (infra/azure/main.bicep configures exactly this).
Only the job may reference `database-migrate-url`: give the job **its own** user-assigned
managed identity with *Key Vault Secrets User* scoped to that one secret, and scope the apps' identity to
their secrets (or use a separate vault) — with one identity holding the role on the whole vault, a
compromised `api` could read the owner credentials from Key Vault and the role split would protect little.

Rollout order for every release:

1. Push images (`nuvexa-backend:<sha>`, `nuvexa-frontend:<sha>`).
2. Update the `migrate` job to `<sha>`, start it and **wait for success** (fail the pipeline otherwise).
3. Update `worker` and `api` to `<sha>`, then `frontend`.

Old revisions keep running while new ones start, so migrations must be backward compatible with the
previous release (expand → deploy → contract). Use a single-revision mode unless you do blue/green.

## Scaling, probes and job leases

| App | Replicas | Probes |
|---|---|---|
| frontend | min 1, max 3+ (HTTP scale rule) | default TCP probes on 8080 |
| api | min 1 (avoid cold starts), max n | liveness `GET /api/v1/health/live`, readiness `GET /api/v1/health/ready` (checks DB), startup on live |
| worker | min 1, max n | none (no port); the platform restarts the container if the process exits |

* **API replicas** are stateless (sessions and OAuth state are in PostgreSQL). Rate limits are per
  process unless `RATE_LIMIT_BACKEND=database` (below); the template sets `database` whenever
  `apiMaxReplicas` is above 1.
* **Worker replicas** claim jobs with `SELECT … FOR UPDATE SKIP LOCKED`; a partial unique index
  ensures one running job per workflow run across all replicas. Each replica runs up to
  `WORKER_CONCURRENCY` jobs. Scale on CPU, or optionally with a KEDA `postgresql` scale rule on the
  number of queued jobs (not validated here).
* **Leases:** a claimed job records `locked_at`; there is no heartbeat. If a worker dies or is
  stopped mid-job, the job is re-queued once it is older than `JOB_LEASE_SECONDS` (or marked `dead`
  when attempts are exhausted; log event `jobs_recovered_after_lease_expiry`). Keep the lease well
  above the longest job (LLM timeout × retries × steps). Handlers are idempotent, and Azure DevOps
  writes are protected by the tag lookup, so re-running is safe — but it costs time and LLM tokens.
* **Shutdown:** on `SIGTERM` the worker stops claiming and waits up to 30 s for in-flight jobs. Set
  the worker's termination grace period above that (e.g. `--termination-grace-period 60`). The
  template's `workerTerminationGracePeriodSeconds` defaults to 60 and cannot be set to 30 or less.

> **Running more than one API replica.** Set `RATE_LIMIT_BACKEND=database` on every API replica, or enforce equivalent limits at the ingress/API gateway. With the default `memory`, counters are per process, so N replicas allow N× every limit; a production API on `memory` logs `rate_limits_are_per_process` at startup. Requirements: run the migrations first (creates `rate_limit_counters`); all replicas use the same `DATABASE_URL` and the same `AUDIT_HMAC_KEY` (counter keys are derived from it — replicas with different keys count separately); keep replica clocks NTP-synchronised (window boundaries come from each replica's wall clock); keep `FORWARDED_ALLOW_IPS` so per-IP limits see real client IPs. Load: per rate-limited request one indexed SELECT plus, when admitted, one upsert, each in its own short transaction; one DELETE of expired rows per replica per minute; keys are HMAC digests only. If the counter store fails, sign-in routes answer `429` (`Retry-After: 5`) and authenticated routes are served; alert on the `rate_limit_backend_unavailable` log event. The worker does not use rate limits.

In the template, every api replica gets the same `database-url` and `audit-hmac-key` references (one
app, one revision mode `Single`), and `RATE_LIMIT_BACKEND` is `database` whenever `apiMaxReplicas > 1`
(`tests/security/test_infrastructure_as_code.py` asserts it); the `migrate` stage runs before `apps`,
so the counters table exists first.

The template sets these probes on the api container (`tests/security/test_infrastructure_as_code.py`
compares them with the block below), the CPU scale rule on the worker and HTTP scale rules on the api
and frontend. With the CLI, probes need the YAML spec (export, edit, update). The api container's
probes:

```yaml
# az containerapp show -n nuvexa-api -g <rg> -o yaml > api.yaml ; add under
# properties.template.containers[0] ; az containerapp update -n nuvexa-api -g <rg> --yaml api.yaml
probes:
  - type: Startup
    httpGet: { path: /api/v1/health/live, port: 8000 }
    periodSeconds: 3
    failureThreshold: 40
  - type: Liveness
    httpGet: { path: /api/v1/health/live, port: 8000 }
    periodSeconds: 15
    failureThreshold: 3
  - type: Readiness
    httpGet: { path: /api/v1/health/ready, port: 8000 }
    periodSeconds: 10
    failureThreshold: 3
```

Do not point liveness at `/health/ready`: a database blip would restart healthy replicas.

## Telemetry

* **Logs:** stdout JSON → Log Analytics. The template sends the environment's logs through a
  diagnostic setting (`appLogsConfiguration.destination = azure-monitor`), so they land in the
  resource-specific tables `ContainerAppConsoleLogs` and `ContainerAppSystemLogs` (the
  `log-analytics` destination, which needs the workspace shared key, writes `ContainerAppConsoleLogs_CL`).
* **Traces:** the backend exports **OTLP over HTTP/protobuf** (`OTEL_EXPORTER_OTLP_ENDPOINT`, the SDK
  appends `/v1/traces`). The Container Apps *managed* OpenTelemetry agent (preview) accepts **gRPC
  only** and injects a gRPC `OTEL_EXPORTER_OTLP_ENDPOINT` into apps, so it is not compatible with
  this exporter as-is. Instead run an OpenTelemetry Collector container app (internal ingress,
  OTLP/HTTP receiver, target port 4318) with the `azuremonitor` exporter (Application Insights
  connection string from Key Vault), and set `OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector-app-name>`
  on api and worker. If the managed agent is enabled in the environment, set the variable explicitly
  — an explicit value overrides the injected one. The template does all of this when `enableTracing`
  is on: the collector (contrib distribution, which includes `azuremonitor`; import it into your
  registry with `az acr import` and pin it by digest in `otelCollectorImage`) reads its configuration
  from the `OTELCOL_CONFIG` variable (`--config=env:OTELCOL_CONFIG`) and the connection string from
  the `appinsights-connection-string` secret.
* **Langfuse:** point the collector (or the apps directly) at
  `https://cloud.langfuse.com/api/public/otel` with the Basic auth header. See
  [observability.md](observability.md).

## Network and security checklist

Items marked *(template)* are set by `infra/azure/main.bicep` and asserted by
`tests/security/test_infrastructure_as_code.py`; the others remain the operator's.

- [ ] *(template)* Only the frontend has external ingress; api and collector are internal; worker has none.
- [ ] HTTPS with a custom domain and managed certificate; HTTP redirected *(template: redirect, and
      the binding once `dashboardManagedCertificateName` is set)*.
- [ ] *(template)* Environment VNet-integrated; PostgreSQL private (delegated subnet, no firewall
      rules, public access disabled, TLS required); Key Vault behind a private endpoint with public
      access disabled. ACR private endpoint if your policy requires (the registry is not managed here).
- [ ] Outbound allowed to `login.microsoftonline.com`, `graph.microsoft.com`, `dev.azure.com` (and
      `*.visualstudio.com` as needed), your Azure OpenAI endpoint, and the telemetry backend; restrict
      everything else with NSGs/UDR and a firewall (not in the template: topology-specific).
- [ ] *(template)* Two user-assigned managed identities and three built-in roles. The apps' identity:
      **AcrPull** (registry), **Key Vault Secrets User** on each secret the apps reference,
      **Cognitive Services OpenAI User** (Azure OpenAI account). The `migrate` job's identity:
      **AcrPull** and **Key Vault Secrets User** on `database-migrate-url` only, which the apps'
      identity cannot read. No role on a resource group, subscription or the whole vault; no registry
      admin user, no keys in env vars. Only the worker's code can request tokens for the apps'
      identity (`identitySettings` lifecycle `Main`); for the frontend, api, collector and job the
      identity only pulls images and resolves Key Vault references (`None`).
- [ ] *(template)* All secrets as Key Vault references; none in images, YAML in git, or pipeline logs.
- [ ] `APP_ENV=production`, `DEMO_MODE=false`; `EXPOSE_API_DOCS` unset or `false`.
- [ ] `TOKEN_ENCRYPTION_KEYS` generated with `nuvexa gen-key`, stored in Key Vault, rotation runbook.
- [ ] Entra production app registration with only `https://` redirect URIs; tenant allow-list set.
- [ ] Limit who can read container app/job secrets: built-in Container Apps (Jobs) Contributor and
      Operator roles include `listSecrets`; starting a job also grants use of its secrets.
- [ ] *(template)* `RATE_LIMIT_BACKEND=database` when more than one API replica can run (or equivalent
      limits at the edge); alert on `rate_limit_backend_unavailable`.
- [ ] Backups/PITR on PostgreSQL; alerts on job failures (`job_failed`, dead jobs) and 5xx rates.

## Infrastructure as code (Bicep)

> **Not executed.** Nothing below has been run against a subscription by this repository. The
> template is compiled, linted, scanned and tested offline only (see
> [infra/azure/README.md](../infra/azure/README.md)).

[`infra/azure/main.bicep`](../infra/azure/main.bicep) codifies this document: network, private DNS,
Log Analytics, Application Insights, the two managed identities and their role assignments, Key Vault
with its private endpoint, PostgreSQL Flexible Server 18 with the `nuvexa` database and TLS
parameters, the Container Apps environment with its diagnostic setting, the `migrate` job and the
`frontend`, `api`, `worker` and `otel-collector` apps. `main.json` is the ARM template generated from
it (committed, so CI scans and tests exactly what would be deployed), and `main.bicepparam` holds the
parameters, placeholders only.

**Stages.** The rollout order above cannot be expressed in one ARM deployment (ARM cannot wait for a
job execution), so every deployment names its `stage`. Deploy in incremental mode only: a stage keeps
whatever a later stage created.

| `stage` | Adds | Then, before the next stage |
|---|---|---|
| `foundation` | everything except the per-secret Key Vault roles, the job and the apps | set every Key Vault secret value ([Secrets](#secrets-key-vault--container-app-secrets--secretref-env-vars)); create the database roles ([Database](#database)) |
| `migrate` | *Key Vault Secrets User* on each referenced secret (apps' identity; job identity on `database-migrate-url`), then the `migrate` job at `backendImageTag` | start the job and wait for `Succeeded`; stop the release otherwise |
| `apps` | `otel-collector`, then `worker` and `api` at the same `backendImageTag`, then `frontend` (ARM `dependsOn`) | first release only: bind the custom domain, then set `dashboardManagedCertificateName` |

Every release runs `migrate` and then `apps` with the new tags. The template does not handle the
PostgreSQL administrator password on releases: pass `NUVEXA_POSTGRES_ADMIN_PASSWORD` only to the
deployment that creates the server (or to rotate the password). Leaving it empty omits the property;
confirm on a test server that an update without it keeps the password before relying on this.

```bash
# Reference only — not executed. Placeholders in <...>. Azure CLI with the containerapp extension.
RG=<rg-nuvexa-prod>; ACR=<registry-name>; TAG=<git-sha>

# Images (BuildKit; --platform matters on Apple silicon). Collector: import and pin by digest.
az acr login -n $ACR
docker buildx build --platform linux/amd64 --push -t $ACR.azurecr.io/nuvexa-backend:$TAG .
docker buildx build --platform linux/amd64 --push -t $ACR.azurecr.io/nuvexa-frontend:$TAG ./frontend
az acr import -n $ACR --source docker.io/otel/opentelemetry-collector-contrib:<version>@sha256:<digest> \
  --image opentelemetry-collector-contrib:<version>

# Stage foundation (first deployment: also the PostgreSQL administrator password, from your secret store).
export NUVEXA_DEPLOY_STAGE=foundation NUVEXA_BACKEND_IMAGE_TAG=$TAG NUVEXA_FRONTEND_IMAGE_TAG=$TAG
NUVEXA_POSTGRES_ADMIN_PASSWORD="$(<print-it-from-your-secret-store>)" \
  az deployment group create -g $RG --mode Incremental -p infra/azure/main.bicepparam

# Secret values (names in the Secrets table), as a principal with Key Vault Secrets Officer, from a
# network that reaches the private endpoint. Never put values in files or shell history.
az keyvault secret set --vault-name <key-vault-name> -n token-encryption-keys --file <path-to-key-file>
# ... database-url (runtime role), database-migrate-url (schema owner), audit-hmac-key, entra-client-secret,
#     azure-devops-pat (pat mode), appinsights-connection-string (az monitor app-insights component show)
# Database roles (schema owner nuvexa_owner + DML-only runtime role nuvexa_app; never the server administrator
# in an application URL): the one-time procedure in "Database". The migrate job grants the runtime role.

# Stage migrate, then run the job and wait for it.
NUVEXA_DEPLOY_STAGE=migrate az deployment group create -g $RG --mode Incremental -p infra/azure/main.bicepparam
az containerapp job start -g $RG -n nuvexa-migrate
az containerapp job execution list -g $RG -n nuvexa-migrate -o table   # wait for Succeeded

# Stage apps.
NUVEXA_DEPLOY_STAGE=apps az deployment group create -g $RG --mode Incremental -p infra/azure/main.bicepparam

# First release only: custom domain and managed certificate (CNAME to the frontend FQDN and the asuid
# TXT record from the customDomainVerificationId output first), then set dashboardManagedCertificateName
# in main.bicepparam to the certificate's name for every later deployment.
az containerapp hostname add  -g $RG -n nuvexa-frontend --hostname <your-dashboard-host>
az containerapp hostname bind -g $RG -n nuvexa-frontend --hostname <your-dashboard-host> \
  --environment <cae-nuvexa> --validation-method CNAME
az containerapp env certificate list -g $RG -n <cae-nuvexa> --managed-certificates-only -o table   # its name
```

Role assignments can take a few minutes to propagate; if a stage fails to pull an image or resolve a
Key Vault reference right after its roles were created (AcrPull in `foundation`, the per-secret roles
in `migrate`), wait and rerun the stage.
