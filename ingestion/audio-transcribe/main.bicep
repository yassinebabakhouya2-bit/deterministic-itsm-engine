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
//
// Design note (2026-09-19 -- PII redaction + resolution extraction move
// upstream to ingestion): app/app.py's display-time defenses (never render
// a raw call excerpt, LLM-written safe_summary with a heuristic backstop)
// only ever controlled what the WEB APP shows -- the Azure AI Search INDEX
// itself still held the raw, unredacted transcript text underneath, since
// nothing upstream of indexing ever touched it. Per Yassine: fix it at the
// source with a real semantic PII detector, not a display-layer workaround,
// and extract the actual resolution steps, not a vague paraphrase. Two new
// workflow-definition.json steps, right after the merged Canal 0/Canal 1
// text is built and BEFORE it is ever written to kb-<client>:
//   1. Azure AI Language Conversation PII redaction (ConversationalPIITask)
//      -- a trained model over the whole diarized conversation, not a
//      regex; this is what should have caught the character-by-character
//      password dictation in the first place.
//   2. Azure AI Language Conversation Summarization (ConversationalSummarizationTask,
//      summaryAspects: issue + resolution) -- Microsoft's own aspect
//      literally described as "Summary of resolutions in transcripts of
//      ... service calls between customer-service agents and customers",
//      run on the ALREADY-REDACTED text.
// Both run against the SAME Foundry account (kind: AIServices already
// bundles Language) already used for GPT-4o -- no new resource, just the
// cognitiveServicesUserRoleId assignment below. FAIL-CLOSED by design: if
// either job fails or the response doesn't parse as expected, the
// workflow does NOT fall back to writing the raw transcript -- it leaves
// the source blob unmarked (transcribed stays unset) so it's retried on
// the next run, rather than silently reintroducing the leak. The
// app-layer safe_summary/_looks_sensitive() backstop (orchestration/
// answer.py, app/app.py) stays in place as defense-in-depth, now as a
// SECOND layer behind this one, not the only one.
//
// IMPORTANT -- verify before a full backfill: the exact request shape was
// confirmed against Microsoft's own docs and a live API error message
// (task kind strings ConversationalPIITask / ConversationalSummarizationTask,
// analysisInput.conversations[].conversationItems[] fields), but the
// precise RESPONSE field names (redactedText path, summaries[].aspect/.text
// path) could not be confirmed against a literal example -- this was built
// from the well-established Azure "AnalyzeConversation" async-job response
// shape, not verified live (no Azure credentials in the authoring
// environment). Test on ONE file first and inspect that Logic App run's
// action outputs in the Azure Portal before trusting it for the backfill
// of already-transcribed files (N°7.txt and friends) -- see the redeploy
// notes handed to Yassine for the exact steps.
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

@description('Azure AI Search service — the Logic App triggers ix-<clientCode>-text after writing transcripts, so audio content is searchable without waiting for a manual/scheduled indexer run')
param searchServiceName string = 'srch-${namePrefix}-v9'

@description('Key Vault secret name holding the Azure AI Speech resource key')
param speechSecretName string = 'speech-key'

@description('Azure AI Speech resource endpoint, e.g. https://aif-knowledgeengine2-v9.cognitiveservices.azure.com')
param speechEndpoint string

@description('Speech-to-text batch transcription locale')
param locale string = 'fr-FR'

@description('Azure AI Foundry resource name (multi-service AIServices account, Jalon 7+, 2026-09-19) -- used for Conversation PII redaction + Resolution/Issue extraction (Azure AI Language) BEFORE a transcript is ever written to Blob/indexed. Same resource orchestration/answer.py already calls for GPT-4o -- see infra/modules/foundry.bicep (kind: AIServices bundles Language under the same endpoint).')
param foundryName string = 'aif-${namePrefix}-v9'

@description('Azure AI Language "analyze-conversations" jobs API version -- Conversation PII + Conversation Summarization async job submission')
param languageApiVersion string = '2024-05-01'

@description('Polling interval while waiting for a transcription job to finish')
param pollIntervalSeconds int = 10

@description('Max polling attempts per file before giving up (pollIntervalSeconds * pollMaxAttempts = max wait per file)')
param pollMaxAttempts int = 60

@description('Create the 5 RBAC role assignments (Key Vault Secrets User, Storage Blob Data Contributor, Storage Account Contributor, Search Service Contributor, Cognitive Services User on the Foundry account). Set to false if they already exist for this identity (e.g. a Logic App being redeployed in place).')
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
// Data-plane role that lets a managed identity run/manage indexers, indexes,
// skillsets and data sources (but not read/write documents) — required for
// the Run_search_indexer action. Distinct from "Search Index Data Reader"
// (granted to the Web App's identity in infra/modules/roles.bicep, which can
// query documents but not trigger an indexer run).
var searchServiceContributorRoleId = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
// Data-plane access to call any Cognitive Services API on the Foundry
// account (Microsoft.CognitiveServices/* data actions) via the Logic App's
// managed identity -- Language's Conversation PII + Summarization "analyze
// conversations" jobs (Jalon 7+, 2026-09-19). GUID verified against
// Microsoft's own built-in-roles docs, not guessed -- "Cognitive Services
// OpenAI User" (used elsewhere in this project for GPT-4o) is scoped to
// OpenAI operations only and does NOT cover Language.
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storageAccountName
}

resource searchService 'Microsoft.Search/searchServices@2023-11-01' existing = {
  name: searchServiceName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: foundryName
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
      searchServiceName: { value: searchServiceName }
      languageEndpoint: { value: foundry.properties.endpoint }
      languageApiVersion: { value: languageApiVersion }
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

output logicAppName string = logicApp.name
output managedIdentityPrincipalId string = logicApp.identity.principalId
