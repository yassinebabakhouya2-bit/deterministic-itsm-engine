// =====================================================================
// Module: ITSM EXECUTOR Logic App -- Jalon 10, step 10.4 (10.4a)
// Every 2 minutes, takes the itsmtickets rows an agent VALIDATED in the web
// app (reviewStatus = 'validated', no executionStatus yet) and, per ticket:
//   1. claims the row (MERGE executionStatus=running with If-Match on its
//      ETag) -> a validated ticket is executed at most once;
//   2. RE-CHECKS the guards against Entra ID at execution time (the directory
//      may have changed since the proposal): subject exists, holds no Entra
//      directory role, action implemented, group still allowlisted, row still
//      'pending_review' + 'validated';
//   3. executes ONLY the approved parameters (approvedParamsJson written by the
//      agent's decision, never the raw LLM output):
//        group_add   -> add the subject to the allowlisted group
//        offboarding -> disable account / revoke sessions / remove from every
//                       static group (dynamic groups are skipped), only the
//                       steps the agent kept;
//   4. ServiceNow: RITM closed (state 3) with a work note on full success;
//      otherwise a work note only, ticket left open for a human;
//   5. writes executionStatus (success / partial / blocked / dry_run / error)
//      and the per-step executionLog back into the row.
//
// NOT in this step (10.4b): password_reset and mfa_reset. They produce a secret
// (temporary password / Temporary Access Pass) that must reach the validating
// agent only, never ServiceNow nor the table -- that delivery channel is not
// built yet, so these actions end as 'blocked' (action_not_implemented) and
// nothing is executed.
//
// SEPARATE IDENTITY from the web app and from the proposal Logic App: this is
// the ONLY component holding Graph WRITE permissions (granted with
// scripts/itsm/grant-graph-app-roles.ps1, runbook 11.7). The web app can only
// record a decision; it can never execute one.
//
// dryRun=true (default): everything runs (claim, re-check) but NO write is made
// to Entra ID or ServiceNow; the row gets executionStatus=dry_run with the plan.
// Created Disabled on a first deployment (startEnabled=false).
// =====================================================================

param clientCode string = 'itsm-demo'
param location string = resourceGroup().location
param namePrefix string = 'knowledgeengine2'
param storageAccountName string = 'st${namePrefix}v9'
param tableName string = 'itsmtickets'
param keyVaultName string = 'kv-knowledgeengine-v9'
param snInstance string = 'dev374242'
param snUser string = 'svc_ke_itsm'
param snSecretName string = 'servicenow-svc-ke-itsm-password'

@description('Must stay in sync with itsm/propose/main.bicep allowedGroups and config/itsm.yaml allowedGroups')
param allowedGroupNames array = [
  'SG-SP-Projets'
  'SG-VPN-Users'
  'SG-App-Planning'
]

@description('true = no write to Entra ID / ServiceNow, only the plan is logged')
param dryRun bool = true

@description('false on a FIRST deployment (Graph app roles not granted yet)')
param startEnabled bool = false

param createRoleAssignments bool = true

var logicAppName = 'logic-itsm-execute-${clientCode}'
var storageTableDataContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storageAccountName
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource logicApp 'Microsoft.Logic/workflows@2019-05-01' = {
  name: logicAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: startEnabled ? 'Enabled' : 'Disabled'
    definition: loadJsonContent('workflow-definition.json')
    parameters: {
      clientCode: { value: clientCode }
      storageAccountName: { value: storageAccountName }
      tableName: { value: tableName }
      snInstance: { value: snInstance }
      snUser: { value: snUser }
      keyVaultName: { value: keyVaultName }
      snSecretName: { value: snSecretName }
      allowedGroupNames: { value: allowedGroupNames }
      dryRun: { value: dryRun }
    }
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

resource kvRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  name: guid(keyVault.id, logicAppName, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output logicAppName string = logicApp.name
output managedIdentityPrincipalId string = logicApp.identity.principalId
