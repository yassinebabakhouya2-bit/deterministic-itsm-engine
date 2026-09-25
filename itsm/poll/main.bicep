// =====================================================================
// Module: ITSM ticket polling (Logic App) -- Jalon 10, step 10.1
// Every 5 minutes, reads the ACTIVE incidents and requested items
// (sc_req_item) assigned to one ServiceNow group (KE-Automation) through
// the ServiceNow Table API, and upserts a snapshot of each ticket into an
// Azure Table (itsmtickets, PartitionKey = clientCode, RowKey = ticket
// number). Read-only towards ServiceNow: no ticket is modified here.
//
// Same zero-connector design as ingestion/ and ingestion/audio-transcribe/:
// native HTTP actions only, managed identity for Azure (Key Vault, Table
// storage), no Microsoft.Web/connections resource, no custom code. Chosen
// over an Azure Function on 2026-09-25 to keep the "managed services only,
// no custom code running in the pipeline" principle.
//
// Only stored secret: the password of the ServiceNow integration user
// (svc_ke_itsm, Internal Integration User, role itil), in Key Vault,
// fetched at run time via managed identity. Both the secret fetch
// outputs and the ServiceNow call inputs are marked secureData so the
// password never appears in Logic App run history.
//
// Upsert = Azure Table "Insert Or Merge Entity" (MERGE tunnelled through
// POST with X-HTTP-Method): only the ServiceNow snapshot fields are
// written, so fields added later by the module (proposal, review status,
// validator...) are never overwritten by the poller.
//
// VERIFY ON FIRST RUN (not tested live when authored -- no Azure
// credentials in the authoring environment): (1) the MERGE-over-POST
// tunnelling against the Table endpoint with OAuth; (2) the dot-walked
// field names (caller_id.email, requested_for.email) coming back as flat
// keys in the Table API JSON. Check the run's action outputs in the portal.
// =====================================================================

@description('Logical client code for the ITSM module (PartitionKey in the table)')
param clientCode string = 'itsm-demo'

@description('ServiceNow instance name, e.g. dev374242')
param snInstance string

@description('ServiceNow integration user (Internal Integration User, role itil)')
param snUser string = 'svc_ke_itsm'

@description('ServiceNow assignment group whose queue the module works on')
param snAssignmentGroup string = 'KE-Automation'

@description('Key Vault secret holding the password of snUser')
param snSecretName string = 'servicenow-svc-ke-itsm-password'

param location string = resourceGroup().location

@description('Project naming prefix -- matches infra/main.bicep')
param namePrefix string = 'knowledgeengine2'

param keyVaultName string = 'kv-knowledgeengine-v9'
param storageAccountName string = 'st${namePrefix}v9'
param tableName string = 'itsmtickets'

@description('Create the RBAC role assignments. Set to false when redeploying in place and they already exist.')
param createRoleAssignments bool = true

var logicAppName = 'logic-itsm-poll-${clientCode}'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
// Storage Table Data Contributor (data plane, tables only)
var storageTableDataContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storageAccountName
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource ticketsTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-01-01' = {
  parent: tableService
  name: tableName
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
      clientCode: { value: clientCode }
      snInstance: { value: snInstance }
      snUser: { value: snUser }
      snAssignmentGroup: { value: snAssignmentGroup }
      keyVaultName: { value: keyVaultName }
      snSecretName: { value: snSecretName }
      storageAccountName: { value: storageAccountName }
      tableName: { value: tableName }
    }
  }
  dependsOn: [ ticketsTable ]
}

// NOTE -- before the first run, create the secret (see docs/operations-runbook.md 11.4):
//   az keyvault secret set --vault-name kv-knowledgeengine-v9 --name servicenow-svc-ke-itsm-password --file <tmpfile>

resource kvRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  name: guid(keyVault.id, logicAppName, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource tableRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  name: guid(storageAccount.id, logicAppName, storageTableDataContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageTableDataContributorRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output logicAppName string = logicApp.name
output managedIdentityPrincipalId string = logicApp.identity.principalId
