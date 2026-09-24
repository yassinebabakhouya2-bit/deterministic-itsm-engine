// =====================================================================
// Module: fn-kb-enrich — Function d'enrichissement semantique
//
// Appelee par Azure AI Search (WebApiSkill) pendant l'indexation, sur
// deux routes : /api/enrich (fiches du referentiel) et /api/enrich-call
// (transcriptions d'appels et de videos). Voir enrichment/function_app.py.
//
// ---------------------------------------------------------------------
// HISTORIQUE DE CE CHOIX D'HEBERGEMENT -- a lire avant de le changer.
//
// Premiere tentative : plan Flex Consumption dedie. Sur le papier c'est
// le bon modele pour cette charge (rafale pendant un run d'indexation,
// rien le reste du temps, retombee a zero). En pratique l'hote n'a jamais
// demarre : `Running` cote ARM, InternalServerError du runtime sur toutes
// les routes, aucune trace ni dans Application Insights ni dans
// FunctionAppLogs. Une heure perdue sans diagnostic exploitable.
//
// Choix retenu : le plan App Service B1 DEJA EN PLACE, celui qui fait
// tourner la Web App depuis des semaines. Modele d'hebergement prouve
// dans cette souscription, pas de conteneur de deploiement, pas de
// controleur de mise a l'echelle, logs classiques accessibles.
//
// Ce qu'on paie : pendant une reindexation, l'enrichissement et
// l'interface partagent le CPU du B1. C'est sans effet tant que les
// reindexations se font hors demonstration -- et le vrai frein reste de
// toute facon le quota TPM du deploiement gpt-4o-enrich, pas le CPU.
// Revenir a Flex est une option a rouvrir quand il n'y a pas d'echeance.
// ---------------------------------------------------------------------
//
// Zero secret (axiome A5) : identite managee systeme, connexion au
// stockage par identite (AzureWebJobsStorage__accountName), aucune chaine
// de connexion. Sur un plan dedie, WEBSITE_CONTENTAZUREFILECONNECTIONSTRING
// n'est pas requis -- c'est ce qui permet de rester sans secret.
//
// Reste hors Bicep, volontairement : la CLE DE FONCTION que AI Search
// place dans l'URI du WebApiSkill. Une cle de fonction est un secret de
// plan de donnees, pas une ressource ARM. search/deploy.ps1 la lit au
// moment du deploiement (az functionapp keys list) et l'injecte dans le
// skillset sans jamais l'afficher.
// =====================================================================

@description('Nom de la Function App')
param functionAppName string

@description('Nom du plan App Service EXISTANT qui heberge deja la Web App')
param hostingPlanName string

@description('Region Azure')
param location string

@description('Nom du compte de stockage existant (host storage et table de cache)')
param storageAccountName string

@description('Nom de la ressource Foundry existante (endpoint AOAI + deploiement de generation)')
param foundryName string

@description('Nom du service AI Search existant (lecture de la facette entities pour le vocabulaire client)')
param searchServiceName string

@description('Nom du deploiement de modele utilise pour l\'extraction. Epingle : changer de modele change les sorties, donc le cache.')
param enrichDeployment string = 'gpt-4o-enrich'

@description('Nom de la table de cache d\'enrichissement (empreinte contenu+prompt -> sortie)')
param cacheTableName string = 'enrichCache'

@description('Creer Log Analytics + Application Insights pour cette Function. Sans telemetrie, un echec de demarrage de l\'hote est muet -- on l\'a appris a nos depens. Laisser a true.')
param enableTelemetry bool = true

@description('Retention des logs en jours')
param logRetentionDays int = 30

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: foundryName
}

resource search 'Microsoft.Search/searchServices@2024-06-01-preview' existing = {
  name: searchServiceName
}

resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' existing = {
  name: hostingPlanName
}

// Table de cache : declaree ici plutot que creee a la volee par le code,
// pour qu'un environnement neuf soit complet apres un seul deploiement.
resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource cacheTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: cacheTableName
}

// ---------------------------------------------------------------------
// Telemetrie. Application Insights recoit ce que l'application emet ;
// le parametre de diagnostic plus bas capte les logs de la PLATEFORME,
// qui eux parlent meme quand l'hote meurt avant d'initialiser sa propre
// telemetrie. Les deux, parce que l'absence du second nous a coute cher.
// ---------------------------------------------------------------------
resource logWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (enableTelemetry) {
  name: 'log-${functionAppName}'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionDays
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = if (enableTelemetry) {
  name: 'appi-${functionAppName}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logWorkspace.id
  }
}

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
      // alwaysOn : la Function est appelee par l'indexeur en rafale apres
      // de longues periodes d'inactivite. Sans ce reglage, chaque run
      // commencerait par un demarrage a froid sur les premiers documents.
      alwaysOn: true
      appSettings: concat(
        [
          {
            name: 'FUNCTIONS_EXTENSION_VERSION'
            value: '~4'
          }
          {
            name: 'FUNCTIONS_WORKER_RUNTIME'
            value: 'python'
          }
          {
            // Compilation des dependances cote serveur au deploiement zip
            name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
            value: 'true'
          }
          {
            // Connexion au stockage par identite : pas de chaine de connexion
            name: 'AzureWebJobsStorage__accountName'
            value: storage.name
          }
          {
            name: 'AOAI_ENDPOINT'
            value: foundry.properties.endpoint
          }
          {
            name: 'AOAI_DEPLOYMENT'
            value: enrichDeployment
          }
          {
            name: 'SEARCH_ENDPOINT'
            value: 'https://${search.name}.search.windows.net'
          }
          {
            name: 'CACHE_TABLE_ENDPOINT'
            value: storage.properties.primaryEndpoints.table
          }
          {
            name: 'CACHE_TABLE'
            value: cacheTableName
          }
        ],
        !enableTelemetry ? [] : [
          {
            name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
            value: appInsights.properties.ConnectionString
          }
        ]
      )
    }
  }
  dependsOn: [
    cacheTable
  ]
}

// Logs de la PLATEFORME, distincts de la telemetrie applicative.
resource functionDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (enableTelemetry) {
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
// RBAC de l'identite de la Function. Cinq droits, aucun de plus :
//   - Blob Data Owner        : stockage d'hote
//   - Queue Data Contributor : file interne de l'hote Functions
//   - Table Data Contributor : la table de cache
//   - Cognitive Services OpenAI User : l'appel d'extraction
//   - Search Index Data Reader : LECTURE SEULE de la facette entities,
//     pour connaitre le vocabulaire deja etabli du client. La Function
//     n'ecrit jamais dans l'index -- c'est l'indexeur qui ecrit, avec sa
//     propre identite.
// ---------------------------------------------------------------------
var storageBlobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageQueueDataContributor = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableDataContributor = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
var cognitiveServicesOpenAIUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var searchIndexDataReader = '1407120a-92aa-4202-b7e9-c0e197c71c8f'

resource fnToBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, storageBlobDataOwner)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwner)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource fnToQueue 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, storageQueueDataContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageQueueDataContributor)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource fnToTable 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, storageTableDataContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageTableDataContributor)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource fnToFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, functionApp.id, cognitiveServicesOpenAIUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUser)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource fnToSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, functionApp.id, searchIndexDataReader)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataReader)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output functionAppName string = functionApp.name
output functionAppHostName string = functionApp.properties.defaultHostName
output functionPrincipalId string = functionApp.identity.principalId
