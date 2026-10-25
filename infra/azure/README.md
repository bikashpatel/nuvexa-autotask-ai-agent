# infra/azure — Bicep for the Azure Container Apps reference architecture

**Not deployed.** Nothing in this repository creates cloud resources, and this template has never been
deployed by it. It is compiled, linted, scanned and tested **offline only**. The architecture, the
secrets, the rollout order and the operator steps around the template are in
[docs/deployment.md](../../docs/deployment.md).

| File | What it is |
|---|---|
| `main.bicep` | The template: every resource of the reference architecture, one deployment per `stage` |
| `modules/acr-pull-role.bicep`, `modules/openai-user-role.bicep` | The two role assignments that live in other resource groups (registry, Azure OpenAI) |
| `main.bicepparam` | Parameters, placeholders only (`<...>` values are invalid on purpose); per-release values come from environment variables |
| `main.json` | **Generated** ARM template (`bicep build main.bicep`). Committed so CI scans and tests exactly what would be deployed. Never edit it by hand |
| `bicepconfig.json` | Linter configuration: security-relevant rules raised to errors (the CI check fails on any diagnostic) |
| `.trivyignore.yaml` | One registered Trivy false positive (SEC-EX-007), limited to one check and `main.json` |

## What the template creates

Virtual network (Container Apps infrastructure subnet, PostgreSQL delegated subnet, private-endpoint
subnet); private DNS zones for PostgreSQL and Key Vault; Log Analytics (shared-key ingestion off);
Application Insights (workspace-based); two user-assigned managed identities (the apps', the
`migrate` job's) holding three built-in roles, each scoped to one resource or one secret; Key Vault
(RBAC, purge protection, public access disabled,
private endpoint, no secret values); PostgreSQL Flexible Server 18 (private access, TLS required,
password administrator, zone-redundant HA, database `nuvexa`); the Container Apps environment
(workload profiles, VNet-integrated, logs through a diagnostic setting, peer-to-peer encryption); the
`migrate` job; the `otel-collector`, `worker`, `api` and `frontend` apps.

It references, but does not create or change, the **container registry** and the **Azure OpenAI
account** (parameters; they can live in other resource groups or subscriptions).

`stage` selects what a deployment adds, following the documented rollout order:
`foundation` (everything but the per-secret roles, the job and the apps) → set every secret, create
the database roles → `migrate` (Key Vault Secrets User per secret, then the job at `backendImageTag`)
→ run the job, wait for `Succeeded` → `apps` (collector, then worker and api at the same tag, then
frontend). Deploy in **incremental** mode only.

## Properties to keep

Each is asserted on the evaluated template by
[`tests/security/test_infrastructure_as_code.py`](../../tests/security/test_infrastructure_as_code.py)
(for every stage, both Azure DevOps modes and with tracing on and off):

* Exactly three built-in roles. The apps' identity: **AcrPull** (registry), **Key Vault Secrets User**
  on each secret the apps reference (never the vault) and **Cognitive Services OpenAI User** (Azure
  OpenAI account). The `migrate` job's own identity: **AcrPull** and **Key Vault Secrets User** on
  `database-migrate-url` only, which the apps' identity cannot read. Only the worker's code can request
  tokens (`identitySettings` lifecycle `Main`); elsewhere the identity only pulls images and resolves
  Key Vault references (`None`). No registry passwords.
* Every app and job secret is a versionless Key Vault reference (`keyVaultUrl` + the identity), never a
  `value`; every secret-like environment variable is a `secretRef`; no `list*()`/`getSecret()` in the
  template; the secure parameter has no default and comes from the environment.
* `frontend` is the only external ingress (HTTP redirected to HTTPS); `api` internal on target port
  8000 with exactly the documented startup/liveness/readiness probes; `otel-collector` internal on
  4318; `worker` no ingress, termination grace above 30 s (parameter minimum 31).
* The `migrate` job (manual trigger, no retries, `nuvexa migrate`) runs the same backend image as `api`
  and `worker`, holds only `database-migrate-url` (schema owner), and the apps never hold it; the job,
  `api` and `worker` set `DATABASE_RUNTIME_ROLE` (`nuvexa_app`, the role in `database-url`).
* Production settings use the real names from `app/core/config.py` (`APP_ENV=production`,
  `DEMO_MODE=false`, `AZURE_OPENAI_USE_ENTRA_ID=true`, ...); `FORWARDED_ALLOW_IPS` is the
  infrastructure subnet; the api uses `RATE_LIMIT_BACKEND=database` whenever `apiMaxReplicas > 1`.
* Key Vault: RBAC, purge protection, public access disabled, firewall default `Deny` without bypass,
  private endpoint. PostgreSQL 18: private access, no firewall rules, `require_secure_transport=ON`,
  TLS ≥ 1.2. Environment: VNet-integrated, logs without workspace keys.
* No GUIDs other than the three built-in role ids, no hosts, e-mail addresses or credential shapes in
  the sources or parameters.

## Offline validation

Requirements: Docker, `uv`, `python3` and network access to download the pinned tools once.

```bash
scripts/security-scan.sh config   # Bicep lint, build-params, main.json in sync, Trivy config, actionlint, zizmor, exceptions
uv run pytest tests/security/test_infrastructure_as_code.py tests/security/test_ci_pipeline_contract.py -q
```

The config stage downloads the Bicep CLI release binary for the Docker host's architecture into
`~/.cache/nuvexa-bicep/<version>/` (override with `BICEP_CACHE`), checks it against the SHA-256 sums in
`scripts/security-scan.sh` before every run, and runs it with `--network none` in the pinned
`mcr.microsoft.com/dotnet/runtime-deps` image. It fails on any lint diagnostic, on an invalid parameter
file and when `main.json` differs from a fresh build (the diff is in `reports/security/bicep-main-json.diff`).

**Regenerate `main.json`** after editing any `.bicep` file, with the same pinned CLI (run the config
stage once first so the verified binary is cached; use `bicep-linux-x64` on x86-64 Docker hosts):

```bash
docker run --rm --network none -e DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
  -v "$HOME/.cache/nuvexa-bicep/0.48.1/bicep-linux-arm64:/usr/local/bin/bicep:ro" -v "$PWD:/src:ro" -w /src \
  mcr.microsoft.com/dotnet/runtime-deps:10.0.12-noble-chiseled@sha256:dc5cd0c7d0a39b825312b4f0986d99b9adf9b6a3af69bc23cd345c27d9ed916a \
  /usr/local/bin/bicep build infra/azure/main.bicep --stdout > infra/azure/main.json
```

Updating the Bicep CLI or its image: pick a release at least 7 days old (docs/CI_SECURITY_GATES.md),
update `BICEP_VERSION`, both `BICEP_SHA256_LINUX_*` sums (from the release assets) and `BICEP_IMAGE`
(tag and digest) together, and regenerate `main.json` (the test checks the generator version).

**What the offline checks cannot tell you:** whether a real deployment succeeds (quotas, region and
zone support, API behaviour, RBAC propagation), whether Key Vault references resolve through the
private endpoint in your network, and whether omitting the administrator password on an update keeps it.
Trivy's ARM support covers Key Vault here but not Container Apps or PostgreSQL Flexible Server; those
properties rest on the tests above.

## Deployment (reference only, not executed)

```bash
# Per stage; see docs/deployment.md "Infrastructure as code (Bicep)" for secrets, database roles and the job run.
export NUVEXA_BACKEND_IMAGE_TAG=<git-sha> NUVEXA_FRONTEND_IMAGE_TAG=<git-sha>
NUVEXA_DEPLOY_STAGE=foundation az deployment group create -g <rg> --mode Incremental -p infra/azure/main.bicepparam
NUVEXA_DEPLOY_STAGE=migrate    az deployment group create -g <rg> --mode Incremental -p infra/azure/main.bicepparam
az containerapp job start -g <rg> -n nuvexa-migrate      # wait for Succeeded before the next line
NUVEXA_DEPLOY_STAGE=apps       az deployment group create -g <rg> --mode Incremental -p infra/azure/main.bicepparam
```

## Deliberately not included

* **The container registry and Azure OpenAI account**: shared, long-lived resources usually owned by
  another team; the template only assigns roles on them. Disable the registry admin user and add
  private endpoints there if your policy requires.
* **Secret values and database roles**: values are set out of band (the template never handles them);
  ARM cannot create PostgreSQL roles (docs/deployment.md, Database).
* **Custom domain certificate issuance**: managed certificates need DNS validation after the first
  `apps` deployment (`az containerapp hostname bind`); the template then keeps the binding through
  `dashboardManagedCertificateName`.
* **Egress control** (NSGs, UDR, Azure Firewall with the documented FQDN allow-list), **edge rate
  limiting** (Front Door / API Management), **alerts** and **Key Vault/ACR diagnostic settings**:
  topology- and organisation-specific.
* **Key-based LLM access** (`azure-openai-api-key`, `openai-api-key`) and **direct OTLP export with
  headers** (`otlp-headers`): the template wires the keyless Azure OpenAI path and the collector only.
* **A KEDA `postgresql` scale rule** for the worker (not validated; the worker scales on CPU).

`infra/postgres/initdb/` is used only by the local `docker-compose.yml`.
