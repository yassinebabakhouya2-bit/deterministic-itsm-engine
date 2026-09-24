// =====================================================================
// Module: Azure Web App — demo interface (Jalon 4, auth hardened Jalon 5)
// Linux App Service (Python/Flask, app/), system-assigned managed
// identity -> keyless RBAC access to Search + Foundry (see roles.bicep).
// No secrets, no Key Vault needed for THOSE two credentials (axiom A5).
//
// Easy Auth (authsettingsV2, added 2026-09-11, extended Jalon 5): gates
// the whole app on a valid Entra ID sign-in. Two real defenses now stack
// on top of that gate (neither existed at Jalon 4):
//   1. WEBSITE_AUTH_AAD_ALLOWED_TENANTS (platform-level, this file) --
//      Microsoft-documented app setting restricting which Entra tenants
//      may even complete sign-in, checked against the `tid` claim before
//      the request reaches app code at all. Required as soon as
//      easyAuthMultiTenant is true: Microsoft's own docs are explicit
//      that a multi-tenant Easy Auth app "doesn't validate which tenant
//      the request comes from" on its own.
//   2. app/auth.py (code-level) -- resolves tenant+group to a client_id,
//      deny-by-default. Still needed even with (1): (1) only says WHICH
//      tenants may sign in, not which client each one may query, and a
//      tenant hosting several clients (Yassine's own sandbox) still
//      needs the group-level split.
// ALLOWED_CLIENTS (the Jalon 4 stopgap env var) is gone -- app/app.py no
// longer reads it. See app/README.md and project memory
// jalon5-auth-isolation.md for the full design and the exact az commands
// needed on the App Registration itself (groupMembershipClaims,
// signInAudience) -- neither is a Bicep-managed resource.
// =====================================================================

@description('Name of the App Service plan')
param planName string

@description('Name of the Web App')
param webAppName string

@description('Azure region')
param location string

@description('App Service plan SKU')
param skuName string = 'B1'

@description('Enable the Easy Auth gate (require a signed-in Entra ID user to reach the app at all). Client-level isolation beyond this is handled by app/auth.py (Jalon 5), not by this flag.')
param enableEasyAuth bool = true

@description('Entra ID App Registration (client) ID for Easy Auth -- required when enableEasyAuth is true. See app/README.md for how to create it (az ad app create).')
param easyAuthClientId string = ''

@description('Entra ID tenant ID for Easy Auth when easyAuthMultiTenant is false -- required in that case (single-tenant issuer). Ignored (but harmless to leave set) when easyAuthMultiTenant is true.')
param easyAuthTenantId string = ''

@description('Entra ID App Registration client secret for Easy Auth -- required when enableEasyAuth is true. Pass at deploy time only (az deployment group create --parameters), never commit it, never put it in a .bicepparam file.')
@secure()
param easyAuthClientSecret string = ''

@description('Jalon 5: allow sign-in from any Microsoft Entra tenant (external organizations), not just easyAuthTenantId. Flipping this alone does nothing -- the App Registration itself must also be switched to multi-tenant first (az ad app update --set signInAudience=AzureADMultipleOrgs, see app/README.md). Defaults to false so existing single-tenant deployments are byte-for-byte unchanged.')
param easyAuthMultiTenant bool = false

@description('Jalon 5: Entra tenant IDs allowed to complete sign-in at all, enforced by the platform itself (WEBSITE_AUTH_AAD_ALLOWED_TENANTS, checked against the tid claim before the request reaches app code) -- Microsoft caps this at 10 tenant IDs. Always include your own tenant; add the tenant ID of an external organization here once its engine.<client>.yaml is onboarded (see app/auth.py). Leaving this empty removes the platform-level restriction entirely -- the allowlist in app/auth.py becomes the ONLY defense, which matters a lot once easyAuthMultiTenant is true (any tenant could otherwise complete sign-in after admin consent).')
param easyAuthAllowedTenantIds array = []

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
      SCM_DO_BUILD_DURING_DEPLOYMENT: 'true' // Oryx builds from requirements.txt on push/zip-deploy
    },
    enableEasyAuth ? { MICROSOFT_PROVIDER_AUTHENTICATION_SECRET: easyAuthClientSecret } : {},
    // Platform-level tenant allowlist (Jalon 5) -- see param description above
    // for why this matters once easyAuthMultiTenant is true.
    !empty(easyAuthAllowedTenantIds) ? { WEBSITE_AUTH_AAD_ALLOWED_TENANTS: join(easyAuthAllowedTenantIds, ',') } : {}
  )
}

// NOT conditional on enableEasyAuth (if (enableEasyAuth) { ... }): ARM/Bicep does
// not delete a conditional resource when its condition later flips to true ->
// false -- it just stops managing it, leaving the old authsettingsV2 active
// and orphaned (hit this for real on 2026-09-11: disabling Easy Auth via
// Bicep did nothing until the resource was patched directly with
// `az resource update ... --set properties.platform.enabled=false`). So this
// resource always exists; enableEasyAuth only toggles platform.enabled.
resource authSettings 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: webApp
  name: 'authsettingsV2'
  dependsOn: [
    appSettings // the secret setting must exist before Easy Auth references its name
  ]
  properties: {
    platform: {
      enabled: enableEasyAuth
    }
    globalValidation: {
      requireAuthentication: enableEasyAuth
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: easyAuthClientId
          clientSecretSettingName: 'MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'
          // Jalon 5: Microsoft's documented default issuer for an "Any
          // Microsoft Entra directory - Multitenant" App Registration is
          // the /organizations/v2.0 endpoint below -- NOT independently
          // verified against EasyAuth specifically (community reports are
          // mixed; some say EasyAuth only accepts /common/v2.0 for
          // multi-tenant and rejects /organizations/v2.0 with an issuer
          // validation error). If login breaks after switching
          // easyAuthMultiTenant to true, try
          // 'https://login.microsoftonline.com/common/v2.0' instead
          // (personal Microsoft accounts would then also technically be
          // able to attempt sign-in, but WEBSITE_AUTH_AAD_ALLOWED_TENANTS
          // above still blocks anyone outside easyAuthAllowedTenantIds).
          openIdIssuer: easyAuthMultiTenant
            ? 'https://login.microsoftonline.com/organizations/v2.0'
            : 'https://sts.windows.net/${easyAuthTenantId}/v2.0'
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
