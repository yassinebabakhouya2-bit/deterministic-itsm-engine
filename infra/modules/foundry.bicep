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

// ---- GPT-4o deployment (regional Standard, pinned version) ----
resource gpt4o 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: foundry
  name: 'gpt-4o'
  sku: {
    name: 'Standard' // regional → France Central sovereignty
    capacity: 50 // ~50,000 tokens/min
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

output foundryName string = foundry.name
output foundryEndpoint string = foundry.properties.endpoint
output foundryPrincipalId string = foundry.identity.principalId
