# deploy-kecore-function.ps1 -- V10 slice 2
# Deploys the kecore Function code (kecore_func/ plus the kecore package) to the
# Function App created by infra/modules/kecore.bicep (slice 1).
#
# Same packaging method as deploy-webapp.ps1 and bootstrap-new-tenant.ps1: zip built
# entry by entry with '/' separators (Compress-Archive writes '\', unreadable on Linux),
# remote build (requirements.txt installed by Oryx), retried on the transient SCM errors
# a freshly created Function App gives (runbook 12.4). The zip is written to the temp
# folder and deleted afterwards: nothing is left in the repository.

param(
    [string]$ResourceGroup = 'rg-knowledgeengine-v9',
    [string]$FunctionApp = 'fn-kecore-knowledgeengine3-v9'
)

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$root = Split-Path -Parent $PSScriptRoot
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
# So: sync the triggers and ask the host itself, through Azure Resource Manager, until it answers.
$sub = az account show --query id -o tsv
$base = "https://management.azure.com/subscriptions/$sub/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$FunctionApp"
$names = $null
for ($i = 1; $i -le 12 -and -not $names; $i++) {
    Start-Sleep -Seconds 30
    az rest --method post --url "$base/syncfunctiontriggers?api-version=2022-03-01" -o none 2>$null
    $names = az rest --method get --url "$base/hostruntime/admin/functions?api-version=2022-03-01" --query '[].name' -o tsv 2>$null
}
if (-not $names) {
    Write-Warning 'No function served after 6 minutes. Host status:'
    az rest --method get --url "$base/hostruntime/admin/host/status?api-version=2022-03-01"
    Write-Warning 'See runbook 16.5.'
    exit 1
}
Write-Host "Deployed to $FunctionApp. Functions served by the host:"
$names
