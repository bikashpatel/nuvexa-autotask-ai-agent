// NUVEXA-AutoTask-AI-Agent — Azure Container Apps reference architecture (docs/deployment.md) as Bicep.
//
// Nothing in this repository deploys this template, and it has not been deployed by this repository. It is compiled
// and checked offline only (infra/azure/README.md). main.json is generated from this file: edit the Bicep source and
// regenerate it, never the JSON.
//
// Rollout order (docs/deployment.md), one deployment per stage, incremental mode only:
//   foundation  network, monitoring, identities + registry/OpenAI roles, Key Vault, PostgreSQL, environment
//               -> then set the Key Vault secrets and create the database roles
//   migrate     + Key Vault Secrets User on each secret (they exist now) + the migrate job at backendImageTag
//               -> then start the job and wait for Succeeded
//   apps        + worker and api at backendImageTag, then the frontend (and the OpenTelemetry Collector)
// A stage never removes what a later stage created (incremental mode keeps resources the template leaves out).
//
// Resources are declared here, not in modules, so that the configuration scanner (Trivy reads only the top-level
// resources of an ARM template) sees them. The modules only hold role assignments in other resource groups.

targetScope = 'resourceGroup'

// =====================================================================================================================
// Parameters
// =====================================================================================================================

// --- Release ---------------------------------------------------------------------------------------------------------

@description('Deployment stage: foundation, then migrate (run the migrate job and wait for success), then apps.')
@allowed([
  'foundation'
  'migrate'
  'apps'
])
param stage string

@description('Immutable tag (git SHA) of nuvexa-backend. The migrate job, api and worker always run the same tag.')
param backendImageTag string

@description('Immutable tag (git SHA) of nuvexa-frontend.')
param frontendImageTag string

@description('Azure region of the workload resources.')
param location string = resourceGroup().location

@description('Tags applied to every resource.')
param tags object = {}

// --- Existing resources ----------------------------------------------------------------------------------------------

@description('Existing Azure Container Registry (admin user disabled) holding the images; pulled with the managed identity.')
param registryName string

@description('Resource group of the registry.')
param registryResourceGroupName string = resourceGroup().name

@description('Subscription of the registry.')
param registrySubscriptionId string = subscription().subscriptionId

@description('OpenTelemetry Collector (contrib distribution, azuremonitor exporter) image in the registry, as repository:tag@sha256:digest.')
param otelCollectorImage string

@description('Existing Azure OpenAI account; the managed identity gets Cognitive Services OpenAI User on it.')
param openAiAccountName string

@description('Resource group of the Azure OpenAI account.')
param openAiResourceGroupName string = resourceGroup().name

@description('Subscription of the Azure OpenAI account.')
param openAiSubscriptionId string = subscription().subscriptionId

@description('Azure OpenAI deployment name (AZURE_OPENAI_DEPLOYMENT).')
param openAiDeploymentName string

@description('Azure OpenAI GA API version with structured outputs (AZURE_OPENAI_API_VERSION), e.g. 2024-10-21 or newer.')
param openAiApiVersion string

// --- Resource names --------------------------------------------------------------------------------------------------

@description('User-assigned managed identity of the apps (frontend, api, worker, collector).')
param identityName string = 'id-nuvexa'

@description('Separate user-assigned managed identity of the migrate job: the only principal that can read database-migrate-url.')
param migrateIdentityName string = 'id-nuvexa-migrate'

@description('Log Analytics workspace.')
param logAnalyticsName string = 'log-nuvexa'

@description('Application Insights component (traces, through the OpenTelemetry Collector).')
param appInsightsName string = 'appi-nuvexa'

@description('Virtual network.')
param vnetName string = 'vnet-nuvexa'

@description('Container Apps environment.')
param environmentName string = 'cae-nuvexa'

@description('Key Vault (globally unique, 3-24 characters).')
@minLength(3)
@maxLength(24)
param keyVaultName string

@description('PostgreSQL Flexible Server (globally unique).')
@minLength(3)
@maxLength(63)
param postgresServerName string

