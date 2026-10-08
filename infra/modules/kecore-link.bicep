// =====================================================================
// Module: kecore-link — the Web App calls the deterministic engine (V10 slice 5)
//
// The Diagnostic tab finds the fiche with fn-kecore (POST /api/kecore/find) before the search
// index, and the dictionary review tab reads and decides through its API. The Web App calls the
// Function App with its function key; that key is never in code, a file or a plain app setting:
//   - this module copies the Function's default host key into Key Vault (secret
//     kecore-function-key), read with listKeys at deployment time;
//   - the Web App's managed identity may read THAT secret only (Key Vault Secrets User at the
//     secret's scope, not the vault's);
//   - the Web App's setting KECORE_FUNCTION_KEY is a Key Vault reference, resolved by App Service
//     with that identity. KECORE_FUNCTION_URL is the Function's API base.
// The two settings are MERGED into the existing ones (list() of the current app settings): nothing
// else is touched. If webapp.bicep (main.bicep) is deployed again later, deploy this module again
// after it. After a rotation of the function key, deploy this module again too.
//
// Deployable on its own:
//   az deployment group create -g rg-knowledgeengine-v9 --name kecore-link \
//     --template-file infra/modules/kecore-link.bicep
// =====================================================================

@description('Naming prefix shared with main.bicep')
param namePrefix string = 'knowledgeengine3'

@description('EXISTING Function App of the engine (kecore.bicep)')
param functionAppName string = 'fn-kecore-${namePrefix}-v9'

@description('EXISTING Web App (webapp.bicep)')
param webAppName string = 'app-${namePrefix}-v9'

@description('EXISTING Key Vault, RBAC mode (created by scripts/bootstrap-new-tenant.ps1)')
param keyVaultName string = 'kv-${namePrefix}-v9'

@description('Name of the secret holding the function key')
param secretName string = 'kecore-function-key'

resource functionApp 'Microsoft.Web/sites@2023-12-01' existing = {
  name: functionAppName
}

resource webApp 'Microsoft.Web/sites@2023-12-01' existing = {
  name: webAppName
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource keySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: secretName
  properties: {
    value: listKeys('${functionApp.id}/host/default', '2023-12-01').functionKeys.default
    contentType: 'default function key of ${functionAppName}, read by ${webAppName} through a Key Vault reference'
  }
}

// Built-in role: Key Vault Secrets User (read secret values), scoped to this one secret.
var keyVaultSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'

resource webAppReadsKey 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keySecret.id, webApp.id, keyVaultSecretsUser)
  scope: keySecret
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUser)
    principalId: webApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource webAppSettings 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: webApp
  name: 'appsettings'
  properties: union(list('${webApp.id}/config/appsettings', '2023-12-01').properties, {
    KECORE_FUNCTION_URL: 'https://${functionApp.properties.defaultHostName}/api'
    KECORE_FUNCTION_KEY: '@Microsoft.KeyVault(SecretUri=${keySecret.properties.secretUri})'
  })
  dependsOn: [
    webAppReadsKey
  ]
}

output functionUrl string = 'https://${functionApp.properties.defaultHostName}/api'
output secretUri string = keySecret.properties.secretUri
