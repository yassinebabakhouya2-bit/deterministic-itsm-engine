<#
.SYNOPSIS
  Turns on the signed machine endpoints of the Diagnostic tab (ServiceNow webhook + timeout sweep)
  on the running web app. Optional: without it the tab works for signed-in users and both
  endpoints answer 404.

.DESCRIPTION
  1. Generates a random signing secret (or keeps the current one) and stores it, with the client the
     webhook feeds, as web app settings DIAG_WEBHOOK_SECRET and DIAG_WEBHOOK_CLIENT.
  2. Excludes exactly two paths from Easy Auth (globalValidation.excludedPaths):
       /api/servicenow/webhook   and   /diag/internal/sweep
     A caller there has no Entra session; the app itself checks an HMAC-SHA256 signature over
     "<X-KE-Timestamp>.<body>" (header X-KE-Signature: sha256=<hex>, 5 minute window).
  3. Writes the secret to clients-local\diag-webhook-secret.txt (git-ignored) for the ServiceNow
     side. The secret is NEVER printed.

  Idempotent. Re-run with -Rotate to issue a new secret (the old one stops working at once).
  Prerequisite: az logged in to this deployment's tenant. Windows PowerShell 5.1 or PowerShell 7.
  ASCII-only source (runbook 9.1).

.EXAMPLE
  .\scripts\enable-diagnostic-webhook.ps1 -ClientId client-s
#>
param(
    [Parameter(Mandatory = $true)][string]$ClientId,
    [string]$ResourceGroup = 'rg-knowledgeengine-v9',
    [string]$NamePrefix = 'knowledgeengine3',
    [switch]$Rotate
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$web = "app-$NamePrefix-v9"
$excluded = @('/api/servicenow/webhook', '/diag/internal/sweep')

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function ConvertTo-AzText($Out) {
    if ($null -eq $Out) { return '' }
    return (($Out | ForEach-Object { "$_" }) -join "`n").Trim()
}
function Invoke-Az {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & az @args } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0]) $($args[1]) failed (exit code $LASTEXITCODE) - see the az error above." }
    return (ConvertTo-AzText $out)
}
function Invoke-AzOptional {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & az @args 2>$null } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { return $null }
    return (ConvertTo-AzText $out)
}

$cfg = @('config', 'clients-local') | ForEach-Object { Join-Path (Join-Path $root $_) "engine.$ClientId.yaml" } | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $cfg) { throw "No engine.$ClientId.yaml in config\ or clients-local\." }

$acct = (Invoke-Az account show -o json) | ConvertFrom-Json
$subId = $acct.id

Write-Step "Signing secret and client on $web"
$curSecret = Invoke-AzOptional webapp config appsettings list --resource-group $ResourceGroup --name $web --query "[?name=='DIAG_WEBHOOK_SECRET'].value" -o tsv
$curClient = Invoke-AzOptional webapp config appsettings list --resource-group $ResourceGroup --name $web --query "[?name=='DIAG_WEBHOOK_CLIENT'].value" -o tsv
$secretFile = Join-Path (Join-Path $root 'clients-local') 'diag-webhook-secret.txt'
if ($curSecret -and -not $Rotate) {
    $secret = $curSecret
    Write-Host '  secret kept (use -Rotate to issue a new one)'
} else {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $secret = ([System.BitConverter]::ToString($bytes)).Replace('-', '').ToLower()
    Write-Host '  new secret generated'
}
if (($secret -ne $curSecret) -or ($curClient -ne $ClientId)) {
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        $body = '{"DIAG_WEBHOOK_SECRET": "' + $secret + '", "DIAG_WEBHOOK_CLIENT": "' + $ClientId + '"}'
        [System.IO.File]::WriteAllText($tmp, $body, $utf8NoBom)
        Invoke-Az webapp config appsettings set --resource-group $ResourceGroup --name $web --settings "@$tmp" -o none | Out-Null
    } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    Write-Host "  app settings set (client: $ClientId)"
} else {
    Write-Host "  app settings already in place (client: $ClientId)"
}
if (-not (Test-Path (Split-Path -Parent $secretFile))) { New-Item -ItemType Directory -Path (Split-Path -Parent $secretFile) | Out-Null }
[System.IO.File]::WriteAllText($secretFile, $secret, $utf8NoBom)

Write-Step "Easy Auth exclusions"
$authUrl = "https://management.azure.com/subscriptions/$subId/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$web/config/authsettingsV2?api-version=2023-12-01"
$auth = (Invoke-Az rest --method get --url $authUrl -o json) | ConvertFrom-Json
if (-not $auth.properties.globalValidation) { throw "No globalValidation in the Easy Auth settings of $web - deploy the foundation first." }
$gv = $auth.properties.globalValidation
$have = @()
if ($gv.PSObject.Properties.Name -contains 'excludedPaths' -and $gv.excludedPaths) { $have = @($gv.excludedPaths) }
$missing = @($excluded | Where-Object { $have -notcontains $_ })
if ($missing.Count -eq 0) {
    Write-Host "  excluded paths already in place: $($excluded -join ' ')"
} else {
    $new = @($have + $missing)
    $gv | Add-Member -NotePropertyName excludedPaths -NotePropertyValue $new -Force
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($tmp, ([ordered]@{ properties = $auth.properties } | ConvertTo-Json -Depth 30), $utf8NoBom)
        Invoke-Az rest --method put --url $authUrl --body "@$tmp" --headers 'Content-Type=application/json' -o none | Out-Null
    } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    Write-Host "  excluded from Easy Auth: $($missing -join ' ')"
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host "  Secret saved to: $secretFile (git-ignored, not printed)"
Write-Host '  ServiceNow side: POST https://<web app>/api/servicenow/webhook with headers'
Write-Host '    X-KE-Timestamp: <unix seconds>    X-KE-Signature: sha256=HMAC_SHA256(secret, "<timestamp>.<raw body>")'
Write-Host '  JSON body: { "event_id", "ticket_number", "short_description", "description", "comment" }'
Write-Host '  The JSON response carries the question, plan or escalation to post back into the ticket.'
