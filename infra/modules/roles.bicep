// =====================================================================
// Module: Role assignments (RBAC) — Search's managed identity
// Lets AI Search read Blob and call the embedding WITHOUT A KEY
// (managed identity → axiom A5, zero secrets)
// =====================================================================

@description('PrincipalId (objectId) of the Search service managed identity')
param searchPrincipalId string

@description('Name of the target storage account')
param storageAccountName string

@description('Name of the target Foundry resource')
param foundryName string

// Built-in Azure roles (stable IDs)
var storageBlobDataReader = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var cognitiveServicesOpenAIUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: foundryName
}

// Search → read blobs (KB records)
resource searchToStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, searchPrincipalId, storageBlobDataReader)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReader)
    principalId: searchPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Search → call the embedding (integrated vectorization)
resource searchToFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, searchPrincipalId, cognitiveServicesOpenAIUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUser)
    principalId: searchPrincipalId
    principalType: 'ServicePrincipal'
  }
}