@description('Container app names. The frontend reaches the api at http://<apiAppName>.')
param frontendAppName string = 'nuvexa-frontend'
param apiAppName string = 'nuvexa-api'
param workerAppName string = 'nuvexa-worker'
param otelCollectorAppName string = 'nuvexa-otel-collector'

@description('Container Apps job that runs `nuvexa migrate`.')
param migrateJobName string = 'nuvexa-migrate'

// --- Network ---------------------------------------------------------------------------------------------------------

@description('Address space of the virtual network.')
param vnetAddressPrefix string = '10.40.0.0/16'

@description('Container Apps environment infrastructure subnet (workload profiles: /27 or larger).')
param containerAppsSubnetPrefix string = '10.40.0.0/23'

@description('Subnet delegated to PostgreSQL Flexible Server (private access).')
param postgresSubnetPrefix string = '10.40.2.0/28'

@description('Subnet for private endpoints (Key Vault).')
param privateEndpointSubnetPrefix string = '10.40.3.0/28'

// --- Application settings (environment variables, see .env.example) -------------------------------------------------

@description('Public host name of the dashboard (custom domain bound to the frontend app), without scheme.')
param dashboardHost string

@description('Managed certificate of dashboardHost in the environment, once bound (docs/deployment.md). Empty: no custom domain yet. Set it on every later deployment, or the deployment removes the binding.')
param dashboardManagedCertificateName string = ''

@description('ENTRA_TENANT_ID: tenant of the app registration (a tenant id, or "organizations").')
param entraTenantId string = tenant().tenantId

@description('ENTRA_CLIENT_ID: application (client) id of the production app registration.')
param entraClientId string

@description('ENTRA_ALLOWED_TENANT_IDS: tenants allowed to sign in.')
param entraAllowedTenantIds array = [
  entraTenantId
]

@description('BOOTSTRAP_ADMIN_EMAILS: accounts made tenant administrator when first created. Clear after the first sign-in.')
param bootstrapAdminEmails array

@description('AZURE_DEVOPS_MODE. pat also wires the azure-devops-pat Key Vault secret.')
@allowed([
  'pat'
  'entra_delegated'
])
param azureDevOpsMode string

@description('AZURE_DEVOPS_ORGANIZATION.')
param azureDevOpsOrganization string

@description('AZURE_DEVOPS_DEFAULT_PROJECT.')
param azureDevOpsDefaultProject string

@description('MAIL_POLL_INTERVAL_SECONDS: 0 = manual sync only.')
@minValue(0)
param mailPollIntervalSeconds int = 0

@description('LOG_LEVEL.')
@allowed([
  'DEBUG'
  'INFO'
  'WARNING'
  'ERROR'
])
param logLevel string = 'INFO'

@description('DATABASE_POOL_SIZE per process (plus the same overflow). Keep pool x replicas under max_connections.')
@minValue(1)
@maxValue(100)
param databasePoolSize int = 5

@description('WORKER_CONCURRENCY: jobs per worker replica.')
@minValue(1)
@maxValue(64)
param workerConcurrency int = 4

@description('JOB_LEASE_SECONDS: keep it well above the longest job (LLM timeout x retries x steps).')
@minValue(30)
param jobLeaseSeconds int = 300

@description('DATABASE_RUNTIME_ROLE: the DML-only role of database-url. The migrate job grants it; api and worker check it at startup.')
@minLength(1)
@maxLength(63)
param databaseRuntimeRole string = 'nuvexa_app'

// --- Key Vault -------------------------------------------------------------------------------------------------------

@description('Key Vault public network access. Disabled: reachable only through the private endpoint.')
@allowed([
  'Disabled'
  'Enabled'
])
param keyVaultPublicNetworkAccess string = 'Disabled'

@description('Operator CIDRs allowed through the Key Vault firewall (used only when public network access is Enabled).')
param keyVaultAllowedIpRanges array = []

@description('Soft-delete retention of the vault (days).')
@minValue(7)
@maxValue(90)
param keyVaultSoftDeleteRetentionDays int = 90

