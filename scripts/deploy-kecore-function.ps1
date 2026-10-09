# deploy-kecore-function.ps1 -- V10 slice 2
# Deploys the kecore Function code (kecore_func/ plus the kecore, kefind and scoreboard packages;
# scoreboard since slice 4, for the scoreboard runs on Azure) to the
# Function App created by infra/modules/kecore.bicep (slice 1).
#
# Same packaging method as deploy-webapp.ps1 and bootstrap-new-tenant.ps1: zip built
# entry by entry with '/' separators (Compress-Archive writes '\', unreadable on Linux),
# remote build (requirements.txt installed by Oryx), retried on the transient SCM errors
# a freshly created Function App gives (runbook 12.4). The zip is written to the temp
# folder and deleted afterwards: nothing is left in the repository.
#
# 2026-10-09 (runbook 19.17-19.18 #2): refuses to package anything but a clean commit of origin/main
# (scripts/deploy-guard.ps1, -AllowNotMain to ship another commit on purpose), writes that commit into
# the package (BUILD_COMMIT), then waits until the host answers GET /api/kecore/version with THAT commit
# and serves every function declared in kecore_func/function_app.py -- the previous host keeps answering
# for a while after a 202, and its list once passed for the new one. Every failure throws: a pasted
# block stops there instead of starting a run on unknown code.

param(
    [string]$ResourceGroup = 'rg-knowledgeengine-v9',
    [string]$FunctionApp = 'fn-kecore-knowledgeengine3-v9',
    [switch]$AllowNotMain
)

# Any error stops the script (a half-written package is never deployed); native commands are checked
# through $LASTEXITCODE, and the polling calls that silence stderr go through Invoke-Quietly.
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'deploy-guard.ps1')
$tree = Assert-DeployableTree -Root $root -Paths @('kecore_func', 'kecore', 'kefind', 'scoreboard') -AllowNotMain:$AllowNotMain
$commit = $tree.Commit

# The functions the new host must serve: every function decorated with @app.* in function_app.py.
$expected = @()
$decorated = $false
foreach ($line in Get-Content (Join-Path $root 'kecore_func/function_app.py') -Encoding UTF8) {
    if ($line -match '^@app\.') { $decorated = $true; continue }
    if ($line -match '^@') { continue }
    if ($decorated -and $line -match '^(?:async\s+)?def\s+(\w+)\s*\(') { $expected += $Matches[1] }
    if ($line.Trim()) { $decorated = $false }
}
if (-not $expected) { throw "no function found in kecore_func/function_app.py" }

$zipPath = Join-Path ([System.IO.Path]::GetTempPath()) ('kecore-func-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.zip')

function Add-Tree([System.IO.Compression.ZipArchive]$Zip, [string]$Dir, [string]$Prefix) {
    Get-ChildItem -Path $Dir -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($Dir.Length).TrimStart('\', '/') -replace '\\', '/'
        if ($rel -match '(^|/)(tests|__pycache__|examples)/' -or $rel -like '*.pyc') { return }
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($Zip, $_.FullName, $Prefix + $rel) | Out-Null
    }
}

$zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    Add-Tree $zip (Join-Path $root 'kecore_func') ''
    Add-Tree $zip (Join-Path $root 'kecore') 'kecore/'
    Add-Tree $zip (Join-Path $root 'kefind') 'kefind/'
    Add-Tree $zip (Join-Path $root 'scoreboard') 'scoreboard/'
    Add-TextEntry $zip 'BUILD_COMMIT' $commit
} finally {
    $zip.Dispose()
}
Write-Host "Package: $zipPath"

$ok = $false
for ($i = 1; $i -le 4; $i++) {
    az functionapp deployment source config-zip --resource-group $ResourceGroup --name $FunctionApp --src $zipPath --build-remote true -o none
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
    Write-Warning "Attempt $i failed; retrying in 30 s (a cold SCM site right after creation is common)"
    Start-Sleep -Seconds 30
}
Remove-Item $zipPath -ErrorAction SilentlyContinue
if (-not $ok) { throw "Deployment to $FunctionApp failed after 4 attempts" }

# The zip deployment can answer 202 while the remote build is still running, a Function App created
# minutes before cannot reach its identity-based storage until its roles have propagated, and the
# function list Azure keeps (az functionapp function list) stays empty until the triggers are synced.
# 2026-10-06, first deployment: AuthorizationPermissionMismatch on AzureWebJobsStorage for about a
# minute, then a healthy host serving 6 functions while that list still said none (runbook 16.5).
# 2026-10-09: the previous host answered for minutes after the 202 (runbook 19.17). So: sync the
# triggers, then ask the host itself which commit it runs and which functions it serves, until both
# are the new ones, for 10 minutes by the clock.
$sub = az account show --query id -o tsv
if ($LASTEXITCODE -ne 0) { throw "az account show failed (az login?)" }
$base = "https://management.azure.com/subscriptions/$sub/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$FunctionApp"
$hostName = az functionapp show --resource-group $ResourceGroup --name $FunctionApp --query defaultHostName -o tsv
if ($LASTEXITCODE -ne 0 -or -not $hostName) { throw "az functionapp show failed" }
$served, $running, $missing = @(), $null, $expected
$clock = [System.Diagnostics.Stopwatch]::StartNew()
$i = 0
while ($clock.Elapsed.TotalMinutes -lt 10) {
    $i++
    Start-Sleep -Seconds 30
    Invoke-Quietly { az rest --method post --url "$base/syncfunctiontriggers?api-version=2022-03-01" -o none }
    $served = @(Invoke-Quietly { az rest --method get --url "$base/hostruntime/admin/functions?api-version=2022-03-01" --query '[].name' -o tsv })
    $key = Invoke-Quietly { az functionapp keys list --resource-group $ResourceGroup --name $FunctionApp --query 'functionKeys.default' -o tsv }
    $running = $null
    if ($key) {
        try { $running = (Invoke-RestMethod -Uri "https://$hostName/api/kecore/version?code=$key" -TimeoutSec 30).commit } catch { $running = $null }
    }
    $missing = @($expected | Where-Object { $served -notcontains $_ })
    Write-Host ("Attempt {0}: host runs {1}, {2} of {3} functions served" -f $i, ($(if ($running) { $running.Substring(0, [Math]::Min(7, $running.Length)) } else { 'nothing yet' })), ($expected.Count - $missing.Count), $expected.Count)
    if ($running -eq $commit -and -not $missing) { break }
}
if ($running -ne $commit -or $missing) {
    Write-Warning 'Host status:'
    az rest --method get --url "$base/hostruntime/admin/host/status?api-version=2022-03-01"
    $why = @()
    if ($running -ne $commit) { $why += "the host runs '$(if ($running) { $running } else { 'no answer' })', not $($commit.Substring(0, 7))" }
    if ($missing) { $why += "functions not served: $($missing -join ', ')" }
    throw ("after $([int]$clock.Elapsed.TotalMinutes) minutes: " + ($why -join '; ') + " (runbook 16.5, 19.17)")
}
Write-Host "Deployed $($commit.Substring(0, 7)) to $FunctionApp. Functions served by the host:"
$served
