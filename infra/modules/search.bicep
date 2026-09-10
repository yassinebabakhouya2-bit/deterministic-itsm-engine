// =====================================================================
// Module: Azure AI Search — hybrid index (BM25 + vectors + semantic reranker)
// Managed identity (SystemAssigned) → keyless connections to Storage & OpenAI
// =====================================================================

@description('Name of the Azure AI Search service')
param searchServiceName string

@description('Azure region')
param location string

resource search 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: searchServiceName
  location: location
  sku: {
    name: 'basic'
  }
  identity: {
    type: 'SystemAssigned' // managed identity: no hardcoded key (axiom A5)
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    semanticSearch: 'standard' // enables the semantic reranker (required by the architecture)
    publicNetworkAccess: 'enabled' // POC; private endpoint at Milestone 2 hardening
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
  }
}

output searchServiceName string = search.name
output searchServiceId string = search.id
output searchPrincipalId string = search.identity.principalId // for the upcoming role assignments