// --- PostgreSQL ------------------------------------------------------------------------------------------------------

@description('PostgreSQL server administrator login (password authentication). Never used by the application.')
param postgresAdministratorLogin string

@description('Administrator password: required when the server is created (or to rotate it); leave empty on later deployments.')
@secure()
param postgresAdministratorPassword string = ''

@description('Compute SKU (zone-redundant HA needs General Purpose or Memory Optimized).')
param postgresSkuName string = 'Standard_D2ds_v5'

@allowed([
  'Burstable'
  'GeneralPurpose'
  'MemoryOptimized'
])
param postgresSkuTier string = 'GeneralPurpose'

@minValue(32)
param postgresStorageSizeGB int = 64

@description('High availability; zone-redundant for production.')
@allowed([
  'Disabled'
  'SameZone'
  'ZoneRedundant'
])
param postgresHighAvailabilityMode string = 'ZoneRedundant'

@description('Automated backup (point-in-time restore) retention in days.')
@minValue(7)
@maxValue(35)
param postgresBackupRetentionDays int = 14

@allowed([
  'Disabled'
  'Enabled'
])
param postgresGeoRedundantBackup string = 'Disabled'

// --- Monitoring ------------------------------------------------------------------------------------------------------

@minValue(30)
@maxValue(730)
param logAnalyticsRetentionDays int = 30

@description('Deploy the OpenTelemetry Collector and Application Insights and export traces (OTEL_ENABLED).')
param enableTracing bool = true

// --- Container Apps --------------------------------------------------------------------------------------------------

@description('Zone-redundant Container Apps environment (region must support availability zones).')
param zoneRedundant bool = false

@description('Encrypt traffic between apps inside the environment (peer-to-peer TLS), including http://<app-name> calls.')
param enablePeerTrafficEncryption bool = true

@minValue(1)
param apiMinReplicas int = 1
param apiMaxReplicas int = 3

@minValue(1)
param workerMinReplicas int = 1
param workerMaxReplicas int = 3

@minValue(1)
param frontendMinReplicas int = 1
param frontendMaxReplicas int = 3

@description('Worker termination grace period: above the 30 s the worker waits for in-flight jobs on SIGTERM.')
@minValue(31)
@maxValue(600)
param workerTerminationGracePeriodSeconds int = 60

@description('FORWARDED_ALLOW_IPS for the api: proxies whose X-Forwarded-* headers uvicorn trusts (the infrastructure subnet).')
param apiForwardedAllowIps string = containerAppsSubnetPrefix

@description('Maximum run time of one migration attempt (seconds).')
@minValue(60)
param migrateReplicaTimeoutSeconds int = 900

// =====================================================================================================================
// Variables
// =====================================================================================================================

var registryLoginServer = '${registryName}${environment().suffixes.acrLoginServer}'
var backendImage = '${registryLoginServer}/nuvexa-backend:${backendImageTag}'
var frontendImage = '${registryLoginServer}/nuvexa-frontend:${frontendImageTag}'

// Versionless secret URIs: Container Apps picks up a new secret version within about 30 minutes.
var keyVaultSecretsUri = 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/'

var registries = [
  {
    server: registryLoginServer
    identity: identity.id
  }
]

// The api and worker read these secrets (Key Vault references only; the values never pass through a deployment).
var backendSecretNames = concat(
  [
    'database-url'
    'token-encryption-keys'
    'audit-hmac-key'
    'entra-client-secret'
  ],
  azureDevOpsMode == 'pat' ? ['azure-devops-pat'] : []
)
var backendSecrets = map(backendSecretNames, name => {
  name: name
  keyVaultUrl: '${keyVaultSecretsUri}${name}'
  identity: identity.id
})

// Every secret the apps' identity may read (Key Vault Secrets User on each one, never on the vault). The migrate
// job's owner credential (database-migrate-url) is deliberately not in this list.
var appSecretNames = concat(backendSecretNames, enableTracing ? ['appinsights-connection-string'] : [])

