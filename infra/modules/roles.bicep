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

@description('Name of the target Search service (Jalon 4 — webapp needs a role ON Search)')
param searchServiceName string

@description('PrincipalId (objectId) of the Web App managed identity (Jalon 4)')
param webAppPrincipalId string

// Built-in Azure roles (stable IDs)
var storageBlobDataReader = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var cognitiveServicesOpenAIUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var searchIndexDataReader = '1407120a-92aa-4202-b7e9-c0e197c71c8f' // query-only, not admin

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: foundryName
}

resource search 'Microsoft.Search/searchServices@2024-06-01-preview' existing = {
  name: searchServiceName
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


// ---- Jalon 4: Web App (app/) -- keyless RBAC, no admin keys, no Key Vault ----

// Web App -> query Search (read-only data plane, NOT an admin role)
resource webAppToSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, webAppPrincipalId, searchIndexDataReader)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataReader)
    principalId: webAppPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Web App -> call GPT-4o (same role already granted to Search's own identity, for embeddings)
resource webAppToFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, webAppPrincipalId, cognitiveServicesOpenAIUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUser)
    principalId: webAppPrincipalId
    principalType: 'ServicePrincipal'
  }
}
