// =====================================================================
// Module: fn-kecore — the V10 deterministic engine on Azure
//
// V10 (kecore / kefind / scoreboard) used to run as local Python CLIs
// writing to clients-local/. Decision 2026-10-05: everything V10 runs on
// Azure, provisioned in Bicep; nothing is created on an operator's machine.
// See docs/v10-deterministic-engine.md, "Azure-native migration".
//
// Slice 1 (this module): infrastructure only, no application code yet.
//   - Function App fn-kecore-<prefix>-v9 on the EXISTING B1 plan, the same
//     hosting choice as the enrichment Function and for the same reason (see
//     the history at the top of enrich-function.bicep: Flex Consumption never
//     started its host in this subscription). From slice 2 on, Durable
//     Functions run the decomposition (profile -> fiches in parallel ->
//     report); alwaysOn keeps their triggers alive. During a run it shares
//     the B1 CPU with the Web App, exactly like the enrichment Function.
//   - Per client: kecore-<client> (profile, decomposed fiches, reports, the
//     LLM record) and tickets-<client> (raw ITSM exports an operator drops
//     under raw/, scrubbed from slice 4 on). One container per client, like
//     kb-<client>: client isolation stays physical.
//   - Telemetry: Application Insights + platform logs, always on. Without
//     them a host that dies at start-up is silent (enrich-function.bicep).
//
// Zero secret: system-assigned identity, identity-based host storage
// (AzureWebJobsStorage__accountName), keyless calls to Azure OpenAI.
//
// Deployable on its own (every default matches main.bicep's naming):
//   az deployment group create -g rg-knowledgeengine-v9 --name kecore \
//     --template-file infra/modules/kecore.bicep
// =====================================================================

@description('Naming prefix shared with main.bicep')
param namePrefix string = 'knowledgeengine3'

@description('Azure region (France Central mandated)')
param location string = 'francecentral'

@description('Function App name')
param functionAppName string = 'fn-kecore-${namePrefix}-v9'

@description('EXISTING App Service plan (created by webapp.bicep), already hosting the Web App and the enrichment Function')
param hostingPlanName string = 'plan-${namePrefix}-v9'

@description('EXISTING storage account (KB containers, tables, host storage)')
param storageAccountName string = 'st${namePrefix}v9'

@description('EXISTING Foundry / AI Services account')
param foundryName string = 'aif-${namePrefix}-v9'

@description('Model deployment used by kecore. Pinned: "<deployment>@<host>" is part of every LLM record key, so changing it (or the account) invalidates the record and pays every call again.')
param kecoreDeployment string = 'gpt-4o'

@description('Azure OpenAI API version used by kecore (same as the local runs it replaces)')
param kecoreApiVersion string = '2024-10-21'

@description('text-embedding-3-large deployment (foundry.bicep): the semantic index of each kecore run and the vector of each question (kefind.semantic). Vectors feed code; the model never picks a fiche.')
param kecoreEmbeddingDeployment string = 'text-embedding-3-large'

@description('Clients served by the V10 engine: one kecore-<client> and one tickets-<client> container each. The Function refuses any client not listed here (deny-by-default).')
param clients array = [
  'clienta'
  'clientb'
  'clientc'
  'client-s'
]

@description('Log retention in days')
param logRetentionDays int = 30

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: foundryName
}

resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' existing = {
  name: hostingPlanName
}

// ---------------------------------------------------------------------
// Per-client containers, private.
// ---------------------------------------------------------------------
resource kecoreContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [
  for client in clients: {
    parent: blobService
    name: 'kecore-${client}'
    properties: {
      publicAccess: 'None'
    }
  }
]

resource ticketContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [
  for client in clients: {
    parent: blobService
    name: 'tickets-${client}'
    properties: {
      publicAccess: 'None'
    }
  }
]

// ---------------------------------------------------------------------
// Telemetry: application (Application Insights) and platform logs.
// ---------------------------------------------------------------------
resource logWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${functionAppName}'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionDays
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${functionAppName}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logWorkspace.id
  }
}

// ---------------------------------------------------------------------
// Function App.
// ---------------------------------------------------------------------
resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'Python|3.11'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      alwaysOn: true
      appSettings: [
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          // Dependencies built server-side on zip deploy
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          // Identity-based host storage (also used by Durable Functions): no connection string
          name: 'AzureWebJobsStorage__accountName'
          value: storage.name
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          // Must stay byte-identical to the endpoint of the local runs: its host is part of the LLM record key
          name: 'KECORE_AOAI_ENDPOINT'
          value: foundry.properties.endpoint
        }
        {
          name: 'KECORE_AOAI_DEPLOYMENT'
          value: kecoreDeployment
        }
        {
          name: 'KECORE_AOAI_API_VERSION'
          value: kecoreApiVersion
        }
        {
          name: 'KECORE_AOAI_EMBEDDING_DEPLOYMENT'
          value: kecoreEmbeddingDeployment
        }
        {
          name: 'KECORE_BLOB_ENDPOINT'
          value: storage.properties.primaryEndpoints.blob
        }
        {
          name: 'KECORE_TABLE_ENDPOINT'
          value: storage.properties.primaryEndpoints.table
        }
        {
          name: 'KECORE_CLIENTS'
          value: join(clients, ',')
        }
      ]
    }
  }
  dependsOn: [
    kecoreContainers
    ticketContainers
  ]
}

// Platform logs, distinct from the application telemetry.
resource functionDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: functionApp
  properties: {
    workspaceId: logWorkspace.id
    logs: [
      {
        category: 'FunctionAppLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

// ---------------------------------------------------------------------
// RBAC of the Function's identity. Four roles, no more:
//   - Blob Data Owner        : host storage + Durable task hub, read
//                              kb-<client>, write kecore-<client> and
//                              tickets-<client> (account scope, like the
//                              enrichment Function: the host and Durable
//                              create their own containers at run time)
//   - Queue Data Contributor : host + Durable work-item / control queues
//   - Table Data Contributor : Durable history / instances tables, and the
//                              engine's own tables in later slices
//   - Cognitive Services OpenAI User : keyless model calls
// No Search role yet: kefind's index arrives in slice 3, with the role it needs.
// ---------------------------------------------------------------------
var storageBlobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageQueueDataContributor = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableDataContributor = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
var cognitiveServicesOpenAIUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource kecoreToBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, storageBlobDataOwner)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwner)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource kecoreToQueue 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, storageQueueDataContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageQueueDataContributor)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource kecoreToTable 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, storageTableDataContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageTableDataContributor)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource kecoreToFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, functionApp.id, cognitiveServicesOpenAIUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUser)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output functionAppName string = functionApp.name
output functionAppHostName string = functionApp.properties.defaultHostName
output functionPrincipalId string = functionApp.identity.principalId
output kecoreContainerNames array = [for client in clients: 'kecore-${client}']
output ticketContainerNames array = [for client in clients: 'tickets-${client}']