// Production settings shared by the api and worker (names: app/core/config.py, .env.example).
var backendEnv = concat(
  [
    { name: 'APP_ENV', value: 'production' }
    { name: 'DEMO_MODE', value: 'false' }
    { name: 'EXPOSE_API_DOCS', value: 'false' }
    { name: 'LOG_FORMAT', value: 'json' }
    { name: 'LOG_LEVEL', value: logLevel }
    { name: 'RUN_EMBEDDED_WORKER', value: 'false' }
    { name: 'APP_BASE_URL', value: 'https://${dashboardHost}' }
    { name: 'OAUTH_REDIRECT_URI', value: 'https://${dashboardHost}/api/v1/auth/callback' }
    { name: 'MAIL_MODE', value: 'graph' }
    { name: 'MAIL_POLL_INTERVAL_SECONDS', value: string(mailPollIntervalSeconds) }
    { name: 'ENTRA_TENANT_ID', value: entraTenantId }
    { name: 'ENTRA_CLIENT_ID', value: entraClientId }
    { name: 'ENTRA_ALLOWED_TENANT_IDS', value: join(entraAllowedTenantIds, ',') }
    { name: 'BOOTSTRAP_ADMIN_EMAILS', value: join(bootstrapAdminEmails, ',') }
    { name: 'LLM_PROVIDER', value: 'azure_openai' }
    { name: 'AZURE_OPENAI_USE_ENTRA_ID', value: 'true' }
    { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
    { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.properties.endpoint }
    { name: 'AZURE_OPENAI_DEPLOYMENT', value: openAiDeploymentName }
    { name: 'AZURE_OPENAI_API_VERSION', value: openAiApiVersion }
    { name: 'AZURE_DEVOPS_MODE', value: azureDevOpsMode }
    { name: 'AZURE_DEVOPS_ORGANIZATION', value: azureDevOpsOrganization }
    { name: 'AZURE_DEVOPS_DEFAULT_PROJECT', value: azureDevOpsDefaultProject }
    { name: 'DATABASE_POOL_SIZE', value: string(databasePoolSize) }
    { name: 'DATABASE_RUNTIME_ROLE', value: databaseRuntimeRole }
    { name: 'WORKER_CONCURRENCY', value: string(workerConcurrency) }
    { name: 'JOB_LEASE_SECONDS', value: string(jobLeaseSeconds) }
    { name: 'OTEL_ENABLED', value: enableTracing ? 'true' : 'false' }
    { name: 'DATABASE_URL', secretRef: 'database-url' }
    { name: 'TOKEN_ENCRYPTION_KEYS', secretRef: 'token-encryption-keys' }
    { name: 'AUDIT_HMAC_KEY', secretRef: 'audit-hmac-key' }
    { name: 'ENTRA_CLIENT_SECRET', secretRef: 'entra-client-secret' }
  ],
  azureDevOpsMode == 'pat' ? [{ name: 'AZURE_DEVOPS_PAT', secretRef: 'azure-devops-pat' }] : [],
  // OTLP/HTTP to the collector app; the SDK appends /v1/traces (ingress port 80 -> target port 4318).
  enableTracing ? [{ name: 'OTEL_EXPORTER_OTLP_ENDPOINT', value: 'http://${otelCollectorAppName}' }] : []
)

// OpenTelemetry Collector configuration, passed through an environment variable (--config=env:OTELCOL_CONFIG).
// ${env:...} is expanded by the collector; the connection string comes from a Key Vault reference.
var otelCollectorConfig = '''
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
processors:
  memory_limiter:
    check_interval: 1s
    limit_percentage: 80
    spike_limit_percentage: 20
  batch: {}
exporters:
  azuremonitor:
    connection_string: ${env:APPLICATIONINSIGHTS_CONNECTION_STRING}
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [azuremonitor]
'''

// =====================================================================================================================
// Existing resources referenced, never modified
// =====================================================================================================================

resource openAi 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: openAiAccountName
  scope: resourceGroup(openAiSubscriptionId, openAiResourceGroupName)
}

