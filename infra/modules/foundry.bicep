// =====================================================================
// Module: Azure AI Foundry — account + project + model deployments
// GPT-4o (orchestration, temp=0 on the call side) + text-embedding-3-large
// REGIONAL deployments (France Central) + NoAutoUpgrade (axiom A1)
// =====================================================================

@description('Name of the Azure AI Foundry resource')
param foundryName string

@description('Name of the Foundry project')
param projectName string

@description('Azure region')
param location string

// ---- Foundry account (Cognitive Services / AIServices) ----
resource foundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: foundryName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: foundryName
    publicNetworkAccess: 'Enabled' // POC; harden at Milestone 2
    networkAcls: {
      defaultAction: 'Allow' // required by the resource; restrict at Milestone 2
    }
  }
}

// ---- Default project ----
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: foundry
  name: projectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectName
  }
}

// Le quota gpt-4o de la souscription est de 50 kTPM au total, et il etait
// entierement consomme par le deploiement de generation. Le decoupage
// ci-dessous (30 + 20) ne demande donc AUCUN quota supplementaire : il
// partage l'existant. Ce qu'on achete en le faisant, c'est l'isolation --
// une reindexation complete ne peut plus vider le budget du chemin de
// reponse et le faire tomber en 429, elle sature son propre compartiment.
// Ce qu'on paie, c'est 20 kTPM de moins en pointe sur les reponses.
// Si le quota gpt-4o de la souscription est releve un jour, remonter
// generationCapacity a 50 et laisser enrichCapacity tel quel.
@description('Capacite (kTPM) du deploiement de generation (chemin de reponse utilisateur). generationCapacity + enrichCapacity doit rester <= quota gpt-4o de la souscription.')
param generationCapacity int = 30

@description('Capacite (kTPM) du deploiement d\'enrichissement a l\'indexation. Compartiment separe : une rafale de reindexation sature celui-ci, pas celui des reponses.')
param enrichCapacity int = 20

@description('Creer le deploiement gpt-4o-enrich. La validation preflight d\'ARM evalue le quota AVANT d\'appliquer la reduction de gpt-4o : si elle refuse, deployer une premiere fois avec false (ce qui ramene gpt-4o a 30), puis une seconde fois avec true.')
param deployEnrichModel bool = true

// ---- GPT-4o deployment (regional Standard, pinned version) ----
resource gpt4o 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: foundry
  name: 'gpt-4o'
  sku: {
    name: 'Standard' // regional → France Central sovereignty
    capacity: generationCapacity // partage du quota avec gpt-4o-enrich, voir plus haut
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4o'
      version: '2024-11-20'
    }
    versionUpgradeOption: 'NoAutoUpgrade' // axiom A1: pinned version
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

// ---- text-embedding-3-large deployment (regional Standard) ----
// dependsOn gpt4o: OpenAI deployments must be created sequentially
resource embedding 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: foundry
  name: 'text-embedding-3-large'
  dependsOn: [
    gpt4o
  ]
  sku: {
    name: 'Standard'
    capacity: 120 // ~120,000 tokens/min
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-large'
      version: '1'
    }
    versionUpgradeOption: 'NoAutoUpgrade' // ← PINS the open action (stabilizes the vector space)
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

// ---- gpt-4o-enrich : deploiement DEDIE a l'enrichissement a l'indexation ----
// Deliberement separe de `gpt-4o`, qui sert les reponses aux utilisateurs.
// Une reindexation complete est une rafale de centaines d'appels : sur un
// deploiement partage elle consommerait tout le TPM et ferait tomber le
// chemin de reponse en 429. Deux deploiements = deux quotas, la rafale
// d'indexation ne peut plus affamer l'interface.
// Meme modele, meme version epinglee : l'extraction doit rester stable
// dans le temps (axiome A1).
// dependsOn embedding : les deploiements OpenAI se creent en serie.
resource gpt4oEnrich 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = if (deployEnrichModel) {
  parent: foundry
  name: 'gpt-4o-enrich'
  dependsOn: [
    embedding
  ]
  sku: {
    name: 'Standard'
    capacity: enrichCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4o'
      version: '2024-11-20'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

output foundryName string = foundry.name
output foundryEndpoint string = foundry.properties.endpoint
output foundryPrincipalId string = foundry.identity.principalId
