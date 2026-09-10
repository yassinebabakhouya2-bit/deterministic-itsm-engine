// =====================================================================
// KnowledgeEngine v9 — Infrastructure orchestrator (Infrastructure as Code)
// Scope: resource group (rg-knowledgeengine-v9 already created)
// Reproducible & parameterized → deployable per client (model B)
// =====================================================================

targetScope = 'resourceGroup'

@description('Azure region — sovereignty: France Central mandated')
param location string = 'francecentral'

@description('Project naming prefix')
param namePrefix string = 'knowledgeengine2'

// ---------------------------------------------------------------------
// Blob storage — hosts KB records per client (private containers)
// ---------------------------------------------------------------------
module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    storageAccountName: 'st${namePrefix}v9'
    location: location
  }
}

// ---------------------------------------------------------------------
// Azure AI Search — hybrid index (BM25 + vectors + semantic reranker)
// Naming note: dash before "v9" (srch-knowledgeengine-v9)
// ---------------------------------------------------------------------
module search 'modules/search.bicep' = {
  name: 'search'
  params: {
    searchServiceName: 'srch-${namePrefix}-v9'
    location: location
  }
}

// ---------------------------------------------------------------------
// Azure AI Foundry + project + GPT-4o / embedding deployments
// ---------------------------------------------------------------------
module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  params: {
    foundryName: 'aif-${namePrefix}-v9'
    projectName: 'proj-${namePrefix}-v9'
    location: location
  }
}

// ---------------------------------------------------------------------
// RBAC — Search's managed identity reads Blob & calls the embedding
// ---------------------------------------------------------------------
module roles 'modules/roles.bicep' = {
  name: 'roles'
  params: {
    searchPrincipalId: search.outputs.searchPrincipalId
    storageAccountName: storage.outputs.storageAccountName
    foundryName: foundry.outputs.foundryName
  }
}

output storageAccountName string = storage.outputs.storageAccountName
output searchServiceName string = search.outputs.searchServiceName
output foundryName string = foundry.outputs.foundryName
