# Microsoft Entra ID setup (sign-in and Outlook mailbox access)

NUVEXA uses one Microsoft Entra app registration for three delegated flows, all authorization code
with PKCE, handled server-side by MSAL:

| Flow | When | Scopes requested |
|---|---|---|
| Sign-in | "Sign in with Microsoft" | `openid profile email offline_access User.Read` |
| Mailbox connection | Integrations → connect Microsoft mailbox (incremental consent) | `Mail.Read` (+ `offline_access`) |
| Azure DevOps (optional) | Only with `AZURE_DEVOPS_MODE=entra_delegated` | `499b84ac-1321-427f-aa17-267ca6975798/user_impersonation` |

Tokens are redeemed and stored by the backend (encrypted with `TOKEN_ENCRYPTION_KEYS`); the browser
never sees them. Demo mode does not need any of this.

Portal labels below follow Microsoft Learn as of October 2026
([register an app](https://learn.microsoft.com/entra/identity-platform/quickstart-register-app),
[redirect URIs](https://learn.microsoft.com/entra/identity-platform/how-to-add-redirect-uri),
[credentials](https://learn.microsoft.com/entra/identity-platform/how-to-add-credentials),
[API permissions](https://learn.microsoft.com/entra/identity-platform/quickstart-configure-app-access-web-apis)).
The portal changes occasionally; if a label differs, those pages are authoritative.

## Prerequisites

* A Microsoft Entra tenant with Exchange Online mailboxes (Microsoft 365 work or school accounts).
* At least the **Application Developer** role to create the registration.
* For tenant-wide admin consent: an administrator role allowed to grant it (see Microsoft Learn,
  "Grant tenant-wide admin consent to an application").

Use **separate registrations for development and production** so localhost redirect URIs never
exist on the production app.

## 1. Register the application

1. Sign in to the [Microsoft Entra admin center](https://entra.microsoft.com).
2. If you have several tenants, switch to the right one with the **Settings** icon in the top menu.
3. Browse to **Entra ID** → **App registrations** → **New registration**.
4. **Name**: for example `NUVEXA AutoTask (dev)`.
5. **Supported account types**:
   * **Single tenant only - &lt;your tenant&gt;** (recommended for internal use). Set
     `ENTRA_TENANT_ID=<directory-tenant-id>`.
   * **Multiple Entra ID tenants** if several organizations will sign in. Set
     `ENTRA_TENANT_ID=organizations` **and** restrict sign-in with `ENTRA_ALLOWED_TENANT_IDS`.
   * Do not choose the options that include personal Microsoft accounts; NUVEXA targets work or
     school mailboxes, and Microsoft Entra apps do not support personal accounts for the Azure DevOps
     resource.
6. Select **Register**. On the **Overview** page record:
   * **Application (client) ID** → `ENTRA_CLIENT_ID`
   * **Directory (tenant) ID** → `ENTRA_TENANT_ID` (single tenant) and/or `ENTRA_ALLOWED_TENANT_IDS`

## 2. Add redirect URIs (Web platform)

The backend receives the authorization code, so the platform must be **Web** (not "Single-page
application").

1. In the app registration, under **Manage**, select **Authentication**.
2. On **Redirect URI configuration**, select **Add Redirect URI** → **Web**.
3. Add the callback for each way you run the dashboard:

   | Environment | Redirect URI | `OAUTH_REDIRECT_URI` / `APP_BASE_URL` |
   |---|---|---|
   | Docker Compose | `http://localhost:8080/api/v1/auth/callback` | `http://localhost:8080/api/v1/auth/callback` / `http://localhost:8080` |
   | Vite dev server | `http://localhost:5173/api/v1/auth/callback` | defaults (`:5173`) |
   | Production | `https://<your-dashboard-host>/api/v1/auth/callback` | `https://<your-dashboard-host>/api/v1/auth/callback` / `https://<your-dashboard-host>` |

4. Select **Configure** / **Save**. Leave implicit-grant / hybrid-flow token checkboxes off.

Notes:

* `http://` is accepted only for `localhost`. Production must use `https://`.
* Matching is exact on scheme, host and path (paths are case-sensitive). For `localhost`, Microsoft
  ignores the port when matching, so the `:8080` entry also matches `:5173`; registering both is
  harmless because they share a platform and path. Microsoft recommends against registering
  localhost URIs that differ only by port, so one localhost entry is enough if you prefer.
* Compose derives `OAUTH_REDIRECT_URI` from `NUVEXA_WEB_PORT`; thanks to the localhost port rule a
  changed local port still matches the registered localhost entry.
* The value NUVEXA sends is exactly `OAUTH_REDIRECT_URI`. A mismatch fails with `AADSTS50011`
  (see [troubleshooting](troubleshooting.md#aadsts50011-redirect-uri-mismatch)).
* The callback goes through the dashboard origin (nginx or Vite proxy `/api`), which keeps the
  session cookie same-origin.

## 3. Create a client secret

1. **Certificates & secrets** → **Client secrets** → **New client secret**.
2. Add a description and an expiry. Microsoft limits secrets to 24 months and recommends less than
   12 months.
3. Select **Add** and copy the secret **Value** (not the Secret ID) immediately; it is not shown again.
   → `ENTRA_CLIENT_SECRET`.

Microsoft recommends certificates or federated credentials over client secrets for production. The
current configuration (`ENTRA_CLIENT_SECRET`) supports client secrets only, so in production:
store the secret in Azure Key Vault, give it a short lifetime, and rotate it by creating a second
secret, deploying it, then deleting the old one. Certificate support is on the roadmap.

## 4. Add delegated API permissions

1. **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**.
2. Select:

   | Permission | Why |
   |---|---|
   | `openid`, `profile`, `email` | Sign the user in and identify them |
   | `offline_access` | Refresh tokens, so mailbox sync works without re-prompting |
   | `User.Read` | Read the signed-in user's profile |
   | `Mail.Read` | Read the user's messages **including the body** (delta query) |

3. Select **Add permissions**.

Do **not** add `Mail.ReadWrite`, `Mail.Send` or any **Application** permissions. NUVEXA never
modifies or sends mail and only reads mailboxes of users who connected them.

**Why not `Mail.ReadBasic`?** It returns message metadata but excludes the body (`body`,
`bodyPreview`, `uniqueBody`) and attachments. Classification and extraction need the body text, so
`Mail.ReadBasic` is insufficient.

### Optional: Azure DevOps delegated permission

Only for `AZURE_DEVOPS_MODE=entra_delegated`, where work items are created as the approving user:

1. **API permissions** → **Add a permission** → find **Azure DevOps** (listed with the Microsoft APIs;
   if it is not shown, search **APIs my organization uses** for `Azure DevOps` or
   `499b84ac-1321-427f-aa17-267ca6975798`).
2. **Delegated permissions** → `user_impersonation` → **Add permissions**.

Microsoft classifies `user_impersonation` as high privilege (the token can call Azure DevOps REST APIs
with the user's full permissions). Azure DevOps still enforces the user's own project permissions,
and NUVEXA only calls work-item APIs on the configured organization, but weigh this against PAT mode
(`vso.work_write` only). The Azure DevOps organization must be connected to the same Entra tenant
(Azure DevOps **Organization settings** → **Microsoft Entra**). See
[setup-azure-devops.md](setup-azure-devops.md).

## 5. Consent

* By default users consent themselves: to sign-in scopes at first login, and to `Mail.Read` when they
  connect their mailbox.
* Many tenants restrict user consent. Users then see "Need admin approval" / `AADSTS65001`. An
  administrator grants consent once: **API permissions** → **Grant admin consent for &lt;tenant&gt;**
  → **Yes**, then **Refresh** and check that **Status** shows "Granted for &lt;tenant&gt;".
* Multi-tenant: each customer tenant needs consent in that tenant (an administrator of that tenant
  signs in and consents on behalf of the organization, or grants it on the enterprise application
  after it appears under **Entra ID** → **Enterprise apps**). Also add the tenant ID to
  `ENTRA_ALLOWED_TENANT_IDS`.
* To limit who can sign in at all, open **Entra ID** → **Enterprise apps** → your app →
  **Properties**, set **Assignment required?** to **Yes**, and assign users or groups.

## 6. Configure NUVEXA

```bash
MAIL_MODE=graph
ENTRA_CLIENT_ID=<your-app-registration-client-id>
ENTRA_CLIENT_SECRET=<your-client-secret-value>
ENTRA_TENANT_ID=<your-tenant-id>                 # or: organizations (multi-tenant)
ENTRA_ALLOWED_TENANT_IDS=<your-tenant-id>         # comma-separated; recommended always
OAUTH_REDIRECT_URI=http://localhost:8080/api/v1/auth/callback
APP_BASE_URL=http://localhost:8080
BOOTSTRAP_ADMIN_EMAILS=<admin@your-domain.example>
TOKEN_ENCRYPTION_KEYS=<output of: uv run nuvexa gen-key>
```

| Variable | Notes |
|---|---|
| `ENTRA_TENANT_ID` | Authority tenant: a tenant ID for single-tenant, `organizations` for multi-tenant work/school accounts |
| `ENTRA_ALLOWED_TENANT_IDS` | Comma-separated tenant IDs whose users may sign in; others are rejected. Set it even for single-tenant apps |
| `ENTRA_AUTHORITY_HOST` | Default `https://login.microsoftonline.com` (public cloud). National clouds also need a matching `GRAPH_BASE_URL`; not tested |
| `BOOTSTRAP_ADMIN_EMAILS` | Comma-separated, case-insensitive. Only these addresses can be bootstrapped as the first tenant administrator on sign-in. Keep the list to named people |
| `TOKEN_ENCRYPTION_KEYS` | Required once real tokens are stored (and always in production) |
| `DEMO_MODE` | May stay `true` in development (demo mailbox and personas remain available); set `false` for shared or production environments |

### Generating and rotating `TOKEN_ENCRYPTION_KEYS`

The MSAL token cache of each connected account is encrypted with Fernet (AES-128-CBC + HMAC-SHA256)
before it is stored in `credential_store`.

```bash
uv run nuvexa gen-key                       # local
docker compose run --rm api nuvexa gen-key  # inside the image
```

Rotation (comma-separated list, **first key encrypts, all keys decrypt**):

1. Generate a new key and **prepend** it: `TOKEN_ENCRYPTION_KEYS=<new>,<old>`. Restart API and worker.
2. Existing rows still decrypt with the old key; rows written from now on (for example after a token
   refresh or a reconnect) use the new key.
3. Remove the old key only when no stored credential still depends on it. If unsure, keep it: an
   extra decryption key costs nothing. Removing it too early makes the affected connections fail with
   "Stored secret cannot be decrypted with the configured keys"; users must reconnect their mailbox.

Keys are secrets: Key Vault in production, never in the repository, logs or tickets.

## 7. Verify

1. Restart the API and worker (settings are read at startup).
2. `curl -s http://localhost:8000/api/v1/auth/config` should report `"entra_login_enabled": true`.
3. Open the dashboard, **Sign in with Microsoft**, accept the consent prompt.
4. **Integrations** → connect the Microsoft mailbox → consent to `Mail.Read`.
5. **Sync mailbox**. The first sync reads up to `MAIL_SYNC_MAX_MESSAGES` messages from the last
   `MAIL_SYNC_INITIAL_DAYS` days; later syncs are incremental (delta).

Problems: [troubleshooting.md](troubleshooting.md).

## Production checklist

- [ ] Dedicated production registration; only `https://` redirect URIs; no localhost entries.
- [ ] `ENTRA_ALLOWED_TENANT_IDS` set; `BOOTSTRAP_ADMIN_EMAILS` limited to named administrators.
- [ ] Client secret in Key Vault with an expiry reminder and a rotation runbook.
- [ ] `TOKEN_ENCRYPTION_KEYS` in Key Vault; rotation procedure documented.
- [ ] Only the delegated permissions listed above; no application permissions.
- [ ] Optional: **Assignment required?** = Yes on the enterprise application.
- [ ] App registration has at least two owners.
