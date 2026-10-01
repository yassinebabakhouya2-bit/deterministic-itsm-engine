// =====================================================================
// Module: Azure AI Video Indexer account (ARM-based) — Jalon 8
// One-time platform prerequisite (like Foundry/Search), NOT per-client.
// System-assigned managed identity + RBAC on the existing storage
// account (Storage Blob Data Owner — required so VI can read the source
// video blob directly via useManagedIdentityToDownloadVideo, no SAS
// needed, and write its own working data there). No Media Services
// account needed with the ARM-based resource type
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

// Storage Blob Data Owner, not Contributor: confirmed 2026-09-25 against the
// real Upload Video API spec (api-portal.videoindexer.ai) -- the
// useManagedIdentityToDownloadVideo=true upload path (VI's own identity reads
// the source blob directly, no SAS token needed) explicitly requires Owner,
// per that parameter's documented requirement. Owner is a superset of
// Contributor, so this also covers VI's own working-data storage link.
var storageBlobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'

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
// (its own working data + reading the source video blob directly via
// useManagedIdentityToDownloadVideo, no SAS needed).
resource videoIndexerToStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, videoIndexer.id, storageBlobDataOwner)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwner)
    principalId: videoIndexer.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output videoIndexerAccountId string = videoIndexer.id
output videoIndexerPrincipalId string = videoIndexer.identity.principalId
output videoIndexerAccountName string = videoIndexer.name
