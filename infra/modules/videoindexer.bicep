// =====================================================================
// Module: Azure AI Video Indexer account (ARM-based) — Jalon 8
// One-time platform prerequisite (like Foundry/Search), NOT per-client.
// System-assigned managed identity + RBAC on the existing storage
// account (Storage Blob Data Contributor — required so VI can read the
// source video blob and write its own working data there). No Media
// Services account needed with the ARM-based resource type
// (Microsoft.VideoIndexer/accounts@2024-01-01).
//
// Deploy standalone (not through infra/main.bicep) to avoid re-supplying
// the Easy Auth secure params on every redeploy — same pattern already
// used for infra/modules/roles.bicep during the Jalon 7 multimodality
// follow-up. See docs/operations-runbook.md §10 for the exact command.
//
// Region: France Central confirmed supported (verified 2026-09-20 in the
// Portal's create-resource region dropdown) — same region as the rest of
// this platform, no sovereignty/availability tradeoff needed here.
// =====================================================================

@description('Name of the Video Indexer account')
param videoIndexerAccountName string

@description('Azure region')
param location string = 'francecentral'

@description('Name of the existing storage account VI will use for its working data (must be StorageV2 general-purpose v2 — already true of this platform storage account)')
param storageAccountName string

var storageBlobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource videoIndexer 'Microsoft.VideoIndexer/accounts@2024-01-01' = {
  name: videoIndexerAccountName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    storageServices: {
      resourceId: storage.id
    }
  }
}

// Video Indexer's managed identity -> read/write the storage account
// (its own working data + the source video blob it will index).
resource videoIndexerToStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, videoIndexer.id, storageBlobDataContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributor)
    principalId: videoIndexer.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output videoIndexerAccountId string = videoIndexer.id
output videoIndexerPrincipalId string = videoIndexer.identity.principalId
output videoIndexerAccountName string = videoIndexer.name
