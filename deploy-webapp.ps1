# deploy-webapp.ps1
# Design note (2026-09-24, Jalon 9): reconstruit un script reutilisable a partir
# de la structure du dernier zip deploye avec succes (deploy-2026-09-22c.zip,
# inspecte pour cette raison) -- ce script n'existait pas encore, chaque deploy
# precedent etait fait a la main (point signale comme lacune dans workflow.md).
# Methode "entry-by-entry" (ZipFileExtensions::CreateEntryFromFile, chemins
# forces en '/'), pas Compress-Archive -- celui-ci ecrit des chemins '\' dans
# le zip, illisibles par Azure App Service (Linux). Inclut exactement les
# dossiers necessaires a l'execution (voir sys.path.insert dans app/app.py) :
# app/, orchestration/, clients-local/ (donnees reelles, gitignored mais
# necessaires au runtime), config/, requirements.txt racine, README.md.

param(
    [string]$ResourceGroup = "rg-knowledgeengine-v9",
    [string]$WebAppName = "app-knowledgeengine2-v9",
    [string]$ZipPath = "deploy-$(Get-Date -Format 'yyyy-MM-dd-HHmmss').zip"
)

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

if (Test-Path $ZipPath) { Remove-Item $ZipPath }

$root = (Get-Location).Path
$includes = @("app", "orchestration", "clients-local", "config", "requirements.txt", "README.md")

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
