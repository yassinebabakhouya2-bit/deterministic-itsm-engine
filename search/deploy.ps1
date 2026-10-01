# =====================================================================
# Deployment of the Azure AI Search index pipeline — PER CLIENT
# Architecture: DEDICATED index per client (physical isolation, M2).
# Creates/updates for ONE client: datasource -> index -> skillset -> indexer.
# Agnostic (axiom A2): the same template serves ALL clients;
#   only -ClientId changes. Adding a client = re-run this script.
# The admin key is retrieved AT RUNTIME (never stored in Git).
# Usage: ./deploy.ps1 -ClientId clienta
# =====================================================================
param(
  [Parameter(Mandatory = $true)][string]$ClientId,
  [string]$Service        = "srch-knowledgeengine3-v9",
  [string]$ResourceGroup  = "rg-knowledgeengine-v9",
  [string]$StorageAccount = "stknowledgeengine3v9",
  [string]$FunctionApp    = "fn-knowledgeengine3-v9",   # Etape 3 : Function d'enrichissement (voir enrichment/)
  [string]$SubscriptionId,
  [string]$ApiVersion     = "2026-04-01"   # >= requis pour #Microsoft.Skills.Util.DocumentIntelligenceLayoutSkill + AIServicesByIdentity
)

$ErrorActionPreference = "Stop"
$ClientId = $ClientId.ToLower()   # Azure Search resource names are lowercase
$endpoint = "https://$Service.search.windows.net"

if (-not $SubscriptionId) {
  $SubscriptionId = az account show --query id -o tsv
}
$storageResourceId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Storage/storageAccounts/$StorageAccount"

Write-Host "Retrieving the admin key (runtime, not stored)..."
$key = az search admin-key show --service-name $Service --resource-group $ResourceGroup --query primaryKey -o tsv
if (-not $key) { throw "Could not retrieve the admin key. Check az login and the service name." }

$headers = @{ "api-key" = $key; "Content-Type" = "application/json" }

# Cle de la Function d'enrichissement (etape 3) : un secret de PLAN DE DONNEES,
# jamais une ressource ARM -- donc jamais dans le Bicep, recuperee ici au
# moment du deploiement comme la cle admin ci-dessus, jamais affichee, jamais
# committee. Injectee dans les skillsets via le placeholder __FN_ENRICH_KEY__.
Write-Host "Retrieving the enrichment function key (runtime, not stored)..."
$fnKey = az functionapp keys list --resource-group $ResourceGroup --name $FunctionApp --query "functionKeys.default" -o tsv
if (-not $fnKey) { throw "Could not retrieve the enrichment function key. Check that $FunctionApp is deployed (see enrichment/README.md)." }

# Le synonym map est reference par les champs title/chunk de l'index : il doit
# exister AVANT la creation de l'index, sinon la reference echoue. Son contenu
# reel est genere automatiquement APRES l'indexation, a partir des alias
# d'entites extraits du corpus (axiome A2 : aucun vocabulaire saisi a la main,
# quel que soit le client). On ne cree donc ici qu'un contenu neutre, et
# UNIQUEMENT s'il n'existe pas deja -- sinon un simple redeploiement ecraserait
# le vocabulaire genere.
# Design note (2026-09-23, Jalon 9): one synonym map per client (syn-$ClientId)
# hit a hard Azure AI Search Basic-tier quota -- 3 synonym maps per service, period,
# not raisable without a tier migration (confirmed live: clientc's deploy failed with
# "Synonym map quota of 3 has been exceeded" after clienta/clientb/client-s alone).
# Fix: every client now references the SAME physical map ("syn-clienta", kept as the
# name to avoid migrating already-generated content) instead of one map each -- this is
# safe because synonym vocabulary ("mot de passe"/SSPR, MFA, VPN, imprimante, ...) is
# generic IT service-desk terminology, not per-client confidential content, unlike the
# index/skillset isolation (axiome A2), which stays fully per-client.
function Ensure-SynonymMap($name) {
  $uri = "$endpoint/synonymmaps/$name`?api-version=$ApiVersion"
  try {
    Invoke-RestMethod -Method Get -Uri $uri -Headers $headers | Out-Null
    Write-Host "  OK -> synonymmaps/$name (existant, contenu preserve)"
    return
  } catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
  }
  $seed = @{
    name    = $name
    format  = "solr"
    # Regle neutre : ces jetons ne peuvent apparaitre dans aucun corpus reel.
    synonyms = "__ke_placeholder_a__, __ke_placeholder_b__`n"
  } | ConvertTo-Json -Depth 3
  Invoke-RestMethod -Method Put -Uri $uri -Headers $headers -Body $seed | Out-Null
  Write-Host "  OK -> synonymmaps/$name (cree, vide en attente de generation)"
}

