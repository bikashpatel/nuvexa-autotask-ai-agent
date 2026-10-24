# Azure DevOps setup

NUVEXA creates Azure DevOps **work items** from approved actions through the REST API
(`api-version` 7.1). Nothing is written until a person has approved the exact payload; see
[security-model.md](security-model.md).

## Modes

| `AZURE_DEVOPS_MODE` | Identity that creates work items | Credential | Use for |
|---|---|---|---|
| `mock` (default) | none — in-memory mock (organization `nuvexa-demo-org`) | none | demo, tests, UI work |
| `pat` | the PAT owner (a service/user account) | `AZURE_DEVOPS_PAT` (env / Key Vault, never stored in the database) | simplest real setup |
| `entra_delegated` | the user who connected Azure DevOps (their own permissions apply) | Entra delegated token, encrypted in `credential_store` | per-user attribution |

The **organization** is always configuration (`AZURE_DEVOPS_ORGANIZATION`); the model can never pick
an organization, URL or credential.

## Prerequisites

* An Azure DevOps Services organization (`https://dev.azure.com/<organization>`) and at least one
  project. Azure DevOps Server (on-premises) is not a tested target.
* The creating identity (PAT owner, or each delegated user) needs project access and permission to
  create and edit work items in the target **area path**.
* Know the project's process (Basic, Agile, Scrum, CMMI): available work item types differ (for
  example `Issue` in Basic, `User Story` in Agile, `Product Backlog Item` in Scrum).
* For `entra_delegated`: the organization must be connected to the same Microsoft Entra tenant as the
  app registration (Azure DevOps **Organization settings** → **Microsoft Entra**).

## PAT mode

Microsoft recommends Microsoft Entra tokens over PATs where possible. PAT mode is still the simplest
service-identity setup; keep the scope and lifetime minimal.

### Create the PAT

1. Sign in to `https://dev.azure.com/<organization>` as the account that should own the work items
   (ideally a dedicated account).
2. **User settings** (gear icon) → **Personal access tokens** → **+ New Token**.
3. **Name**: e.g. `nuvexa-work-items`. **Organization**: select this **one** organization (do not
   create a global "all accessible organizations" PAT — Azure DevOps stops accepting global PATs on
   December 1, 2026). **Expiration**: as short as your rotation process allows; your organization
   may enforce a maximum.
4. **Scopes**: **Custom defined** → **Work Items: Read & write** (`vso.work_write`). This covers
   work-item types, WIQL lookups, validation and creation.
5. **Create**, copy the token (it is shown once).

**Project listing.** **Test connection** and the project picker call the Projects API
(`GET _apis/projects`), which Microsoft documents under the **Project and Team: Read**
(`vso.project`) scope. If **Test connection** reports an authorization error while the PAT is valid,
add **Project and Team: Read** to the PAT. It is read-only; work-item writes still need only
`vso.work_write`. Without it, enter the project name in Settings / `AZURE_DEVOPS_DEFAULT_PROJECT`.

### Store and configure

```bash
AZURE_DEVOPS_MODE=pat
AZURE_DEVOPS_ORGANIZATION=<your-organization>      # name only, not the URL
AZURE_DEVOPS_PAT=<your-azure-devops-pat>
AZURE_DEVOPS_DEFAULT_PROJECT=<your-project>
```

* Local: `.env` (git-ignored). Production: an Azure Key Vault secret referenced by the container apps
  (see [deployment.md](deployment.md)). The PAT is never written to the database, logs or API
  responses.
* Settings are validated at startup: `pat` without `AZURE_DEVOPS_PAT`, or any non-mock mode without
  `AZURE_DEVOPS_ORGANIZATION`, fails fast.
* Rotation: create the new PAT, update the secret, restart/redeploy API and worker, then revoke the
  old PAT. Azure DevOps emails the owner before expiry.

### Invalid PAT symptom: HTTP 203 with an HTML page

When credentials are not accepted (expired, revoked, wrong organization, malformed), Azure DevOps
classically answers REST calls with `203 Non-Authoritative Information` and an **HTML sign-in page**
instead of `401`. NUVEXA sends `X-TFS-FedAuthRedirect: Suppress` to request a plain 401, and also
treats any 203, redirect, or `text/html` success response as an authentication failure, reported as:

> Azure DevOps returned a sign-in page: the PAT or access token is invalid or expired

Such failures are never retried. Check the PAT's status/expiry/organization on the
**Personal access tokens** page and replace it.

## Entra delegated mode

Work items are created with the connected user's own Azure DevOps identity and permissions.

