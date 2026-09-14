// =====================================================================
// Module: SharePoint → Blob ingestion (Logic App, per client)
// Zero-connector design: every step is a native HTTP action authenticated
// via system-assigned managed identity (Key Vault + Blob) — no
// Microsoft.Web/connections resource, no custom code, no secret ever
// stored outside Key Vault.
// Reproducible & parameterized → same template deployed once per client,
// matching axiom A2 (client-agnosticism): onboarding a new client is a
// new parameters file, never a template change.
//
// UPDATED 2026-09-13 (Jalon 7 - Audio): the file listing now walks the
// SharePoint drive recursively (/delta instead of /children, see
// workflow-definition.json) and routes each discovered file by extension —
// audio files go to `audioContainerName` (new), everything else keeps
// going to `containerName` exactly as before. Blob names now preserve the
// full relative folder path instead of just the file name — this is a
// no-op for a file already at the drive root ([CLIENT-PARENT]/[CLIENT-PROD] today), and only
// changes behavior for files in subfolders (newly discovered thanks to the
// recursive walk).
// =====================================================================

@description('Client identifier, matching config/engine.<clientCode>.yaml (e.g. "clienta")')
param clientCode string

@description('Full SharePoint site ID (format: hostname,siteCollectionId,webId)')
param siteId string

@description('Destination Blob container name for non-audio documents')
param containerName string = 'kb-${clientCode}'

@description('Destination Blob container name for audio files discovered anywhere in the site (Jalon 7)')
param audioContainerName string = 'audio-raw-${clientCode}'

@description('Additional query string on the Graph listing call. Empty in production; "?$top=1" to smoke-test with a single file before a full ingestion.')
param listQuery string = ''

param location string = resourceGroup().location

@description('Project naming prefix — matches infra/main.bicep')
param namePrefix string = 'knowledgeengine2'

param keyVaultName string = 'kv-knowledgeengine-v9'
param secretName string = 'ingestion-secret-v2'
param storageAccountName string = 'st${namePrefix}v9'

@description('Entra ID tenant hosting the SharePoint site. No default on purpose — supply via your own untracked parameters file.')
param tenantId string

@description('App Registration client ID, granted Sites.Selected on the target site via Graph. No default on purpose.')
param appClientId string

@description('Create the 2 RBAC role assignments (Key Vault Secrets User, Storage Blob Data Contributor). Set to false if they already exist for this identity (e.g. a Logic App being redeployed in place).')
param createRoleAssignments bool = true

var logicAppName = 'logic-ingest-${clientCode}'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storageAccountName
}

resource logicApp 'Microsoft.Logic/workflows@2019-05-01' = {
  name: logicAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: 'Enabled'
    definition: loadJsonContent('workflow-definition.json')
    parameters: {
      siteId: { value: siteId }
      tenantId: { value: tenantId }
      appClientId: { value: appClientId }
      keyVaultName: { value: keyVaultName }
      secretName: { value: secretName }
      storageAccountName: { value: storageAccountName }
      containerName: { value: containerName }
      audioContainerName: { value: audioContainerName }
      clientId: { value: clientCode }
      listQuery: { value: listQuery }
    }
  }
}

// Storage RBAC (Storage Blob Data Contributor) is granted at the STORAGE
// ACCOUNT scope below, not per-container — so the new audioContainerName
// needs no additional role assignment, provided the container itself
// exists before the Logic App's first run (create it once, e.g.
// `az storage container create --name audio-raw-<client> --account-name
// stknowledgeengine2v9 --auth-mode login`, or let the PUT blob call create
// it implicitly — Azure Blob Storage does NOT auto-create containers on
// PUT blob, so create it explicitly first).

resource kvRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  name: guid(keyVault.id, logicAppName, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  name: guid(storageAccount.id, logicAppName, storageBlobDataContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output logicAppName string = logicApp.name
output managedIdentityPrincipalId string = logicApp.identity.principalId
