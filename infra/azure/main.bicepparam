using 'main.bicep'

// Parameters for main.bicep. Placeholders only: <...> values are invalid on purpose, so a deployment fails until
// every one is replaced. No secret is ever written here. Keep one `param name = value` per line (the IaC test reads
// this file).

// --- Per deployment, from the environment of the (not executed) release procedure ------------------------------------
// NUVEXA_DEPLOY_STAGE: foundation | migrate | apps (infra/azure/README.md, docs/deployment.md).
param stage = readEnvironmentVariable('NUVEXA_DEPLOY_STAGE')
param backendImageTag = readEnvironmentVariable('NUVEXA_BACKEND_IMAGE_TAG', '<git-sha>')
param frontendImageTag = readEnvironmentVariable('NUVEXA_FRONTEND_IMAGE_TAG', '<git-sha>')
// Set only for the deployment that creates the server (or to rotate the password); unset afterwards.
param postgresAdministratorPassword = readEnvironmentVariable('NUVEXA_POSTGRES_ADMIN_PASSWORD', '')

// --- Existing resources ----------------------------------------------------------------------------------------------
param registryName = '<registry-name>'
param registryResourceGroupName = '<registry-resource-group>'
param otelCollectorImage = '<opentelemetry-collector-contrib-repository>:<version>@sha256:<digest>'
param openAiAccountName = '<azure-openai-account-name>'
param openAiResourceGroupName = '<azure-openai-resource-group>'
param openAiDeploymentName = '<azure-openai-deployment-name>'
param openAiApiVersion = '<azure-openai-api-version>'

// --- Globally unique names -------------------------------------------------------------------------------------------
param keyVaultName = '<key-vault-name>'
param postgresServerName = '<postgres-server-name>'
param postgresAdministratorLogin = '<postgres-administrator-login>'

// --- Application -----------------------------------------------------------------------------------------------------
param dashboardHost = '<your-dashboard-host>'
// Empty until the managed certificate is bound once (docs/deployment.md); then its name, on every deployment.
param dashboardManagedCertificateName = ''
param entraTenantId = '<entra-tenant-id>'
param entraClientId = '<entra-application-client-id>'
param entraAllowedTenantIds = ['<entra-tenant-id>']
param bootstrapAdminEmails = ['<admin@your-domain.example>']
param azureDevOpsMode = 'pat'
param azureDevOpsOrganization = '<azure-devops-organization>'
param azureDevOpsDefaultProject = '<azure-devops-project>'
// The DML-only role in database-url (docs/deployment.md, Database); the schema owner is in database-migrate-url.
param databaseRuntimeRole = 'nuvexa_app'

// --- Network (replace with your address plan) ------------------------------------------------------------------------
param vnetAddressPrefix = '10.40.0.0/16'
param containerAppsSubnetPrefix = '10.40.0.0/23'
param postgresSubnetPrefix = '10.40.2.0/28'
param privateEndpointSubnetPrefix = '10.40.3.0/28'

// --- Production defaults, stated explicitly --------------------------------------------------------------------------
param keyVaultPublicNetworkAccess = 'Disabled'
param postgresHighAvailabilityMode = 'ZoneRedundant'
param enableTracing = true
param enablePeerTrafficEncryption = true
param workerTerminationGracePeriodSeconds = 60
