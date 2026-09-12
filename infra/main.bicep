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

@description('Enable Easy Auth on the demo Web App (require a signed-in Entra ID user). Client-level isolation is handled by app/auth.py (Jalon 5), not by this flag. See app/README.md for how to create the App Registration.')
param enableEasyAuth bool = true

@description('Entra ID App Registration (client) ID for Easy Auth — required when enableEasyAuth is true')
param easyAuthClientId string = ''

@description('Entra ID tenant ID for Easy Auth when easyAuthMultiTenant is false — required in that case')
param easyAuthTenantId string = ''

@description('Entra ID App Registration client secret for Easy Auth — required when enableEasyAuth is true. Pass at deploy time only (--parameters), never commit it.')
@secure()
param easyAuthClientSecret string = ''

@description('Jalon 5: allow sign-in from any Microsoft Entra tenant (external organizations). Requires the App Registration itself to also be multi-tenant — see app/README.md.')
param easyAuthMultiTenant bool = false

@description('Jalon 5: Entra tenant IDs allowed to complete sign-in at all (platform-enforced, WEBSITE_AUTH_AAD_ALLOWED_TENANTS) — max 10. Always include your own sandbox tenant.')
param easyAuthAllowedTenantIds array = []

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
// Azure Web App — demo interface (Jalon 4) + coarse Easy Auth gate
// ---------------------------------------------------------------------
module webapp 'modules/webapp.bicep' = {
  name: 'webapp'
  params: {
    planName: 'plan-${namePrefix}-v9'
    webAppName: 'app-${namePrefix}-v9'
    location: location
    enableEasyAuth: enableEasyAuth
    easyAuthClientId: easyAuthClientId
    easyAuthTenantId: easyAuthTenantId
    easyAuthClientSecret: easyAuthClientSecret
    easyAuthMultiTenant: easyAuthMultiTenant
    easyAuthAllowedTenantIds: easyAuthAllowedTenantIds
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
