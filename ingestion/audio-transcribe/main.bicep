// =====================================================================
// Module: Audio transcription (Logic App, per client) — Jalon 7
// Reads blobs from audioContainerName (raw audio dropped by the
// ingestion Logic App), transcribes each untranscribed one with Azure
// AI Speech Batch Transcription, writes a formatted .txt transcript
// into containerName (kb-<client>, reusing the existing "-text"
// indexing pipeline unchanged), and tags the source blob
// transcribed=true so it isn't re-processed on the next daily run.
//
// Zero-connector design, same as ingestion: every step is a native
// HTTP action authenticated via system-assigned managed identity —
// no Microsoft.Web/connections resource, no custom code.
//
// One exception to "no stored secret": the Speech resource key still
// lives in Key Vault (fetched via managed identity, never hardcoded).
// Migrating to Speech's own Entra ID auth (custom subdomain +
// speechservicesmanagement scope) is a future improvement, not
// blocking — see jalon7-plan.md.
// =====================================================================

@description('Client identifier, matching config/engine.<clientCode>.yaml (e.g. "client-s")')
param clientCode string

@description('Source Blob container holding raw audio files, dropped there by the ingestion Logic App (Jalon 7)')
param audioContainerName string = 'audio-raw-${clientCode}'

@description('Destination Blob container for the formatted .txt transcripts — same container the ingestion Logic App writes documents to, reusing the existing indexing pipeline')
param containerName string = 'kb-${clientCode}'

param location string = resourceGroup().location

@description('Project naming prefix — matches infra/main.bicep')
param namePrefix string = 'knowledgeengine2'

param keyVaultName string = 'kv-knowledgeengine-v9'
param storageAccountName string = 'st${namePrefix}v9'

@description('Key Vault secret name holding the Azure AI Speech resource key')
param speechSecretName string = 'speech-key'

@description('Azure AI Speech resource endpoint, e.g. https://aif-knowledgeengine2-v9.cognitiveservices.azure.com')
param speechEndpoint string

@description('Speech-to-text batch transcription locale')
param locale string = 'fr-FR'

@description('Polling interval while waiting for a transcription job to finish')
param pollIntervalSeconds int = 10

@description('Max polling attempts per file before giving up (pollIntervalSeconds * pollMaxAttempts = max wait per file)')
param pollMaxAttempts int = 60

@description('Create the 3 RBAC role assignments (Key Vault Secrets User, Storage Blob Data Contributor, Storage Account Contributor). Set to false if they already exist for this identity (e.g. a Logic App being redeployed in place).')
param createRoleAssignments bool = true

var logicAppName = 'logic-transcribe-${clientCode}'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
// Management-plane role (NOT data-plane) — required only for the ListServiceSas
// ARM call that generates a per-blob read-only SAS token server-side, with zero
// HMAC signing code and zero stored account key. Broader than strictly needed
// (account-level management access, not just SAS generation) — acceptable for
// now, tighten later with a custom role definition if needed.
var storageAccountContributorRoleId = '17d1049b-9a84-46fb-8f53-869881c3d3ab'

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
      storageAccountName: { value: storageAccountName }
      audioContainerName: { value: audioContainerName }
      containerName: { value: containerName }
      clientId: { value: clientCode }
      keyVaultName: { value: keyVaultName }
      speechSecretName: { value: speechSecretName }
      speechEndpoint: { value: speechEndpoint }
      locale: { value: locale }
      subscriptionId: { value: subscription().subscriptionId }
      resourceGroupName: { value: resourceGroup().name }
      pollIntervalSeconds: { value: pollIntervalSeconds }
      pollMaxAttempts: { value: pollMaxAttempts }
    }
  }
}

// NOTE — before the first run:
// 1. Create the Key Vault secret holding the Speech resource key:
//    az keyvault secret set --vault-name kv-knowledgeengine-v9 --name speech-key --value <speech-key1>
// 2. The audioContainerName container must already exist and contain audio
//    (it does, populated by the ingestion Logic App — logic-ingest-<client>).
// 3. containerName (kb-<client>) must already exist (it does, used by ingestion).

resource kvRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  name: guid(keyVault.id, logicAppName, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storageDataRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  name: guid(storageAccount.id, logicAppName, storageBlobDataContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storageMgmtRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  name: guid(storageAccount.id, logicAppName, storageAccountContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageAccountContributorRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output logicAppName string = logicApp.name
output managedIdentityPrincipalId string = logicApp.identity.principalId