function Put-Resource($collection, $name, $templateFile) {
  $body = Get-Content (Join-Path $PSScriptRoot $templateFile) -Raw
  $body = $body.Replace("__CLIENTID__", $ClientId).Replace("__STORAGE_RESOURCE_ID__", $storageResourceId).Replace("__FN_ENRICH_KEY__", $fnKey)
  $uri  = "$endpoint/$collection/$name`?api-version=$ApiVersion"
  # A role granted minutes earlier (infra/modules/roles.bicep: Search -> Cognitive Services
  # User on Foundry) can take several minutes to reach the Search service, whose skillset
  # validation then answers "Unable to connect to AI Services using managed identity".
  # Retry that error only (RBAC propagation), for up to 10 minutes; anything else throws.
  for ($attempt = 1; $attempt -le 20; $attempt++) {
    try {
      Invoke-RestMethod -Method Put -Uri $uri -Headers $headers -Body $body | Out-Null
      break
    } catch {
      $detail = "$($_.ErrorDetails.Message) $($_.Exception.Message)"
      if (($detail -notmatch 'Unable to connect to AI Services using managed identity') -or ($attempt -ge 20)) { throw }
      Write-Host "  $collection/$name : Search identity not yet authorized on the AI Services account (RBAC propagation) - retry $attempt/20 in 30 s..."
      Start-Sleep -Seconds 30
    }
  }
  Write-Host "  OK -> $collection/$name"
}

Write-Host "Deploying the pipeline for client '$ClientId' on $endpoint ..."
Ensure-SynonymMap "syn-clienta"
Put-Resource "datasources" "ds-$ClientId"     "datasource.template.json"
Put-Resource "indexes"     "idx-$ClientId"    "index.template.json"
# Two skillsets / two indexers sharing the same datasource + the same target index:
#  - "-di"   : Document Intelligence Layout (PDF/DOCX/XLSX/PPTX/HTML/images) - text + tables.
#  - "-text" : native text pipeline (everything DI does not support: .md, .txt, .csv, .json, ...).
# The indexer-level indexedFileNameExtensions / excludedFileNameExtensions filters make the
# split mutually exclusive, so every blob is processed by exactly one of the two pipelines.
Put-Resource "skillsets"   "ss-$ClientId-di"   "skillset-di.template.json"
Put-Resource "skillsets"   "ss-$ClientId-text" "skillset.template.json"
Put-Resource "indexers"    "ix-$ClientId-di"   "indexer-di.template.json"
Put-Resource "indexers"    "ix-$ClientId-text" "indexer.template.json"

Write-Host ""
Write-Host "Pipeline '$ClientId' deployed. Indexers 'ix-$ClientId-di' and 'ix-$ClientId-text' start"
Write-Host "automatically and index the blobs from kb-$ClientId into the dedicated index idx-$ClientId"
Write-Host "(split by file type: DI for PDF/Office/images, native text for the rest)."
Write-Host "Isolation: each client has ITS OWN index. clientId is also projected (defense in depth)."
