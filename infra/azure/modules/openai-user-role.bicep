// Cognitive Services OpenAI User for the workload identity, scoped to the one Azure OpenAI account it calls.
// Deployed by main.bicep at the account's resource group (which may differ from the workload's).

@description('Name of the existing Azure OpenAI (or Azure AI Services) account.')
param accountName string

@description('Principal (object) id of the user-assigned managed identity.')
param principalId string

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  scope: account
  properties: {
    // Cognitive Services OpenAI User (built-in): inference only, no keys, no management.
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
