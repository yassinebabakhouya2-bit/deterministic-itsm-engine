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
#
# 2026-10-09 (runbook 19.17-19.19): refuses to package anything but a clean commit of origin/main
# (scripts/deploy-guard.ps1; -AllowNotMain to ship another commit on purpose), writes that commit into
# the package (BUILD_COMMIT), and throws when packaging or az webapp deploy fails -- a pasted block stops
# there. -FromOnboarding (scripts/bootstrap-new-tenant.ps1, scripts/attach-external-tenant.ps1): those
# scripts rewrite config/engine.<client>.yaml and config/itsm.yaml just before calling this one, so those
# local changes are shipped (and recorded as "+local-changes"), any commit is accepted, and a failed
# az webapp deploy is reported through the exit code, as they expect, instead of a throw.

param(
    [string]$ResourceGroup = "rg-knowledgeengine-v9",
    [string]$WebAppName = "app-knowledgeengine3-v9",
    [string]$ZipPath = "deploy-$(Get-Date -Format 'yyyy-MM-dd-HHmmss').zip",
    # Real clients whose clients-local/engine.<client>.yaml is shipped with the app.
    [string[]]$ClientsLocal = @('client-s'),
    # Ship no clients-local/ config at all (demo-only deployment).
    [switch]$SkipClientsLocal,
    # Ship a commit other than origin/main on purpose (a branch being tested).
    [switch]$AllowNotMain,
    # Called by an onboarding script that has just rewritten the client configs (see the header).
    [switch]$FromOnboarding
)

# Any error stops the script (a half-written package is never deployed); native commands are checked
# through $LASTEXITCODE.
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$root = (Get-Location).Path
. (Join-Path $PSScriptRoot 'scripts/deploy-guard.ps1')
# clients-local/ is git-ignored: its engine.<client>.yaml never counts as a change
$onboardingChanges = if ($FromOnboarding) { @('config/engine.*.yaml', 'config/itsm.yaml') } else { @() }
$tree = Assert-DeployableTree -Root $root -Paths @('app', 'orchestration', 'config', 'requirements.txt', 'README.md') `
    -AllowedChanges $onboardingChanges -AllowNotMain:($AllowNotMain -or $FromOnboarding)
$stamp = Get-BuildStamp $tree
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
    Add-TextEntry $zip 'BUILD_COMMIT' $stamp
} finally {
    $zip.Dispose()
}

Write-Host "Zip cree : $ZipPath"
Write-Host "Deploiement vers $WebAppName..."
az webapp deploy --resource-group $ResourceGroup --name $WebAppName --src-path $ZipPath --type zip
if ($LASTEXITCODE -ne 0) {
    # Its own status poll can fail on a real success (runbook 7): the site may run the new package, the old
    # one, or neither. Say so, and stop the block (or hand the exit code to an onboarding script).
    $message = "az webapp deploy reported a failure for $WebAppName ($($tree.Commit.Substring(0, 7))): it can be a real failure " +
               "or a failed status poll on a real success (runbook 7) -- open the site and check its deployment log before re-running"
    if ($FromOnboarding) { Write-Warning $message; exit 1 }
    throw $message
}
Write-Host "Deployed $($tree.Commit.Substring(0, 7)) to $WebAppName ($stamp)."