// =====================================================================================================================
// Monitoring
// =====================================================================================================================

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logAnalyticsRetentionDays
    features: {
      // Ingestion through diagnostic settings and Application Insights only; no shared workspace keys.
      disableLocalAuth: true
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = if (enableTracing) {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
  }
}

// =====================================================================================================================
// Network
// =====================================================================================================================

resource vnet 'Microsoft.Network/virtualNetworks@2025-05-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        name: 'snet-container-apps'
        properties: {
          addressPrefix: containerAppsSubnetPrefix
          delegations: [
            {
              name: 'container-apps'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'snet-postgres'
        properties: {
          addressPrefix: postgresSubnetPrefix
          delegations: [
            {
              name: 'postgres'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
      {
        name: 'snet-private-endpoints'
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
        }
      }
    ]
  }

  resource containerAppsSubnet 'subnets' existing = {
    name: 'snet-container-apps'
  }

  resource postgresSubnet 'subnets' existing = {
    name: 'snet-postgres'
  }

  resource privateEndpointSubnet 'subnets' existing = {
    name: 'snet-private-endpoints'
  }
}

// =====================================================================================================================
// Identities and role assignments: three built-in roles in total, each on the one resource it is needed for.
// Key Vault Secrets User is assigned per secret (never on the vault) from the migrate stage on, once the secrets
// exist: the apps' identity on the apps' secrets, the migrate identity on database-migrate-url only.
// =====================================================================================================================

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: identityName
  location: location
  tags: tags
}

resource migrateIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: migrateIdentityName
  location: location
  tags: tags
}

resource appSecrets 'Microsoft.KeyVault/vaults/secrets@2026-02-01' existing = [
  for name in appSecretNames: {
    parent: keyVault
    name: name
  }
]

resource appSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (name, i) in appSecretNames: if (stage != 'foundation') {
    name: guid(keyVault.id, name, identity.id, '4633458b-17de-408a-b874-0445c86b69e6')
    scope: appSecrets[i]
    properties: {
      // Key Vault Secrets User (built-in): read this one secret's value.
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
      principalId: identity.properties.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource migrateSecret 'Microsoft.KeyVault/vaults/secrets@2026-02-01' existing = {
  parent: keyVault
  name: 'database-migrate-url'
}

resource migrateSecretUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (stage != 'foundation') {
  name: guid(keyVault.id, 'database-migrate-url', migrateIdentity.id, '4633458b-17de-408a-b874-0445c86b69e6')
  scope: migrateSecret
  properties: {
    // Key Vault Secrets User (built-in): the schema owner's connection string, and nothing else.
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: migrateIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

module acrPull 'modules/acr-pull-role.bicep' = {
  name: 'nuvexa-acrpull-${uniqueString(identity.id)}'
  scope: resourceGroup(registrySubscriptionId, registryResourceGroupName)
  params: {
    registryName: registryName
    principalId: identity.properties.principalId
  }
}

module migrateAcrPull 'modules/acr-pull-role.bicep' = {
  name: 'nuvexa-acrpull-${uniqueString(migrateIdentity.id)}'
  scope: resourceGroup(registrySubscriptionId, registryResourceGroupName)
  params: {
    registryName: registryName
    principalId: migrateIdentity.properties.principalId
  }
}

module openAiUser 'modules/openai-user-role.bicep' = {
  name: 'nuvexa-openai-user-${uniqueString(identity.id)}'
  scope: resourceGroup(openAiSubscriptionId, openAiResourceGroupName)
  params: {
    accountName: openAiAccountName
    principalId: identity.properties.principalId
  }
}

// =====================================================================================================================
// Key Vault (RBAC, purge protection, private endpoint; secret values are set out of band, never by this template)
// =====================================================================================================================

resource keyVault 'Microsoft.KeyVault/vaults@2026-02-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: keyVaultSoftDeleteRetentionDays
    enablePurgeProtection: true
    enabledForDeployment: false
    enabledForTemplateDeployment: false
    enabledForDiskEncryption: false
    publicNetworkAccess: keyVaultPublicNetworkAccess
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: map(keyVaultAllowedIpRanges, range => { value: range })
      virtualNetworkRules: []
    }
  }
}

resource keyVaultDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
  tags: tags

  resource link 'virtualNetworkLinks' = {
    name: vnetName
    location: 'global'
    tags: tags
    properties: {
      registrationEnabled: false
      virtualNetwork: {
        id: vnet.id
      }
    }
  }
}

