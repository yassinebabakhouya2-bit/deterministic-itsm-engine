<#
.SYNOPSIS
  Restaure le flag "transcribed=true" sur les blobs audio dont le .txt
  correspondant existe deja dans le container KB (sans re-transcrire).

  v4 : utilise directement l'API REST Azure Storage via Invoke-RestMethod
  au lieu de parser la sortie texte de "az" (dont on a prouve qu'elle
  corrompt irreversiblement les caracteres non-ASCII comme "°" en U+FFFD,
  meme avec tous les reglages d'encodage PowerShell/Python actives -
  le bug se situe dans la capture native process d'az CLI sur Windows,
  hors de notre controle). az sert uniquement a recuperer un token OAuth
  (texte ASCII pur, donc sans risque), tout le reste (listage, mise a
  jour des metadonnees) passe par des appels REST .NET natifs, qui
  decodent l'UTF-8 correctement.

.PARAMETER ClientCode
  Ex: client-s

.PARAMETER WhatIf
  Mode simulation : n'effectue aucune modification, affiche seulement
  ce qui serait fait.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$ClientCode,

    [switch]$WhatIf
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 > $null

$ErrorActionPreference = "Stop"

$storageAccount = "stknowledgeengine3v9"
$audioContainer = "audio-raw-$ClientCode"
$kbContainer    = "kb-$ClientCode"
$apiVersion     = "2021-08-06"

Write-Host "=== Reparation flag transcribed pour $ClientCode (via API REST Azure Storage) ===" -ForegroundColor Cyan
Write-Host "Container audio : $audioContainer"
Write-Host "Container KB    : $kbContainer"
if ($WhatIf) { Write-Host "(Mode -WhatIf : aucune modification ne sera appliquee)" -ForegroundColor Yellow }
Write-Host ""

Write-Host "Recuperation du token d'acces (via az, texte ASCII pur donc sans risque)..."
$token = az account get-access-token --resource "https://storage.azure.com" --query accessToken -o tsv
if ([string]::IsNullOrWhiteSpace($token)) {
    throw "Impossible d'obtenir un token d'acces. Verifie que tu es connecte (az login)."
}
$headers = @{
    "Authorization" = "Bearer $token"
    "x-ms-version"  = $apiVersion
}
Write-Host "  -> Token obtenu"
Write-Host ""

function Invoke-AzureStorageXml {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][hashtable]$Headers
    )

    # Invoke-RestMethod (et le cast [xml] direct) mal-decodent le corps XML
    # renvoye par Azure Storage sur ce poste (bug connu de detection
    # d'encodage de Windows PowerShell 5.1 / WebClient) : le BOM UTF-8
    # devient "ï»¿" et chaque caractere non-ASCII comme "°" devient "Â°"
    # (double-encodage classique UTF-8-lu-en-Latin-1). On contourne en
    # recuperant les OCTETS BRUTS de la reponse et en forcant nous-memes
    # le decodage UTF-8, BOM retire manuellement.
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
    param(
        [Parameter(Mandatory = $true)][string]$Container,
        [switch]$IncludeMetadata
    )

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
                        if ($propName -notlike '#*') {
                            $meta[$propName] = $blob.Metadata.$propName
                        }
                    }
                }
                $results += [PSCustomObject]@{
                    Name     = [string]$blob.Name
                    Metadata = $meta
                }
            }
        }

        $marker = $resp.EnumerationResults.NextMarker
        if ([string]::IsNullOrWhiteSpace($marker)) { $marker = $null }
    } while ($marker)

    return $results
}

function Set-BlobMetadataViaRest {
    param(
        [Parameter(Mandatory = $true)][string]$Container,
        [Parameter(Mandatory = $true)][string]$BlobName,
        [Parameter(Mandatory = $true)][hashtable]$Metadata
    )

    $encodedPath = ($BlobName -split '/' | ForEach-Object { [System.Uri]::EscapeDataString($_) }) -join '/'
    $uri = "https://$storageAccount.blob.core.windows.net/$Container/$encodedPath" + "?comp=metadata"

    $reqHeaders = @{}
    foreach ($k in $headers.Keys) { $reqHeaders[$k] = $headers[$k] }
    foreach ($key in $Metadata.Keys) {
        $reqHeaders["x-ms-meta-$key"] = $Metadata[$key]
    }

    Invoke-RestMethod -Uri $uri -Headers $reqHeaders -Method Put | Out-Null
}

# --- 1. Lister les blobs audio avec metadata ------------------------------
Write-Host "Recuperation des blobs audio (avec metadata) via REST..."
$allAudioBlobs = Get-AllBlobsViaRest -Container $audioContainer -IncludeMetadata
$audioBlobs = $allAudioBlobs | Where-Object {
    -not ($_.Metadata -and $_.Metadata.ContainsKey("transcribed") -and $_.Metadata["transcribed"] -eq "true")
}
Write-Host "  -> $($allAudioBlobs.Count) blob(s) audio au total, $($audioBlobs.Count) sans flag transcribed=true"
Write-Host ""

# --- 2. Lister tous les blobs du container KB (.txt) ----------------------
Write-Host "Recuperation de la liste des fichiers KB (.txt) via REST..."
$kbBlobs = Get-AllBlobsViaRest -Container $kbContainer
$kbSet = [System.Collections.Generic.HashSet[string]]::new([string[]]($kbBlobs | ForEach-Object { $_.Name }), [System.StringComparer]::OrdinalIgnoreCase)
Write-Host "  -> $($kbSet.Count) fichier(s) dans $kbContainer"
Write-Host ""

# --- 3. Pour chaque blob audio, verifier si le .txt existe ----------------
$restored     = 0
$failed       = 0
$stillMissing = 0
$failedList   = @()
$missingList  = @()

$i = 0
foreach ($blob in $audioBlobs) {
    $i++
    $audioName = $blob.Name
    $txtName = [System.IO.Path]::ChangeExtension($audioName, ".txt")

    Write-Host "[$i/$($audioBlobs.Count)] $audioName" -NoNewline

    if (-not $kbSet.Contains($txtName)) {
        Write-Host "  -> pas de .txt correspondant, laisse tel quel (sera retranscrit)" -ForegroundColor DarkGray
        $stillMissing++
        $missingList += $audioName
        continue
    }

    if ($WhatIf) {
        Write-Host "  -> [WhatIf] serait restaure (txt trouve: $txtName)" -ForegroundColor Yellow
        $restored++
        continue
    }

    try {
        Set-BlobMetadataViaRest -Container $audioContainer -BlobName $audioName -Metadata @{
            clientid    = $ClientCode
            transcribed = "true"
        }
        Write-Host "  -> restaure (txt trouve: $txtName)" -ForegroundColor Green
        $restored++
    }
    catch {
        Write-Host "  -> ECHEC : $($_.Exception.Message)" -ForegroundColor Red
        $failed++
        $failedList += $audioName
    }
}

Write-Host ""
Write-Host "=== Resume ===" -ForegroundColor Cyan
Write-Host "Restaure       : $restored"
Write-Host "Echec          : $failed"
Write-Host "Toujours manquant (pas de .txt) : $stillMissing"

if ($failedList.Count -gt 0) {
    Write-Host ""
    Write-Host "Fichiers en echec :" -ForegroundColor Red
    $failedList | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
}

if ($missingList.Count -gt 0) {
    Write-Host ""
    Write-Host "Fichiers sans .txt (a retranscrire) :" -ForegroundColor DarkGray
    $missingList | ForEach-Object { Write-Host "  - $_" -ForegroundColor DarkGray }
}
