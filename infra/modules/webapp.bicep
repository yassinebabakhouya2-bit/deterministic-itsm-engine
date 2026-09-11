// =====================================================================
// Module: Azure Web App — demo interface (Jalon 4)
// Linux App Service (Python/Flask, app/), system-assigned managed
// identity -> keyless RBAC access to Search + Foundry (see roles.bicep).
// No secrets, no Key Vault needed for THOSE two credentials (axiom A5).
//
// Easy Auth (authsettingsV2, added 2026-09-11): a coarse gate in front of
// the whole app -- "signed in to Yassine's own sandbox tenant = in", no
// per-client mapping. This is NOT the real per-user/per-client auth of
// Jalon 5 (which will use access.entraGroup already reserved in each
// engine.<client>.yaml) -- it exists only because deploying the app with
// zero protection at all (the state before this change) was judged too
// exposed even as a stopgap. See project memory jalon4-app-interface.md.
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

@description('Enable the coarse Easy Auth gate (any signed-in user in easyAuthTenantId reaches the app). Stopgap before Jalon 5 real per-client isolation.')
param enableEasyAuth bool = true

@description('Entra ID App Registration (client) ID for Easy Auth -- required when enableEasyAuth is true. See app/README.md for how to create it (az ad app create).')
param easyAuthClientId string = ''

@description('Entra ID tenant ID for Easy Auth -- required when enableEasyAuth is true.')
param easyAuthTenantId string = ''

@description('Entra ID App Registration client secret for Easy Auth -- required when enableEasyAuth is true. Pass at deploy time only (az deployment group create --parameters), never commit it, never put it in a .bicepparam file.')
@secure()
param easyAuthClientSecret string = ''

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
    type: 'SystemAssigned' // keyless RBAC access to Search + Foundry (axiom A5) -- unrelated to Easy Auth below, which uses a separate App Registration
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'gunicorn --bind 0.0.0.0 --chdir app app:app'
    }
  }
}

// App settings as a distinct resource (not the deprecated siteConfig.appSettings
// array) so the Easy Auth secret setting can be added conditionally without
// fighting over which resource owns the app settings collection.
resource appSettings 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: webApp
  name: 'appsettings'
  properties: union(
    {
      ALLOWED_CLIENTS: allowedClients
      SCM_DO_BUILD_DURING_DEPLOYMENT: 'true' // Oryx builds from requirements.txt on push/zip-deploy
    },
    enableEasyAuth ? { MICROSOFT_PROVIDER_AUTHENTICATION_SECRET: easyAuthClientSecret } : {}
  )
}

resource authSettings 'Microsoft.Web/sites/config@2023-12-01' = if (enableEasyAuth) {
  parent: webApp
  name: 'authsettingsV2'
  dependsOn: [
    appSettings // the secret setting must exist before Easy Auth references its name
  ]
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: easyAuthClientId
          clientSecretSettingName: 'MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'
          openIdIssuer: 'https://sts.windows.net/${easyAuthTenantId}/v2.0'
        }
        validation: {
          defaultAuthorizationPolicy: {
            allowedApplications: []
          }
        }
      }
    }
    login: {
      tokenStore: {
        enabled: true
      }
    }
  }
}

output webAppName string = webApp.name
output webAppPrincipalId string = webApp.identity.principalId
output webAppHostName string = webApp.properties.defaultHostName