resource keyVaultPrivateEndpoint 'Microsoft.Network/privateEndpoints@2025-05-01' = {
  name: 'pe-${keyVaultName}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: vnet::privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'vault'
        properties: {
          privateLinkServiceId: keyVault.id
          groupIds: [
            'vault'
          ]
        }
      }
    ]
  }

  resource dnsZoneGroup 'privateDnsZoneGroups' = {
    name: 'vault'
    properties: {
      privateDnsZoneConfigs: [
        {
          name: 'vault'
          properties: {
            privateDnsZoneId: keyVaultDnsZone.id
          }
        }
      ]
    }
  }
}

// =====================================================================================================================
// PostgreSQL Flexible Server 18 (private access through the delegated subnet, TLS required)
// =====================================================================================================================

resource postgresDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: '${postgresServerName}.private.postgres.database.azure.com'
  location: 'global'
  tags: tags

  resource link 'virtualNetworkLinks' = {
    name: vnetName
    location: 'global'
    tags: tags
    properties: {
      registrationEnabled: false
      virtualNetwork: {
        id: vnet.id
      }
    }
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = {
  name: postgresServerName
  location: location
  tags: tags
  sku: {
    name: postgresSkuName
    tier: postgresSkuTier
  }
  properties: {
    version: '18'
    administratorLogin: postgresAdministratorLogin
    // Omitted when empty, so later deployments neither need nor reset the administrator password.
    administratorLoginPassword: empty(postgresAdministratorPassword) ? null : postgresAdministratorPassword
    authConfig: {
      // Password authentication, as documented: the application roles use passwords (DATABASE_URL).
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
    network: {
      delegatedSubnetResourceId: vnet::postgresSubnet.id
      privateDnsZoneArmResourceId: postgresDnsZone.id
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      storageSizeGB: postgresStorageSizeGB
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: postgresBackupRetentionDays
      geoRedundantBackup: postgresGeoRedundantBackup
    }
    highAvailability: {
      mode: postgresHighAvailabilityMode
    }
  }
  dependsOn: [
    postgresDnsZone::link
  ]
}

// Server parameters are applied one at a time (the server accepts one configuration change at a time).
resource postgresRequireSecureTransport 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2025-08-01' = {
  parent: postgres
  name: 'require_secure_transport'
  properties: {
    value: 'ON'
    source: 'user-override'
  }
}

resource postgresMinimumTls 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2025-08-01' = {
  parent: postgres
  name: 'ssl_min_protocol_version'
  properties: {
    value: 'TLSv1.2'
    source: 'user-override'
  }
  dependsOn: [
    postgresRequireSecureTransport
  ]
}

// The application database. Its owner and the least-privilege runtime role are created by the database procedure in
// docs/deployment.md, not by this template (ARM cannot manage PostgreSQL roles).
resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = {
  parent: postgres
  name: 'nuvexa'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [
    postgresMinimumTls
  ]
}

// =====================================================================================================================
// Container Apps environment (workload profiles, VNet-integrated, logs through diagnostic settings)
// =====================================================================================================================

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    vnetConfiguration: {
      infrastructureSubnetId: vnet::containerAppsSubnet.id
      internal: false // the frontend's external ingress is published on the environment's public endpoint
    }
    appLogsConfiguration: {
      destination: 'azure-monitor' // diagnostic settings below; no workspace shared key in the environment
    }
    peerTrafficConfiguration: {
      encryption: {
        enabled: enablePeerTrafficEncryption
      }
    }
    zoneRedundant: zoneRedundant
    publicNetworkAccess: 'Enabled'
  }
}

resource environmentDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'log-analytics'
  scope: containerAppsEnvironment
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
  }
}