1. In the Entra app registration add the delegated permission **Azure DevOps** →
   `user_impersonation` (resource `499b84ac-1321-427f-aa17-267ca6975798`), and grant consent if your
   tenant requires it — see [setup-microsoft-entra.md](setup-microsoft-entra.md#optional-azure-devops-delegated-permission).
   Microsoft marks this scope as high privilege; Azure DevOps still enforces the user's permissions.
2. Configure:

   ```bash
   AZURE_DEVOPS_MODE=entra_delegated
   AZURE_DEVOPS_ORGANIZATION=<your-organization>
   TOKEN_ENCRYPTION_KEYS=<fernet-key>          # tokens are stored encrypted
   # plus the ENTRA_* sign-in settings
   ```

3. Each user who executes Azure DevOps actions connects once: **Integrations** → **Connect Azure
   DevOps** (`GET /api/v1/integrations/azure-devops/connect`). The token is stored encrypted and
   refreshed silently.

Personal Microsoft accounts are not supported for the Azure DevOps resource through Entra apps; use
work or school accounts.

## Defaults, allow-lists and field policy

Deployment defaults come from the environment and seed the tenant settings the first time a tenant's
settings are created:

| Variable | Default | Tenant setting (Settings page → Azure DevOps) |
|---|---|---|
| `AZURE_DEVOPS_DEFAULT_PROJECT` | unset | `project` |
| `AZURE_DEVOPS_DEFAULT_WORK_ITEM_TYPE` | `Task` | `work_item_type` |
| `AZURE_DEVOPS_DEFAULT_AREA_PATH` | unset (project root) | `area_path` |
| `AZURE_DEVOPS_DEFAULT_ITERATION_PATH` | unset (project default) | `iteration_path` |

After that, admins change them on the **Settings** page (stored per tenant in `app_settings`;
changing the environment variables later does not overwrite saved tenant settings). The same page
holds the policy that bounds every proposal (`app/schemas/settings.py::AzureDevOpsDefaults`):

* `allowed_projects` — empty means any project the connection can see; otherwise only these.
* `allowed_work_item_types` — default `Bug`, `Task`, `User Story`, `Issue`, `Product Backlog Item`,
  `Epic`, `Feature`.
* `category_work_item_types` — category → type mapping (e.g. `incident`/`bug` → `Bug`,
  `feature_request` → `User Story`).
* `allowed_additional_fields` — field reference names the planner or reviewer may set besides title,
  description, priority and tags (default `Microsoft.VSTS.Common.Severity`,
  `Microsoft.VSTS.TCM.ReproSteps`). Anything else is rejected.
* Priority mapping (Settings → priorities): NUVEXA priority → `Microsoft.VSTS.Common.Priority` 1–4.

Proposals that reference an unlisted project, type or field fail validation before they can be
approved.

## Idempotency tags

Azure DevOps has no server-side idempotency key, so NUVEXA tags each created work item with
`nuvexa-<first 16 hex characters of the proposal's idempotency key>`, e.g. `nuvexa-1a2b3c4d5e6f7a8b`.

1. **Before create**: a WIQL query (`[System.TeamProject] = … AND [System.Tags] CONTAINS 'nuvexa-…'`)
   looks for an existing item; exact tag match is required. Found → the existing item is linked,
   nothing new is created.
2. **Create**: connect errors, `429` and `5xx` are retried with bounded exponential backoff and
   jitter (honouring `Retry-After`), up to `AZURE_DEVOPS_MAX_RETRIES`. `400/401/403/404` are never
   retried.
3. **Ambiguous failure** (timeout or dropped connection after the request may have been sent): the
   tag is looked up again. Found → success; confirmed absent → safe retry; lookup failed → the action
   becomes **uncertain** and a person uses **Reconcile** later.

Limitations — leave the tag on the work item:

* Deleting or renaming the `nuvexa-…` tag, or deleting the work item, between an ambiguous failure
  and the reconcile lookup can lead to a duplicate on retry.
* WIQL results can lag briefly behind a create; a very fast re-lookup might miss a just-created item.
  This is why ambiguous outcomes become `uncertain` instead of being retried blindly.
* The lookup is scoped to the target project; moving the item to another project hides it.

## Testing the connection

* **Settings / Integrations page → Test connection** calls
  `GET /api/v1/integrations/azure-devops/test` and reports mode, organization, latency and a message.
* **Projects and types**: `GET /api/v1/integrations/azure-devops/projects` and
  `.../projects/{project}/work-item-types` populate the pickers.
* **Validate a payload without creating anything**: `POST /api/v1/integrations/azure-devops/validate`
  with `{"payload": {...}, "server_side": true}` runs local policy checks and, with
  `server_side: true`, Azure DevOps' own `validateOnly=true` create. Use it to check area/iteration
  paths and required fields of a custom process before approving anything.

Recommended first run against a real organization: a dedicated test project, `Test connection`,
`validate`, then one approved action; confirm the work item and its `nuvexa-…` tag in Azure DevOps.

## Mock mode

`AZURE_DEVOPS_MODE=mock` (default) makes no network calls. The mock adapter (`app/integrations/azure_devops/mock.py`)
uses organization `nuvexa-demo-org`, projects `NUVEXA-Demo` and `Customer-Portal`, work item ids from
1001 and `mock://` URLs, and reports itself as "Mock Azure DevOps adapter (in-memory, no external
calls)". The same tag-based idempotency path runs, so approve → execute → audit behaves like the real
adapter. The store is in memory, per process: created mock items disappear when the worker restarts.
