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
  [string]$Service        = "srch-knowledgeengine2-v9",
  [string]$ResourceGroup  = "rg-knowledgeengine-v9",
  [string]$StorageAccount = "stknowledgeengine2v9",
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

function Put-Resource($collection, $name, $templateFile) {
  $body = Get-Content (Join-Path $PSScriptRoot $templateFile) -Raw
  $body = $body.Replace("__CLIENTID__", $ClientId).Replace("__STORAGE_RESOURCE_ID__", $storageResourceId)
  $uri  = "$endpoint/$collection/$name`?api-version=$ApiVersion"
  Invoke-RestMethod -Method Put -Uri $uri -Headers $headers -Body $body | Out-Null
  Write-Host "  OK -> $collection/$name"
}

Write-Host "Deploying the pipeline for client '$ClientId' on $endpoint ..."
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
