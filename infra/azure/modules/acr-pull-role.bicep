// AcrPull for the workload identity, scoped to the one registry it pulls from.
// Deployed by main.bicep at the registry's resource group (which may differ from the workload's).

@description('Name of the existing Azure Container Registry.')
param registryName string

@description('Principal (object) id of the user-assigned managed identity.')
param principalId string

resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' existing = {
  name: registryName
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, principalId, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: registry
  properties: {
    // AcrPull (built-in)
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
