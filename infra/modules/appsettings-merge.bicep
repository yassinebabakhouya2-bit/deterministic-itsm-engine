// =====================================================================
// Module: merge settings into a Web App's existing app settings
//
// A template cannot read (list()) the app settings of the resource it writes: ARM rejects it as a
// circular dependency on sites/config/appsettings (first kecore-link deployment, 2026-10-08,
// runbook 19.8). The caller reads the current settings and passes them here; this module writes
// them back with the new ones on top. Nothing else of the Web App is touched.
//
// The current settings hold secrets (the Easy Auth client secret, Key Vault references): the
// parameter is secure, so it is never shown in the deployment history.
// =====================================================================

@description('EXISTING Web App')
param webAppName string

@description('The Web App\'s current app settings, as list() returns them (properties)')
@secure()
param currentAppSettings object

@description('Settings to add, or to replace when they exist')
param appSettings object

resource webApp 'Microsoft.Web/sites@2023-12-01' existing = {
  name: webAppName
}

resource settings 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: webApp
  name: 'appsettings'
  properties: union(currentAppSettings, appSettings)
}