// =====================================================================================================================
// migrate job (stage migrate and later): `nuvexa migrate`, manual trigger, same backend image as api and worker
// =====================================================================================================================

resource migrateJob 'Microsoft.App/jobs@2026-01-01' = if (stage != 'foundation') {
  name: migrateJobName
  location: location
  tags: tags
  // Its own identity: the apps' identity cannot read the owner credential, and this one can read nothing else.
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${migrateIdentity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: migrateReplicaTimeoutSeconds
      replicaRetryLimit: 0
      // The identity only pulls the image and resolves the Key Vault reference; the container cannot use it.
      identitySettings: [
        {
          identity: migrateIdentity.id
          lifecycle: 'None'
        }
      ]
      registries: [
        {
          server: registryLoginServer
          identity: migrateIdentity.id
        }
      ]
      // Only the schema owner's credential; the runtime keys are not needed to change the schema.
      secrets: [
        {
          name: 'database-migrate-url'
          keyVaultUrl: '${keyVaultSecretsUri}database-migrate-url'
          identity: migrateIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: backendImage
          command: [
            'nuvexa'
          ]
          args: [
            'migrate'
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          // Deliberately not APP_ENV=production: that requires the token and audit keys, which schema changes and
          // grants do not need (anyone who can start the job can use its secrets).
          env: [
            { name: 'DATABASE_URL', secretRef: 'database-migrate-url' }
            { name: 'DATABASE_RUNTIME_ROLE', value: databaseRuntimeRole }
            { name: 'DEMO_MODE', value: 'false' }
            { name: 'LOG_FORMAT', value: 'json' }
            { name: 'LOG_LEVEL', value: logLevel }
          ]
        }
      ]
    }
  }
  dependsOn: [
    migrateSecretUser
    migrateAcrPull
    postgresDatabase
  ]
}

// =====================================================================================================================
// Apps (stage apps): collector, then worker and api, then the frontend
// =====================================================================================================================

resource otelCollector 'Microsoft.App/containerApps@2026-01-01' = if (stage == 'apps' && enableTracing) {
  name: otelCollectorAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      identitySettings: [
        {
          identity: identity.id
          lifecycle: 'None'
        }
      ]
      ingress: {
        external: false
        targetPort: 4318
        transport: 'http'
        allowInsecure: true // internal only: http://<collector-app-name> from the api and worker
      }
      registries: registries
      secrets: [
        {
          name: 'appinsights-connection-string'
          keyVaultUrl: '${keyVaultSecretsUri}appinsights-connection-string'
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'otel-collector'
          image: '${registryLoginServer}/${otelCollectorImage}'
          args: [
            '--config=env:OTELCOL_CONFIG'
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            { name: 'OTELCOL_CONFIG', value: otelCollectorConfig }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights-connection-string' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 2
      }
    }
  }
  dependsOn: [
    appSecretsUser
    acrPull
    appInsights
  ]
}

