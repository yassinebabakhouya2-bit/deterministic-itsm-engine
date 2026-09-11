// =====================================================================
// Module: Azure Web App — demo interface (Jalon 4)
// Linux App Service (Python/Flask, app/), system-assigned managed
// identity -> keyless RBAC access to Search + Foundry (see roles.bicep).
// No secrets, no Key Vault needed for these two credentials (axiom A5).
// =====================================================================

@description('Name of the App Service plan')
param planName string

@description('Name of the Web App')
param webAppName string

@description('Azure region')
param location string

@description('Comma-separated client ids this instance is allowed to query (stopgap before Jalon 5 real auth)')
param allowedClients string

@description('App Service plan SKU')
param skuName string = 'B1'

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  sku: {
    name: skuName
  }
  kind: 'linux'
  properties: {
    reserved: true // required for Linux plans
  }
}

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  identity: {
    type: 'SystemAssigned' // keyless RBAC access to Search + Foundry (axiom A5)
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'gunicorn --bind 0.0.0.0 --chdir app app:app'
      appSettings: [
        {
          name: 'ALLOWED_CLIENTS'
          value: allowedClients
        }
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true' // Oryx builds from requirements.txt on push/zip-deploy
        }
      ]
    }
  }
}

output webAppName string = webApp.name
output webAppPrincipalId string = webApp.identity.principalId
output webAppHostName string = webApp.properties.defaultHostName
