// =====================================================================
// Module: Video indexing (Logic App, per client) — Jalon 8
// Reads video blobs from videoContainerName (raw video dropped by the
// ingestion Logic App, see ingestion/main.bicep's video routing), indexes
// each unindexed one with Azure AI Video Indexer, writes a formatted .txt
// (timestamped transcript + on-screen text (OCR) + detected topics) into
// containerName (kb-<client>, same "-text" indexing pipeline as audio and
// documents, unchanged), and tags the source blob videoindexed=true so it
// isn't re-processed (re-billed) on the next daily run.
//
// Zero-connector design, same as ingestion/audio-transcribe: every step is
// a native HTTP action authenticated via system-assigned managed identity
// — no Microsoft.Web/connections resource, no custom code.
//
// UNLIKE audio-transcribe: no Key Vault secret at all. Video Indexer's
// ARM-based account uses a two-step MSI auth chain instead of a stored
// subscription key — ARM bearer token (native ManagedServiceIdentity
// authentication on the HTTP action, audience https://management.azure.com/)
// -> POST .../generateAccessToken (ARM management-plane action) -> a
// short-lived data-plane JWT used against api.videoindexer.ai. Confirmed
// live 2026-09-25 (see docs/operations-runbook.md §10.3ter, §10.7) —
// including that the data-plane JWT is SHORT-LIVED and must be refreshed
// inside the poll loop, not just once before it (a single video's
// processing can run long past the token's TTL — confirmed empirically
// during the manual test, 10.6/10.7).
//
// UNLIKE audio-transcribe: the Logic App's own managed identity never
// reads the video bytes and needs NO storage-read role for that purpose.
// Video Indexer's OWN managed identity reads the blob directly via
// useManagedIdentityToDownloadVideo=true + a plain blob URL (no SAS) —
// already granted Storage Blob Data Owner on the storage account in
// infra/modules/videoindexer.bicep (§10.5bis). The Logic App identity here
// only needs Storage Blob Data Contributor to WRITE the final .txt and to
// mark the source blob's metadata, same as audio.
//
// PII redaction: applied to the SPOKEN TRANSCRIPT only (Azure AI Language
// ConversationalPIITask, same Foundry/Language resource as audio, same
// fail-closed design — a PII job failure leaves the source blob unmarked
// so it's retried next run, the unredacted text is NEVER written). The
// on-screen text (OCR) is NOT redacted in this first version — it is raw
// UI text from a software tutorial recording, judged lower-risk than a
// spoken support call, but this is a judgement call, not a proven-safe
// default; revisit if OCR content policy needs to change. No conversation
// "resolution" summarization task (audio-call-specific, not relevant to a
// tutorial video) — only PII.
// =====================================================================

@description('Client identifier, matching config/engine.<clientCode>.yaml (e.g. "client-s")')
param clientCode string

@description('Source Blob container holding raw video files, dropped there by the ingestion Logic App (Jalon 8) — never indexed directly from kb-<client>')
param videoContainerName string = 'video-raw-${clientCode}'

@description('Destination Blob container for the formatted .txt transcripts — same container the ingestion Logic App writes documents to, reusing the existing indexing pipeline')
param containerName string = 'kb-${clientCode}'

param location string = resourceGroup().location

@description('Project naming prefix — matches infra/main.bicep')
param namePrefix string = 'knowledgeengine3'

param storageAccountName string = 'st${namePrefix}v9'

@description('Azure AI Search service — the Logic App triggers ix-<clientCode>-text after writing transcripts, so video content is searchable without waiting for a manual/scheduled indexer run')
param searchServiceName string = 'srch-${namePrefix}-v9'

@description('Azure AI Foundry resource name (multi-service AIServices account) — used for Conversation PII redaction on the spoken transcript, same resource orchestration/answer.py already calls for GPT-4o and audio-transcribe already calls for the same purpose')
param foundryName string = 'aif-${namePrefix}-v9'

@description('Azure AI Language "analyze-conversations" jobs API version')
param languageApiVersion string = '2024-05-01'

@description('Video Indexer ARM resource name (platform prerequisite, deployed once via infra/modules/videoindexer.bicep — NOT per-client)')
param videoIndexerAccountName string = 'vi-${namePrefix}-v9'

@description('Video Indexer INTERNAL account GUID (properties.accountId on the ARM resource — distinct from videoIndexerAccountName, required for every data-plane call). See docs/operations-runbook.md §10.5.')
param videoIndexerAccountId string

@description('Video Indexer data-plane region — lowercase ARM region code, NOT a display name (confirmed live, §10.3ter)')
param videoIndexerLocation string = 'francecentral'

@description('Video Indexer ARM API version for generateAccessToken')
param videoIndexerArmApiVersion string = '2024-01-01'

