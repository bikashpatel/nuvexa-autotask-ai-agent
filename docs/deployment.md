# Deployment — Azure Container Apps reference architecture

> **Reference only.** This repository does not deploy anything and creates no cloud resources. The
> architecture and the `az` commands below are illustrative; they have **not been executed or
> verified** by this repository. Review them against current Microsoft documentation, your network
> and security policies, and prefer infrastructure as code (Bicep/Terraform) for real environments.

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
| Container Apps environment | Hosts apps and the job | Workload-profiles environment with VNet integration; logs to Log Analytics |
| `frontend` container app | nginx, static dashboard, proxies `/api/` | Only app with **external** ingress (HTTPS, custom domain + managed certificate) |
| `api` container app | `nuvexa api`, port 8000 | **Internal** ingress only; reached through the frontend proxy |
| `worker` container app | `nuvexa worker` | No ingress; scales 1..n |
| `migrate` Container Apps job | `nuvexa migrate` | Manual trigger, run before each rollout |
| Azure Database for PostgreSQL Flexible Server | System of record, job queue, LangGraph checkpoints | PostgreSQL 18 is supported on Flexible Server; private access |
| Azure Key Vault | Secrets | RBAC mode; apps read via managed identity (**Key Vault Secrets User**) |
| Azure Container Registry | Images | Pull via managed identity (**AcrPull**); admin user disabled |
| Log Analytics | Container console logs (structured JSON) | Environment-level |
| Application Insights | Traces | Through an OpenTelemetry Collector (see [Telemetry](#telemetry)) |
| User-assigned managed identity | Shared by apps and the job | AcrPull, Key Vault Secrets User, Cognitive Services OpenAI User |

## Images and configuration

* Build both images in CI (already done without push in `.github/workflows/ci.yml`), push to ACR
  with an immutable tag (git SHA). Deploy the **same backend tag** to `migrate`, `api` and `worker`.
* All configuration is environment variables (see `.env.example`). Production values:

| Variable | Value |
|---|---|
| `APP_ENV` | `production` (requires `TOKEN_ENCRYPTION_KEYS`; secure cookies; demo login disabled; API docs off by default) |
| `DEMO_MODE` | `false` |
| `APP_BASE_URL` | `https://<your-dashboard-host>` |
| `OAUTH_REDIRECT_URI` | `https://<your-dashboard-host>/api/v1/auth/callback` (registered in Entra) |
| `MAIL_MODE` | `graph` |
| `LLM_PROVIDER` | `azure_openai` with `AZURE_OPENAI_USE_ENTRA_ID=true` and `AZURE_CLIENT_ID=<managed identity client id>` |
| `AZURE_DEVOPS_MODE` | `pat` or `entra_delegated` |
| `ENTRA_ALLOWED_TENANT_IDS`, `BOOTSTRAP_ADMIN_EMAILS` | set explicitly |
| `RUN_EMBEDDED_WORKER` | `false` (dedicated worker app) |
| `LOG_FORMAT` | `json` |
| `FORWARDED_ALLOW_IPS` (api) | the environment's infrastructure subnet CIDR, or `*` when the api app has internal ingress only |
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT` | `true`, `http://<otel-collector-app>` (see Telemetry) |

### Secrets (Key Vault → container app secrets → `secretref:` env vars)

| Key Vault secret | Env var | Needed when |
|---|---|---|
| `database-url` | `DATABASE_URL` | always (`postgresql+psycopg://<user>:<url-encoded password>@<server>.postgres.database.azure.com:5432/nuvexa?sslmode=require`) |
| `token-encryption-keys` | `TOKEN_ENCRYPTION_KEYS` | always in production |
| `entra-client-secret` | `ENTRA_CLIENT_SECRET` | Entra sign-in |
| `azure-devops-pat` | `AZURE_DEVOPS_PAT` | `AZURE_DEVOPS_MODE=pat` |
| `azure-openai-api-key` | `AZURE_OPENAI_API_KEY` | only if not using managed identity |
| `openai-api-key` | `OPENAI_API_KEY` | `LLM_PROVIDER=openai` |
| `otlp-headers` | `OTEL_EXPORTER_OTLP_HEADERS` | exporter needs auth (e.g. Langfuse) |

Use versionless Key Vault secret URIs to pick up rotations: Container Apps fetches the latest version
within about 30 minutes and restarts revisions that reference it in env vars. Pin a version when you
need explicit control. PostgreSQL authentication uses a password (in `database-url`); Entra
token-based database authentication is not supported by the current configuration.

## Frontend → API routing

The frontend image proxies `/api/` to `http://api:8000`, which matches Docker Compose. In Container
Apps an app with internal ingress is reached inside the environment at `http://<app-name>` (ingress
port 80 → target port 8000) or `https://<app-name>.internal.<environment-default-domain>`, not on
port 8000. The frontend image therefore needs a configurable upstream for this deployment (for
example an nginx template rendered from an environment variable), and the proxied request's `Host`
header must be the API app's name/FQDN so the environment's ingress routes it; forward the original
host in `X-Forwarded-Host`. Keep everything same-origin: the session cookie and the OAuth callback
live on the dashboard host. The image implements this with `API_UPSTREAM` (default `http://api:8000`).

**Client IP integrity.** The API uses the forwarded client address for login rate limiting and audit
records, so nginx *overwrites* `X-Forwarded-For` with `$remote_addr` instead of appending a
client-supplied value. When another proxy terminates traffic first (the Container Apps ingress),
`$remote_addr` is that proxy: add `set_real_ip_from <ingress/infrastructure subnet>;` and
`real_ip_header X-Forwarded-For;` to the nginx config so `$remote_addr` becomes the real client before
it is forwarded, and keep `FORWARDED_ALLOW_IPS` on the API restricted to the frontend's network.

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
`--env-vars DATABASE_URL=secretref:database-migrate-url DATABASE_RUNTIME_ROLE=nuvexa_app` (plus the other
shared variables). Only the job may reference `database-migrate-url`: give the job **its own** user-assigned
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

* **API replicas** are stateless (sessions and OAuth state are in PostgreSQL). Rate limiting is
  in-process **per replica**; put a shared limit at the edge (Azure Front Door / API Management) if
  that matters.
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
  the worker's termination grace period above that (e.g. `--termination-grace-period 60`).

Probes need the YAML spec (export, edit, update). Example for the api container:

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

* **Logs:** stdout JSON → Log Analytics (`ContainerAppConsoleLogs_CL` / `ContainerAppConsoleLogs`).
* **Traces:** the backend exports **OTLP over HTTP/protobuf** (`OTEL_EXPORTER_OTLP_ENDPOINT`, the SDK
  appends `/v1/traces`). The Container Apps *managed* OpenTelemetry agent (preview) accepts **gRPC
  only** and injects a gRPC `OTEL_EXPORTER_OTLP_ENDPOINT` into apps, so it is not compatible with
  this exporter as-is. Instead run an OpenTelemetry Collector container app (internal ingress,
  OTLP/HTTP receiver, target port 4318) with the `azuremonitor` exporter (Application Insights
  connection string from Key Vault), and set `OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector-app-name>`
  on api and worker. If the managed agent is enabled in the environment, set the variable explicitly
  — an explicit value overrides the injected one.
* **Langfuse:** point the collector (or the apps directly) at
  `https://cloud.langfuse.com/api/public/otel` with the Basic auth header. See
  [observability.md](observability.md).

## Network and security checklist

- [ ] Only the frontend has external ingress; api and collector are internal; worker has none.
- [ ] HTTPS with a custom domain and managed certificate; HTTP redirected.
- [ ] Environment VNet-integrated; PostgreSQL private; Key Vault and ACR private endpoints if your
      policy requires; outbound allowed to `login.microsoftonline.com`, `graph.microsoft.com`,
      `dev.azure.com` (and `*.visualstudio.com` as needed), your Azure OpenAI endpoint, and the
      telemetry backend.
- [ ] One user-assigned managed identity: **AcrPull** (registry), **Key Vault Secrets User** (vault),
      **Cognitive Services OpenAI User** (Azure OpenAI). No registry admin user, no keys in env vars.
- [ ] All secrets as Key Vault references; none in images, YAML in git, or pipeline logs.
- [ ] `APP_ENV=production`, `DEMO_MODE=false`; `EXPOSE_API_DOCS` unset or `false`.
- [ ] `TOKEN_ENCRYPTION_KEYS` generated with `nuvexa gen-key`, stored in Key Vault, rotation runbook.
- [ ] Entra production app registration with only `https://` redirect URIs; tenant allow-list set.
- [ ] Limit who can read container app/job secrets: built-in Container Apps (Jobs) Contributor and
      Operator roles include `listSecrets`; starting a job also grants use of its secrets.
- [ ] Shared rate limiting at the edge if you run multiple API replicas.
- [ ] Backups/PITR on PostgreSQL; alerts on job failures (`job_failed`, dead jobs) and 5xx rates.

## Illustrative `az` CLI sequence

> **Reference — not executed or verified by this repository.** Placeholders in `<...>`. Requires the
> Azure CLI with the `containerapp` extension. Networking flags (VNet, subnets, private DNS) are
> abbreviated; complete them for your topology.

```bash
# --- variables --------------------------------------------------------------------------------
RG=<rg-nuvexa-prod>; LOC=<region>; ENV=<cae-nuvexa-prod>
ACR=<acrnuvexaprod>; KV=<kv-nuvexa-prod>; PG=<pg-nuvexa-prod>; ID=<id-nuvexa-prod>
TAG=<git-sha>

az group create -n $RG -l $LOC

# --- registry and images ----------------------------------------------------------------------
az acr create -g $RG -n $ACR --sku Standard --admin-enabled false
az acr login -n $ACR
# BuildKit is required (cache mounts); --platform matters when building on Apple silicon.
docker buildx build --platform linux/amd64 --push -t $ACR.azurecr.io/nuvexa-backend:$TAG .
docker buildx build --platform linux/amd64 --push -t $ACR.azurecr.io/nuvexa-frontend:$TAG ./frontend

# --- identity and RBAC ------------------------------------------------------------------------
az identity create -g $RG -n $ID
ID_RES=$(az identity show -g $RG -n $ID --query id -o tsv)
ID_PRINCIPAL=$(az identity show -g $RG -n $ID --query principalId -o tsv)
ID_CLIENT=$(az identity show -g $RG -n $ID --query clientId -o tsv)
az role assignment create --assignee-object-id $ID_PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope $(az acr show -n $ACR --query id -o tsv)

# --- Key Vault (RBAC) and secrets -------------------------------------------------------------
az keyvault create -g $RG -n $KV --enable-rbac-authorization true
az role assignment create --assignee-object-id $ID_PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" --scope $(az keyvault show -n $KV --query id -o tsv)
az keyvault secret set --vault-name $KV -n database-url          --value '<postgresql+psycopg://...?sslmode=require>'
az keyvault secret set --vault-name $KV -n token-encryption-keys --value '<output of nuvexa gen-key>'
az keyvault secret set --vault-name $KV -n entra-client-secret   --value '<client-secret-value>'
az keyvault secret set --vault-name $KV -n azure-devops-pat      --value '<pat>'   # pat mode only
KVURI=https://$KV.vault.azure.net/secrets

# --- PostgreSQL Flexible Server ---------------------------------------------------------------
az postgres flexible-server create -g $RG -n $PG -l $LOC --version 18 \
  --tier GeneralPurpose --sku-name <Standard_D2ds_v5> --storage-size 64 \
  --admin-user <pgadmin> --admin-password '<strong-password>' \
  --vnet <vnet-name> --subnet <postgres-delegated-subnet> --private-dns-zone <zone>
az postgres flexible-server db create -g $RG -s $PG -d nuvexa
# then create a least-privilege application role that owns database "nuvexa" (psql), and put its
# credentials in the database-url secret.

# --- Log Analytics + Container Apps environment -----------------------------------------------
az monitor log-analytics workspace create -g $RG -n <log-nuvexa-prod>
LAW_ID=$(az monitor log-analytics workspace show -g $RG -n <log-nuvexa-prod> --query customerId -o tsv)
LAW_KEY=$(az monitor log-analytics workspace get-shared-keys -g $RG -n <log-nuvexa-prod> --query primarySharedKey -o tsv)
az containerapp env create -g $RG -n $ENV -l $LOC \
  --logs-workspace-id $LAW_ID --logs-workspace-key $LAW_KEY \
  --infrastructure-subnet-resource-id <aca-subnet-resource-id>

# --- shared settings --------------------------------------------------------------------------
SECRETS=(
  "database-url=keyvaultref:$KVURI/database-url,identityref:$ID_RES"
  "token-encryption-keys=keyvaultref:$KVURI/token-encryption-keys,identityref:$ID_RES"
  "entra-client-secret=keyvaultref:$KVURI/entra-client-secret,identityref:$ID_RES"
  "azure-devops-pat=keyvaultref:$KVURI/azure-devops-pat,identityref:$ID_RES"
)
ENVVARS=(
  APP_ENV=production DEMO_MODE=false LOG_FORMAT=json RUN_EMBEDDED_WORKER=false
  APP_BASE_URL=https://<your-dashboard-host>
  OAUTH_REDIRECT_URI=https://<your-dashboard-host>/api/v1/auth/callback
  MAIL_MODE=graph ENTRA_TENANT_ID=<tenant-id> ENTRA_ALLOWED_TENANT_IDS=<tenant-id>
  ENTRA_CLIENT_ID=<client-id> BOOTSTRAP_ADMIN_EMAILS=<admin@your-domain.example>
  LLM_PROVIDER=azure_openai AZURE_OPENAI_USE_ENTRA_ID=true AZURE_CLIENT_ID=$ID_CLIENT
  AZURE_OPENAI_ENDPOINT=https://<aoai>.openai.azure.com AZURE_OPENAI_DEPLOYMENT=<deployment>
  AZURE_OPENAI_API_VERSION=<api-version>
  AZURE_DEVOPS_MODE=pat AZURE_DEVOPS_ORGANIZATION=<org> AZURE_DEVOPS_DEFAULT_PROJECT=<project>
  DATABASE_URL=secretref:database-url TOKEN_ENCRYPTION_KEYS=secretref:token-encryption-keys
  ENTRA_CLIENT_SECRET=secretref:entra-client-secret AZURE_DEVOPS_PAT=secretref:azure-devops-pat
)
IMAGE=$ACR.azurecr.io/nuvexa-backend:$TAG

# --- migrations job (run before every rollout) ------------------------------------------------
az containerapp job create -g $RG -n nuvexa-migrate --environment $ENV \
  --trigger-type Manual --replica-timeout 900 --replica-retry-limit 0 \
  --replica-completion-count 1 --parallelism 1 \
  --image $IMAGE --cpu 0.5 --memory 1.0Gi --command nuvexa --args migrate \
  --registry-server $ACR.azurecr.io --registry-identity $ID_RES --mi-user-assigned $ID_RES \
  --secrets "${SECRETS[@]}" --env-vars "${ENVVARS[@]}"
az containerapp job start -g $RG -n nuvexa-migrate
az containerapp job execution list -g $RG -n nuvexa-migrate -o table   # wait for Succeeded

# --- api (internal ingress) -------------------------------------------------------------------
az containerapp create -g $RG -n nuvexa-api --environment $ENV --image $IMAGE \
  --ingress internal --target-port 8000 --min-replicas 1 --max-replicas 3 --cpu 0.5 --memory 1.0Gi \
  --registry-server $ACR.azurecr.io --registry-identity $ID_RES --user-assigned $ID_RES \
  --secrets "${SECRETS[@]}" --env-vars "${ENVVARS[@]}" FORWARDED_ALLOW_IPS='*'
# add the probes from the YAML example above (az containerapp update --yaml)

# --- worker (no ingress) ----------------------------------------------------------------------
az containerapp create -g $RG -n nuvexa-worker --environment $ENV --image $IMAGE \
  --command nuvexa --args worker --min-replicas 1 --max-replicas 3 --cpu 0.5 --memory 1.0Gi \
  --termination-grace-period 60 \
  --registry-server $ACR.azurecr.io --registry-identity $ID_RES --user-assigned $ID_RES \
  --secrets "${SECRETS[@]}" --env-vars "${ENVVARS[@]}"

# --- frontend (external ingress) --------------------------------------------------------------
az containerapp create -g $RG -n nuvexa-frontend --environment $ENV \
  --image $ACR.azurecr.io/nuvexa-frontend:$TAG \
  --ingress external --target-port 8080 --min-replicas 1 --max-replicas 3 --cpu 0.25 --memory 0.5Gi \
  --registry-server $ACR.azurecr.io --registry-identity $ID_RES --user-assigned $ID_RES
# + upstream configuration for the api app (see "Frontend → API routing"), custom domain and
#   managed certificate (az containerapp hostname add / bind).

# --- later releases ---------------------------------------------------------------------------
az containerapp job update -g $RG -n nuvexa-migrate --image $ACR.azurecr.io/nuvexa-backend:<new-sha>
az containerapp job start  -g $RG -n nuvexa-migrate      # wait for Succeeded, then:
az containerapp update -g $RG -n nuvexa-worker   --image $ACR.azurecr.io/nuvexa-backend:<new-sha>
az containerapp update -g $RG -n nuvexa-api      --image $ACR.azurecr.io/nuvexa-backend:<new-sha>
az containerapp update -g $RG -n nuvexa-frontend --image $ACR.azurecr.io/nuvexa-frontend:<new-sha>
```

Also grant the managed identity **Cognitive Services OpenAI User** on the Azure OpenAI resource, and
deploy the OpenTelemetry Collector app described in [Telemetry](#telemetry). See
[infra/azure/README.md](../infra/azure/README.md) for notes on turning this into Bicep/Terraform.
