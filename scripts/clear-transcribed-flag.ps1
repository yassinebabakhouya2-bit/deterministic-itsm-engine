<#
.SYNOPSIS
  Retire le flag "transcribed=true" sur UN blob audio precis, pour forcer
  logic-transcribe-<client> a le retraiter (avec le nouveau pipeline PII +
  resume) au prochain declenchement. Reutilise le meme mecanisme REST
  fiable (contournement du bug d'encodage az CLI) que repair-transcribed-flag.ps1.

.PARAMETER ClientCode
  Ex: client-s

.PARAMETER BlobName
  Nom EXACT du blob (chemin complet dans le container audio-raw-<client>),
  copie-colle depuis le portail Azure ou une sortie du script de reparation.

.PARAMETER WhatIf
  Simulation seulement.
#>

param(
    [Parameter(Mandatory = $true)][string]$ClientCode,
    [Parameter(Mandatory = $true)][string]$BlobName,
    [switch]$WhatIf
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 > $null

$ErrorActionPreference = "Stop"

$storageAccount = "stknowledgeengine3v9"
$audioContainer = "audio-raw-$ClientCode"
$apiVersion     = "2021-08-06"

Write-Host "=== Retrait du flag transcribed sur 1 fichier ($ClientCode) ===" -ForegroundColor Cyan
Write-Host "Container : $audioContainer"
Write-Host "Blob      : $BlobName"
if ($WhatIf) { Write-Host "(Mode -WhatIf : aucune modification ne sera appliquee)" -ForegroundColor Yellow }
Write-Host ""

$token = az account get-access-token --resource "https://storage.azure.com" --query accessToken -o tsv
if ([string]::IsNullOrWhiteSpace($token)) {
    throw "Impossible d'obtenir un token d'acces. Verifie que tu es connecte (az login)."
}
$headers = @{
    "Authorization" = "Bearer $token"
    "x-ms-version"  = $apiVersion
}

if ($WhatIf) {
    Write-Host "[WhatIf] Le blob '$BlobName' perdrait son flag transcribed (metadata remise a clientid=$ClientCode uniquement)." -ForegroundColor Yellow
    exit 0
}

$encodedPath = ($BlobName -split '/' | ForEach-Object { [System.Uri]::EscapeDataString($_) }) -join '/'
$uri = "https://$storageAccount.blob.core.windows.net/$audioContainer/$encodedPath" + "?comp=metadata"

$reqHeaders = @{}
foreach ($k in $headers.Keys) { $reqHeaders[$k] = $headers[$k] }
# IMPORTANT : Set Blob Metadata REMPLACE toutes les metadonnees. On ne
# remet QUE clientid (pas de transcribed) -> le fichier redevient
# "non-transcrit" pour le filtre du Logic App.
$reqHeaders["x-ms-meta-clientid"] = $ClientCode

try {
    Invoke-RestMethod -Uri $uri -Headers $reqHeaders -Method Put | Out-Null
    Write-Host "OK : flag transcribed retire. Ce fichier sera retraite au prochain declenchement de logic-transcribe-$ClientCode." -ForegroundColor Green
}
catch {
    Write-Host "ECHEC : $($_.Exception.Message)" -ForegroundColor Red
    throw
}