@description('Speech/OCR recognition language for Video Indexer, BCP-47 (real client-s videos are French; the one-off manual test used an English demo asset with fr-FR forced on purpose, giving a low-quality transcript on THAT test only — see §10.7)')
param sourceLanguage string = 'fr-FR'

@description('Video Indexer indexing preset — Default confirmed sufficient for Transcript+OCR+Topics, no Advanced tier needed (§10.3, §10.7)')
param indexingPreset string = 'Default'

@description('Polling interval while waiting for a video to finish processing')
param pollIntervalSeconds int = 30

@description('Max polling attempts per file before giving up (pollIntervalSeconds * pollMaxAttempts = max wait per file — 30s * 240 = 2h, sized for long tutorial videos)')
param pollMaxAttempts int = 240

@description('Create the 3 RBAC role assignments (Storage Blob Data Contributor, Search Service Contributor, Cognitive Services User on the Foundry account, Contributor scoped to the Video Indexer account). Set to false if they already exist for this identity (e.g. a Logic App being redeployed in place).')
param createRoleAssignments bool = true

@description('First run of the daily Recurrence trigger, UTC. Default: 1 h 30 after the deployment: the roles granted below are active by then, and the first SharePoint ingestion (1 h after its own deployment) has brought the video files. Then daily at that time.')
param firstRunUtc string = dateTimeAdd(utcNow('u'), 'PT90M', 'yyyy-MM-ddTHH:mm:ssZ')

var baseDefinition = loadJsonContent('workflow-definition.json')
var workflowDefinition = union(baseDefinition, {
  triggers: {
    Recurrence: union(baseDefinition.triggers.Recurrence, {
      recurrence: union(baseDefinition.triggers.Recurrence.recurrence, { startTime: firstRunUtc })
    })
  }
})

var logicAppName = 'logic-video-index-${clientCode}'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var searchServiceContributorRoleId = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
// No fine-grained built-in role covers just "generateAccessToken" on a
// Video Indexer ARM resource -- Contributor is the narrowest built-in role
// that includes it, scoped here to the Video Indexer account only (not
// the whole resource group), same narrow-scope-with-a-coarse-role
// tradeoff already accepted for storageAccountContributorRoleId in
// audio-transcribe/main.bicep.
var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storageAccountName
}

resource searchService 'Microsoft.Search/searchServices@2023-11-01' existing = {
  name: searchServiceName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: foundryName
}

resource videoIndexer 'Microsoft.VideoIndexer/accounts@2024-01-01' existing = {
  name: videoIndexerAccountName
}

resource logicApp 'Microsoft.Logic/workflows@2019-05-01' = {
  name: logicAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: 'Enabled'
    definition: workflowDefinition
    parameters: {
      storageAccountName: { value: storageAccountName }
      videoContainerName: { value: videoContainerName }
      containerName: { value: containerName }
      clientId: { value: clientCode }
      searchServiceName: { value: searchServiceName }
      languageEndpoint: { value: foundry.properties.endpoint }
      languageApiVersion: { value: languageApiVersion }
      subscriptionId: { value: subscription().subscriptionId }
      resourceGroupName: { value: resourceGroup().name }
      videoIndexerAccountName: { value: videoIndexerAccountName }
      videoIndexerAccountId: { value: videoIndexerAccountId }
      videoIndexerLocation: { value: videoIndexerLocation }
      videoIndexerArmApiVersion: { value: videoIndexerArmApiVersion }
      sourceLanguage: { value: sourceLanguage }
      indexingPreset: { value: indexingPreset }
      pollIntervalSeconds: { value: pollIntervalSeconds }
      pollMaxAttempts: { value: pollMaxAttempts }
    }
  }
}

// NOTE — before the first run:
// 1. The videoContainerName container must exist and contain video files
//    before this Logic App's first run — created by the ingestion Logic
//    App's video routing (ingestion/main.bicep, redeployed with the new
//    videoContainerName param) or manually:
//    az storage container create --name video-raw-<client> --account-name stknowledgeengine3v9 --auth-mode login
// 2. containerName (kb-<client>) must already exist (it does, used by ingestion).
// 3. The Video Indexer account (videoindexer.bicep) must already have
//    Storage Blob Data Owner on storageAccountName (§10.5bis) — otherwise
//    useManagedIdentityToDownloadVideo will fail with USER_NOT_ALLOWED.

resource storageDataRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  name: guid(storageAccount.id, logicAppName, storageBlobDataContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource searchRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  name: guid(searchService.id, logicAppName, searchServiceContributorRoleId)
  scope: searchService
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchServiceContributorRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource foundryRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  name: guid(foundry.id, logicAppName, cognitiveServicesUserRoleId)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource videoIndexerRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  name: guid(videoIndexer.id, logicAppName, contributorRoleId)
  scope: videoIndexer
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output logicAppName string = logicApp.name
output managedIdentityPrincipalId string = logicApp.identity.principalId
