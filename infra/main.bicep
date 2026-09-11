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

@description('Comma-separated client ids queryable from the demo Web App (stopgap before Jalon 5 real per-user auth — see infra/modules/webapp.bicep and app/README.md)')
param webAppAllowedClients string = 'clienta,clientb,clientc,client-v,client-s'

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
// Azure Web App — demo interface (Jalon 4)
// ---------------------------------------------------------------------
module webapp 'modules/webapp.bicep' = {
  name: 'webapp'
  params: {
    planName: 'plan-${namePrefix}-v9'
    webAppName: 'app-${namePrefix}-v9'
    location: location
    allowedClients: webAppAllowedClients
  }
}

// ---------------------------------------------------------------------
// RBAC — Search's managed identity reads Blob & calls the embedding;
// the Web App's managed identity queries Search & calls the LLM (Jalon 4)
// ---------------------------------------------------------------------
module roles 'modules/roles.bicep' = {
  name: 'roles'
  params: {
    searchPrincipalId: search.outputs.searchPrincipalId
    storageAccountName: storage.outputs.storageAccountName
    foundryName: foundry.outputs.foundryName
    searchServiceName: search.outputs.searchServiceName
    webAppPrincipalId: webapp.outputs.webAppPrincipalId
  }
}

output storageAccountName string = storage.outputs.storageAccountName
output searchServiceName string = search.outputs.searchServiceName
output foundryName string = foundry.outputs.foundryName
output webAppHostName string = webapp.outputs.webAppHostName
