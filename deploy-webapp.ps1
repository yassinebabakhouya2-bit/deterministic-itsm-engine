# deploy-webapp.ps1
# Design note (2026-09-24, Jalon 9): reconstruit un script reutilisable a partir
# de la structure du dernier zip deploye avec succes (deploy-2026-09-22c.zip,
# inspecte pour cette raison) -- ce script n'existait pas encore, chaque deploy
# precedent etait fait a la main (point signale comme lacune dans workflow.md).
# Methode "entry-by-entry" (ZipFileExtensions::CreateEntryFromFile, chemins
# forces en '/'), pas Compress-Archive -- celui-ci ecrit des chemins '\' dans
# le zip, illisibles par Azure App Service (Linux). Inclut exactement les
# dossiers necessaires a l'execution (voir sys.path.insert dans app/app.py) :
# app/, orchestration/, config/, requirements.txt racine, README.md.
#
# 2026-09-30: clients-local/ (git-ignored, real clients) is no longer shipped
# whole. Only clients-local/engine.<client>.yaml of the clients in -ClientsLocal
# goes into the package: it is the only file of that folder the app reads
# (orchestration/answer.py CONFIG_DIRS, app/auth.py). Everything else there
# (demo credentials, eval data, parameters files, abandoned clients) stays on
# this machine.

param(
    [string]$ResourceGroup = "rg-knowledgeengine-v9",
    [string]$WebAppName = "app-knowledgeengine3-v9",
    [string]$ZipPath = "deploy-$(Get-Date -Format 'yyyy-MM-dd-HHmmss').zip",
    # Real clients whose clients-local/engine.<client>.yaml is shipped with the app.
    [string[]]$ClientsLocal = @('client-s'),
    # Ship no clients-local/ config at all (demo-only deployment).
    [switch]$SkipClientsLocal
)

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$root = (Get-Location).Path
# Absolute zip path: .NET (ZipFile::Open) resolves a relative path against the process
# directory, which cd / Push-Location do not change, while az resolves it against this
# location - the zip was written in one folder and looked for in the other.
if (-not [System.IO.Path]::IsPathRooted($ZipPath)) { $ZipPath = Join-Path $root $ZipPath }
if (Test-Path $ZipPath) { Remove-Item $ZipPath }

$includes = @("app", "orchestration", "config", "requirements.txt", "README.md")
if (-not $SkipClientsLocal) {
    foreach ($c in $ClientsLocal) {
        if (-not $c) { continue }
        $f = "clients-local/engine.$c.yaml"
        if (Test-Path (Join-Path $root $f)) {
            $includes += $f
            Write-Host "Config client : $f"
        } else {
            Write-Warning "Absent, ignore : $f"
        }
    }
}

$zip = [System.IO.Compression.ZipFile]::Open($ZipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($inc in $includes) {
        $incPath = Join-Path $root $inc
        if (-not (Test-Path $incPath)) { Write-Warning "Absent, ignore : $inc"; continue }
        if ((Get-Item $incPath).PSIsContainer) {
            Get-ChildItem -Path $incPath -Recurse -File | Where-Object {
                $_.FullName -notmatch '__pycache__' -and $_.Extension -ne '.pyc'
            } | ForEach-Object {
                $relPath = $_.FullName.Substring($root.Length + 1) -replace '\\', '/'
                [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $relPath) | Out-Null
            }
        } else {
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $incPath, $inc) | Out-Null
        }
    }
} finally {
    $zip.Dispose()
}

Write-Host "Zip cree : $ZipPath"
Write-Host "Deploiement vers $WebAppName..."
az webapp deploy --resource-group $ResourceGroup --name $WebAppName --src-path $ZipPath --type zip