resource worker 'Microsoft.App/containerApps@2026-01-01' = if (stage == 'apps') {
  name: workerAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      // The worker's code uses the identity for Azure OpenAI (AZURE_OPENAI_USE_ENTRA_ID, AZURE_CLIENT_ID).
      identitySettings: [
        {
          identity: identity.id
          lifecycle: 'Main'
        }
      ]
      registries: registries
      secrets: backendSecrets
      // No ingress: the worker has no port.
    }
    template: {
      terminationGracePeriodSeconds: workerTerminationGracePeriodSeconds
      containers: [
        {
          name: 'worker'
          image: backendImage
          command: [
            'nuvexa'
          ]
          args: [
            'worker'
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: backendEnv
        }
      ]
      scale: {
        minReplicas: workerMinReplicas
        maxReplicas: workerMaxReplicas
        rules: [
          {
            name: 'cpu'
            custom: {
              type: 'cpu'
              metadata: {
                type: 'Utilization'
                value: '70'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    appSecretsUser
    acrPull
    openAiUser
    migrateJob
    otelCollector
  ]
}

resource api 'Microsoft.App/containerApps@2026-01-01' = if (stage == 'apps') {
  name: apiAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      identitySettings: [
        {
          identity: identity.id
          lifecycle: 'None'
        }
      ]
      ingress: {
        external: false
        targetPort: 8000
        transport: 'http'
        // Internal only. The frontend proxies to http://<apiAppName> (no redirect to HTTPS); with peer traffic
        // encryption that hop is still TLS-encrypted inside the environment.
        allowInsecure: true
      }
      registries: registries
      secrets: backendSecrets
    }
    template: {
      containers: [
        {
          name: 'api'
          image: backendImage
          command: [
            'nuvexa'
          ]
          args: [
            'api'
            '--host'
            '0.0.0.0'
            '--port'
            '8000'
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(backendEnv, [
            { name: 'FORWARDED_ALLOW_IPS', value: apiForwardedAllowIps }
            // Shared counters (rate_limit_counters) as soon as more than one replica can run; per-process
            // counters would allow N x every limit. All replicas share database-url and audit-hmac-key.
            { name: 'RATE_LIMIT_BACKEND', value: apiMaxReplicas > 1 ? 'database' : 'memory' }
          ])
          // Exactly the probes documented in docs/deployment.md (liveness never checks the database).
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/api/v1/health/live'
                port: 8000
              }
              periodSeconds: 3
              failureThreshold: 40
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/api/v1/health/live'
                port: 8000
              }
              periodSeconds: 15
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/api/v1/health/ready'
                port: 8000
              }
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: apiMinReplicas
        maxReplicas: apiMaxReplicas
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    appSecretsUser
    acrPull
    migrateJob
    otelCollector
  ]
}

resource frontend 'Microsoft.App/containerApps@2026-01-01' = if (stage == 'apps') {
  name: frontendAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      identitySettings: [
        {
          identity: identity.id
          lifecycle: 'None'
        }
      ]
      // The only external ingress; HTTP is redirected to HTTPS. The custom domain's managed certificate is issued
      // once by `az containerapp hostname bind` after the first apps deployment (docs/deployment.md).
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        customDomains: empty(dashboardManagedCertificateName)
          ? []
          : [
              {
                name: dashboardHost
                bindingType: 'SniEnabled'
                certificateId: resourceId(
                  'Microsoft.App/managedEnvironments/managedCertificates',
                  environmentName,
                  dashboardManagedCertificateName
                )
              }
            ]
      }
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'frontend'
          image: frontendImage
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            // Internal ingress by app name: port 80 -> target port 8000 (frontend/nginx.conf).
            { name: 'API_UPSTREAM', value: 'http://${apiAppName}' }
          ]
        }
      ]
      scale: {
        minReplicas: frontendMinReplicas
        maxReplicas: frontendMaxReplicas
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '100'
              }
            }
          }
        ]
      }
    }
  }
  // Rollout order: worker and api first, then the frontend.
  dependsOn: [
    worker
    api
    acrPull
  ]
}

// =====================================================================================================================
// Outputs (no secrets)
// =====================================================================================================================

@description('Client id of the managed identity (AZURE_CLIENT_ID).')
output identityClientId string = identity.properties.clientId

@description('Key Vault URI: set the secret values listed in docs/deployment.md here.')
output keyVaultUri string = keyVault.properties.vaultUri

@description('PostgreSQL host for database-url and database-migrate-url.')
output postgresServerFqdn string = postgres.properties.fullyQualifiedDomainName

@description('Default domain of the Container Apps environment.')
output environmentDefaultDomain string = containerAppsEnvironment.properties.defaultDomain

@description('Public IP of the environment (A record for an apex custom domain).')
output environmentStaticIp string = containerAppsEnvironment.properties.staticIp

@description('Value of the asuid TXT record that verifies the custom domain.')
output customDomainVerificationId string = containerAppsEnvironment.properties.?customDomainConfiguration.?customDomainVerificationId ?? ''

@description('Name of the migrate job to start before the apps stage.')
output migrateJobName string = migrateJobName
