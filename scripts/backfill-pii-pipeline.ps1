<#
.SYNOPSIS
  Retire le flag "transcribed=true" sur TOUS les fichiers audio deja
  transcrits par l'ANCIEN pipeline (avant les 3 correctifs PII/resume de
  cette session), pour forcer logic-transcribe-<client> a les retraiter
  avec le nouveau pipeline (redaction PII + resume Probleme/Resolution)
  au prochain declenchement.

  Cible : tout blob dont le nom contient "n°" ou "N°" (empreinte du lot
  de fichiers uploades depuis 2025-08-05, traites par l'ancien pipeline
  bugge) ET qui a actuellement transcribed=true.

.PARAMETER ClientCode
  Ex: client-s

.PARAMETER Exclude
  Noms de blobs a NE PAS reinitialiser (deja retraites manuellement,
  ex: n°7 fait plus tot dans la session).

.PARAMETER WhatIf
  Simulation seulement (recommande de lancer ca en premier).
#>

param(
    [Parameter(Mandatory = $true)][string]$ClientCode,
    [string[]]$Exclude = @(),
    [switch]$WhatIf
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 > $null

$ErrorActionPreference = "Stop"

$storageAccount = "stknowledgeengine3v9"
$audioContainer = "audio-raw-$ClientCode"
$apiVersion     = "2021-08-06"

Write-Host "=== Backfill pipeline PII/resume pour $ClientCode ===" -ForegroundColor Cyan
Write-Host "Container : $audioContainer"
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

function Invoke-AzureStorageXml {
    param([string]$Uri, [hashtable]$Headers)
    $webResp = Invoke-WebRequest -Uri $Uri -Headers $Headers -Method Get -UseBasicParsing
    $bytes = $webResp.RawContentStream.ToArray()
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $bytes = $bytes[3..($bytes.Length - 1)]
    }
    $xmlText = [System.Text.Encoding]::UTF8.GetString($bytes)
    [xml]$xmlDoc = $xmlText
    return $xmlDoc
}

function Get-AllBlobsViaRest {
    param([string]$Container, [switch]$IncludeMetadata)
    $results = @()
    $marker = $null
    do {
        $uri = "https://$storageAccount.blob.core.windows.net/$Container" + "?restype=container&comp=list"
        if ($IncludeMetadata) { $uri += "&include=metadata" }
        if ($marker) { $uri += "&marker=$([System.Uri]::EscapeDataString($marker))" }
        $resp = Invoke-AzureStorageXml -Uri $uri -Headers $headers
        if ($resp.EnumerationResults.Blobs.Blob) {
            foreach ($blob in $resp.EnumerationResults.Blobs.Blob) {
                $meta = $null
                if ($IncludeMetadata -and $blob.Metadata) {
                    $meta = @{}
                    foreach ($propName in $blob.Metadata.PSObject.Properties.Name) {
                        if ($propName -notlike '#*') { $meta[$propName] = $blob.Metadata.$propName }
                    }
                }
                $results += [PSCustomObject]@{ Name = [string]$blob.Name; Metadata = $meta }
            }
        }
        $marker = $resp.EnumerationResults.NextMarker
        if ([string]::IsNullOrWhiteSpace($marker)) { $marker = $null }
    } while ($marker)
    return $results
}

function Clear-BlobTranscribedViaRest {
    param([string]$Container, [string]$BlobName, [string]$ClientCode)
    $encodedPath = ($BlobName -split '/' | ForEach-Object { [System.Uri]::EscapeDataString($_) }) -join '/'
    $uri = "https://$storageAccount.blob.core.windows.net/$Container/$encodedPath" + "?comp=metadata"
    $reqHeaders = @{}
    foreach ($k in $headers.Keys) { $reqHeaders[$k] = $headers[$k] }
    $reqHeaders["x-ms-meta-clientid"] = $ClientCode
    Invoke-RestMethod -Uri $uri -Headers $reqHeaders -Method Put | Out-Null
}

Write-Host "Recuperation des blobs audio (avec metadata) via REST..."
$allBlobs = Get-AllBlobsViaRest -Container $audioContainer -IncludeMetadata
Write-Host "  -> $($allBlobs.Count) blob(s) au total"
Write-Host ""

$targets = $allBlobs | Where-Object {
    $_.Metadata -and $_.Metadata.ContainsKey("transcribed") -and $_.Metadata["transcribed"] -eq "true" -and
    ($Exclude -notcontains $_.Name)
}

Write-Host "$($targets.Count) fichier(s) cible(s) pour le backfill (deja transcrits par l'ancien pipeline, hors exclusions) :" -ForegroundColor Cyan
$i = 0
foreach ($t in $targets) {
    $i++
    Write-Host "[$i/$($targets.Count)] $($t.Name)"
}
Write-Host ""

if ($WhatIf) {
    Write-Host "[WhatIf] Ces $($targets.Count) fichier(s) perdraient leur flag transcribed (rien n'est modifie)." -ForegroundColor Yellow
    exit 0
}

$done = 0
$failed = 0
$i = 0
foreach ($t in $targets) {
    $i++
    Write-Host "[$i/$($targets.Count)] $($t.Name)" -NoNewline
    try {
        Clear-BlobTranscribedViaRest -Container $audioContainer -BlobName $t.Name -ClientCode $ClientCode
        Write-Host "  -> flag retire" -ForegroundColor Green
        $done++
    }
    catch {
        Write-Host "  -> ECHEC : $($_.Exception.Message)" -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
Write-Host "=== Resume ===" -ForegroundColor Cyan
Write-Host "Flags retires : $done"
Write-Host "Echecs        : $failed"
Write-Host ""
Write-Host "Ces fichiers seront retraites (redaction PII + resume) au prochain declenchement de logic-transcribe-$ClientCode." -ForegroundColor Cyan
