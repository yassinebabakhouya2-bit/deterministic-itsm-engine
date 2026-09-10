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
  [string]$ApiVersion     = "2024-07-01"
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
Put-Resource "datasources" "ds-$ClientId"  "datasource.template.json"
Put-Resource "indexes"     "idx-$ClientId" "index.template.json"
Put-Resource "skillsets"   "ss-$ClientId"  "skillset.template.json"
Put-Resource "indexers"    "ix-$ClientId"  "indexer.template.json"

Write-Host ""
Write-Host "Pipeline '$ClientId' deployed. Indexer 'ix-$ClientId' starts automatically"
Write-Host "and indexes the blobs from kb-$ClientId into the dedicated index idx-$ClientId."
Write-Host "Isolation: each client has ITS OWN index. clientId is also projected (defense in depth)."
