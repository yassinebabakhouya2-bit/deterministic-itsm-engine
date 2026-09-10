// =====================================================================
// Module: Blob storage — KB records per client (private containers)
// =====================================================================

@description('Storage account name (3-24 chars, lowercase/digits, globally unique)')
param storageAccountName string

@description('Azure region')
param location string

@description('KB containers, one per client (clientId). Private, no anonymous access.')
param kbContainers array = [
  'kb-clienta'
  'kb-clientb'
  'kb-clientc'
]

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS' // locally redundant → everything stays in France Central
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false // security: no public access at the account level
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled' // to restrict (private endpoint) at the security hardening milestone
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    // Recycle bin / soft-delete: recovery after accidental deletion (data protection)
    deleteRetentionPolicy: {
      enabled: true
      days: 7
      allowPermanentDelete: false
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [
  for c in kbContainers: {
    parent: blobService
    name: c
    properties: {
      publicAccess: 'None'
    }
  }
]

output storageAccountId string = storage.id
output storageAccountName string = storage.name
