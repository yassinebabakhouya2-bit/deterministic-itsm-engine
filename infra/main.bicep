// =====================================================================
// KnowledgeEngine v9 — Infrastructure orchestrator (Infrastructure as Code)
// Scope: resource group (rg-knowledgeengine-v9 already created)
// Reproducible & parameterized → deployable per client (model B)
// =====================================================================

targetScope = 'resourceGroup'

@description('Azure region — sovereignty: France Central mandated')
param location string = 'francecentral'

@description('Project naming prefix')
param namePrefix string = 'knowledgeengine3'

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

@description('Deploiement de modele utilise par la Function d\'enrichissement. Epingle : changer de modele change les sorties extraites, donc le contenu de l\'index.')
param enrichDeployment string = 'gpt-4o-enrich'

@description('Creer le deploiement de modele gpt-4o-enrich. Mettre a false pour un premier passage si la validation preflight refuse le quota (voir infra/modules/foundry.bicep).')
param deployEnrichModel bool = true

@description('gpt-4o (answer path) capacity in kTPM -- see modules/foundry.bicep. scripts/bootstrap-new-tenant.ps1 lowers it to fit a new subscription quota.')
param generationCapacity int = 30

@description('gpt-4o-enrich (indexing) capacity in kTPM -- see modules/foundry.bicep.')
param enrichCapacity int = 20

@description('text-embedding-3-large capacity in kTPM -- see modules/foundry.bicep.')
param embeddingCapacity int = 120

@description('Deploy (or re-apply) the AI Foundry account, its project and the 3 model deployments. scripts/bootstrap-new-tenant.ps1 passes false when they already exist and are Succeeded: every re-apply PUTs the Cognitive Services account again, which on a new subscription hit RequestConflict and, repeated, the anti-abuse block 715-123420 (runbook 12.4).')
param deployFoundry bool = true

// Deterministic name: the modules below no longer need the foundry module's outputs,
// so they still work when deployFoundry is false (they reference the account as existing).
var foundryAccountName = 'aif-${namePrefix}-v9'

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
module foundry 'modules/foundry.bicep' = if (deployFoundry) {
  name: 'foundry'
  params: {
    foundryName: foundryAccountName
    projectName: 'proj-${namePrefix}-v9'
    location: location
    deployEnrichModel: deployEnrichModel
    generationCapacity: generationCapacity
    enrichCapacity: enrichCapacity
    embeddingCapacity: embeddingCapacity
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
// Function d'enrichissement semantique (entites, alias, triplets, audience)
// Appelee par AI Search pendant l'indexation via WebApiSkill -- jamais sur
// le chemin d'une requete utilisateur. Hebergee sur le plan B1 existant
// (voir l'historique de ce choix en tete de enrich-function.bicep : Flex
// Consumption a ete tente et abandonne). Elle depend donc du module webapp,
// qui cree ce plan.
// RBAC porte par le module lui-meme (identite managee systeme).
// ---------------------------------------------------------------------
module enrichFunction 'modules/enrich-function.bicep' = {
  name: 'enrichFunction'
  dependsOn: [
    webapp
    foundry // explicit now that foundryName no longer comes from its outputs
  ]
  params: {
    functionAppName: 'fn-${namePrefix}-v9'
    hostingPlanName: 'plan-${namePrefix}-v9'
    location: location
    storageAccountName: storage.outputs.storageAccountName
    foundryName: foundryAccountName
    searchServiceName: search.outputs.searchServiceName
    enrichDeployment: enrichDeployment
  }
}

// ---------------------------------------------------------------------
// V10 deterministic engine (kecore / kefind / scoreboard) on Azure.
// Slice 1: Function App + per-client containers (kecore-<client>,
// tickets-<client>) + RBAC, no application code yet. Same shared B1 plan
// as the Web App and the enrichment Function. See modules/kecore.bicep
// and docs/v10-deterministic-engine.md ("Azure-native migration").
// ---------------------------------------------------------------------
module kecore 'modules/kecore.bicep' = {
  name: 'kecore'
  dependsOn: [
    webapp // creates the shared plan
    foundry
  ]
  params: {
    namePrefix: namePrefix
    location: location
    hostingPlanName: 'plan-${namePrefix}-v9'
    storageAccountName: storage.outputs.storageAccountName
    foundryName: foundryAccountName
  }
}

// ---------------------------------------------------------------------
// RBAC — Search's managed identity reads Blob & calls the embedding;
// the Web App's managed identity queries Search & calls the LLM (Jalon 4)
// ---------------------------------------------------------------------
module roles 'modules/roles.bicep' = {
  name: 'roles'
  dependsOn: [
    foundry // explicit now that foundryName no longer comes from its outputs
  ]
  params: {
    searchPrincipalId: search.outputs.searchPrincipalId
    storageAccountName: storage.outputs.storageAccountName
    foundryName: foundryAccountName
    searchServiceName: search.outputs.searchServiceName
    webAppPrincipalId: webapp.outputs.webAppPrincipalId
  }
}

output storageAccountName string = storage.outputs.storageAccountName
output searchServiceName string = search.outputs.searchServiceName
output foundryName string = foundryAccountName
output webAppHostName string = webapp.outputs.webAppHostName
output enrichFunctionHostName string = enrichFunction.outputs.functionAppHostName
output kecoreFunctionAppName string = kecore.outputs.functionAppName
