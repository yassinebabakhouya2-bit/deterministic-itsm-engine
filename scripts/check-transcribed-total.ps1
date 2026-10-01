<#
.SYNOPSIS
  Verification (lecture seule) : liste TOUS les blobs audio actuellement
  transcribed=true, sans filtre de nom, pour comparer avec le sous-ensemble
  matchant le motif "N°" utilise par backfill-pii-pipeline.ps1.
#>

param(
    [Parameter(Mandatory = $true)][string]$ClientCode,
    [string[]]$Exclude = @()
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 > $null

$ErrorActionPreference = "Stop"

$storageAccount = "stknowledgeengine3v9"
$audioContainer = "audio-raw-$ClientCode"
$apiVersion     = "2021-08-06"

$token = az account get-access-token --resource "https://storage.azure.com" --query accessToken -o tsv
if ([string]::IsNullOrWhiteSpace($token)) { throw "Impossible d'obtenir un token d'acces." }
$headers = @{ "Authorization" = "Bearer $token"; "x-ms-version" = $apiVersion }

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

Write-Host "Recuperation des blobs audio (avec metadata) via REST..."
$allBlobs = Get-AllBlobsViaRest -Container $audioContainer -IncludeMetadata
Write-Host "  -> $($allBlobs.Count) blob(s) au total"
Write-Host ""

$allTranscribed = $allBlobs | Where-Object {
    $_.Metadata -and $_.Metadata.ContainsKey("transcribed") -and $_.Metadata["transcribed"] -eq "true" -and
    ($Exclude -notcontains $_.Name)
}

$matchingN = $allTranscribed | Where-Object { $_.Name -match '[Nn]\u00B0' }
$notMatchingN = $allTranscribed | Where-Object { $_.Name -notmatch '[Nn]\u00B0' }

Write-Host "Total transcribed=true (hors exclusions) : $($allTranscribed.Count)" -ForegroundColor Cyan
Write-Host "  dont matchant [Nn]\u00B0                     : $($matchingN.Count)"
Write-Host "  dont NE matchant PAS [Nn]\u00B0               : $($notMatchingN.Count)" -ForegroundColor Yellow
Write-Host ""

if ($notMatchingN.Count -gt 0) {
    Write-Host "Fichiers transcribed=true mais SANS 'N°' dans le nom (non captures par le backfill) :" -ForegroundColor Yellow
    foreach ($f in $notMatchingN) { Write-Host "  - $($f.Name)" }
}
