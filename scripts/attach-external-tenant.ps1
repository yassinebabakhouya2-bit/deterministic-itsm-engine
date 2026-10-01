<#
.SYNOPSIS
  Gives every user of another organization's Entra tenant access to ONE client of the running
  deployment (the whole external tenant = that client, no group), without re-running
  scripts\bootstrap-new-tenant.ps1.

.DESCRIPTION
  1. engine.<client>.yaml (config/ or clients-local/): access.entraTenantId = the other
     tenant, access.entraGroup removed (app/auth.py: no entraGroup = every user of that tenant).
  2. App Registration <AppDisplayName>: sign-in audience AzureADMultipleOrgs. While it is
     single-tenant, Entra itself refuses a user of another tenant (AADSTS50020).
  3. Easy Auth of app-<prefix>-v9: issuer https://login.microsoftonline.com/organizations/v2.0
     and WEBSITE_AUTH_AAD_ALLOWED_TENANTS = this tenant + every tenant a shipped client config
     points to (platform-level allowlist, checked before the app code runs).
  4. Web app code redeployed with the client configs (deploy-webapp.ps1 -ClientsLocal).
  5. Prints the one-time admin consent step, done in the other organization.

  Idempotent: what is already in place is left as is. A later bootstrap-new-tenant.ps1 run
  keeps this set-up (it reads the client configs). Nothing secret is read or printed.
  Prerequisite: az logged in to THIS deployment's tenant (App Registration + subscription).
  Windows PowerShell 5.1 or PowerShell 7. ASCII-only source (runbook 9.1).

.EXAMPLE
  .\scripts\attach-external-tenant.ps1 -ClientId clientc -TenantId <tenant-id-of-the-other-organization>
#>
param(
    [Parameter(Mandatory = $true)][string]$ClientId,
    # Tenant ID (GUID) of the other organization - e.g. the GUID in "from identity provider
    # 'https://sts.windows.net/<guid>/'" of an AADSTS50020 error.
    [Parameter(Mandatory = $true)][string]$TenantId,
    [string]$ResourceGroup = 'rg-knowledgeengine-v9',
    [string]$NamePrefix = 'knowledgeengine3',
    [string]$AppDisplayName = 'KnowledgeEngineV9-WebApp-Auth',
    # Real clients (git-ignored clients-local/) whose engine.<client>.yaml ships with the app.
    [string[]]$ClientsLocal = @('client-s'),
    # Leave the web app code as is (then redeploy it yourself with deploy-webapp.ps1).
    [switch]$SkipWebApp,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$guidPattern = '^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$'
$multiTenantIssuer = 'https://login.microsoftonline.com/organizations/v2.0'

# ---------------------------------------------------------------------------------------
# Helpers (same rules as bootstrap-new-tenant.ps1: az.cmd is a batch file on Windows, so no
# argument may contain & | ( ) < > ^ or a comma - values like that go through a file)
# ---------------------------------------------------------------------------------------
function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }

function ConvertTo-AzText($Out) {
    if ($null -eq $Out) { return '' }
    return (($Out | ForEach-Object { "$_" }) -join "`n").Trim()
}

function Invoke-Az {
    # Runs az and returns its trimmed stdout; throws on a non-zero exit code.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & az @args } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0]) $($args[1]) failed (exit code $LASTEXITCODE) - see the az error above." }
    return (ConvertTo-AzText $out)
}

function Invoke-AzOptional {
    # Same as Invoke-Az but returns $null on failure, stderr hidden.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & az @args 2>$null } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { return $null }
    return (ConvertTo-AzText $out)
}

function Get-RelPath([string]$Path) { return $Path.Substring($root.Length + 1) }

function Get-ClientConfigPath([string]$Client) {
    # Synthetic clients live in config/, real ones in the git-ignored clients-local/.
    foreach ($d in @('config', 'clients-local')) {
        $p = Join-Path (Join-Path $root $d) "engine.$Client.yaml"
        if (Test-Path $p) { return [System.IO.Path]::GetFullPath($p) }
    }
    throw "Neither config/engine.$Client.yaml nor clients-local/engine.$Client.yaml exists."
}

function Get-AccessBlock([string]$Path) {
    # access.entraTenantId (lower case, '' if absent) and whether an entraGroup key exists.
    $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $t = [regex]::Match($text, '(?m)^[ \t]*entraTenantId:[ \t]*"?([^"\s#]*)')
    $tid = ''
    if ($t.Success) { $tid = $t.Groups[1].Value.ToLower() }
    return [pscustomobject]@{ TenantId = $tid; HasGroup = [regex]::IsMatch($text, '(?m)^[ \t]*entraGroup:') }
}

function Set-ExternalAccessFile([string]$Path, [string]$Tenant) {
    # access.entraTenantId = the other organization's tenant, entraGroup line removed.
    # UTF-8 without BOM, line endings kept.
    $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $patTenant = '(?m)^([ \t]*)entraTenantId:[^\r\n]*'
    if (-not [regex]::IsMatch($text, $patTenant)) { throw "No access.entraTenantId line in $(Get-RelPath $Path)" }
    $line = 'entraTenantId: "' + $Tenant + '"   # external tenant, whole tenant = this client (no entraGroup), set by scripts/attach-external-tenant.ps1'
    $new = [regex]::Replace($text, $patTenant, ('${1}' + $line))
    $new = [regex]::Replace($new, '(?m)^[ \t]*entraGroup:[^\r\n]*(\r?\n)?', '')
    $a = Get-AccessBlock $Path
    if (($a.TenantId -eq $Tenant) -and (-not $a.HasGroup)) {
        Write-Host "  $(Get-RelPath $Path): tenant $Tenant, no group (already)"
        return
    }
    [System.IO.File]::WriteAllText($Path, $new, $utf8NoBom)
    Write-Host "  $(Get-RelPath $Path) -> tenant $Tenant, no group (was $($a.TenantId), group: $($a.HasGroup))"
}

function Get-ShippedConfigPaths([string[]]$LocalClients) {
    # The client configs the web app package carries (deploy-webapp.ps1): every
    # config/engine.*.yaml plus clients-local/engine.<client>.yaml of $LocalClients.
    $paths = New-Object System.Collections.Generic.List[string]
    foreach ($f in @(Get-ChildItem -Path (Join-Path $root 'config') -Filter 'engine.*.yaml' -File)) {
        $paths.Add([System.IO.Path]::GetFullPath($f.FullName))
    }
    foreach ($c in $LocalClients) {
        if (-not $c) { continue }
        $p = Join-Path (Join-Path $root 'clients-local') "engine.$c.yaml"
        if (Test-Path $p) { $paths.Add([System.IO.Path]::GetFullPath($p)) }
    }
    return $paths.ToArray()
}

function Get-AllowedTenants([string]$HomeTenant, [string[]]$Paths) {
    # WEBSITE_AUTH_AAD_ALLOWED_TENANTS: this tenant + every tenant a shipped config points to.
    $ids = New-Object System.Collections.Generic.List[string]
    $ids.Add($HomeTenant.ToLower())
    foreach ($p in $Paths) {
        $t = (Get-AccessBlock $p).TenantId
        if (($t -match $guidPattern) -and (-not $ids.Contains($t))) { $ids.Add($t) }
    }
    if ($ids.Count -gt 10) { throw "$($ids.Count) Entra tenants in the client configs: Easy Auth accepts at most 10." }
    return $ids.ToArray()
}

# ---------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------
if ($TenantId -notmatch $guidPattern) {
    throw "-TenantId must be a tenant ID (GUID), e.g. the one in `"from identity provider 'https://sts.windows.net/<guid>/'`" of an AADSTS50020 error."
}
$TenantId = $TenantId.ToLower()
if ($ClientId -eq 'client-v') { throw 'client-v ([CLIENT-PARENT]) is abandoned.' }

Push-Location $root
try {
    Write-Step 'Preflight'
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Azure CLI (az) not found in PATH.' }
    $cfg = Get-ClientConfigPath $ClientId
    if (((Split-Path -Leaf (Split-Path -Parent $cfg)) -eq 'clients-local') -and ($ClientsLocal -notcontains $ClientId)) {
        $ClientsLocal = @($ClientsLocal) + $ClientId
    }

    $acct = (Invoke-Az account show -o json) | ConvertFrom-Json
    $homeTenant = "$($acct.tenantId)".ToLower()
    $subId = $acct.id
    if ($TenantId -eq $homeTenant) {
        throw "$TenantId is this deployment's own tenant: give $ClientId an Entra group instead (bootstrap entra phase), not the whole tenant."
    }
    try {
        # Public OpenID metadata: catches a mistyped tenant ID before anything changes.
        Invoke-RestMethod -Uri "https://login.microsoftonline.com/$TenantId/v2.0/.well-known/openid-configuration" -UseBasicParsing | Out-Null
    } catch {
        throw "Tenant $TenantId not found by Microsoft Entra ID (or login.microsoftonline.com unreachable): $($_.Exception.Message)"
    }
    foreach ($p in @(Get-ShippedConfigPaths $ClientsLocal)) {
        if ($p -eq $cfg) { continue }
        if ((Get-AccessBlock $p).TenantId -eq $TenantId) {
            throw "$(Get-RelPath $p) already points to tenant $TenantId. An external tenant maps to ONE client here (app/auth.py)."
        }
    }

    $web = "app-$NamePrefix-v9"
    $appId = Invoke-Az ad app list --display-name $AppDisplayName --query '[0].appId' -o tsv
    if (-not $appId) { throw "App Registration '$AppDisplayName' not found - is az logged in to this deployment's tenant?" }
    $webHost = Invoke-Az webapp show --resource-group $ResourceGroup --name $web --query defaultHostName -o tsv

    Write-Host "  This tenant  : $homeTenant ($($acct.user.name))"
    Write-Host "  App          : https://$webHost - App Registration $AppDisplayName ($appId)"
    Write-Host "  Client       : $ClientId ($(Get-RelPath $cfg))"
    Write-Host "  Other tenant : $TenantId - every user there gets $ClientId, and only $ClientId"
    if (-not $Yes) {
        $answer = Read-Host 'Apply? (y/N)'
        if ($answer -notin @('y', 'Y', 'yes', 'o', 'O', 'oui')) { throw 'Aborted.' }
    }

    # -----------------------------------------------------------------------------------
    Write-Step "$ClientId config"
    Set-ExternalAccessFile $cfg $TenantId

    # -----------------------------------------------------------------------------------
    Write-Step 'App Registration: sign-in from other organizations'
    $aud = Invoke-Az ad app show --id $appId --query signInAudience -o tsv
    if ($aud -eq 'AzureADMultipleOrgs') {
        Write-Host '  signInAudience AzureADMultipleOrgs (already)'
    } else {
        Invoke-Az ad app update --id $appId --sign-in-audience AzureADMultipleOrgs | Out-Null
        Write-Host "  signInAudience $aud -> AzureADMultipleOrgs"
    }

    # -----------------------------------------------------------------------------------
    Write-Step "Easy Auth of $web"
    $authUrl = "https://management.azure.com/subscriptions/$subId/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$web/config/authsettingsV2?api-version=2023-12-01"
    $auth = (Invoke-Az rest --method get --url $authUrl -o json) | ConvertFrom-Json
    $aad = $null
    if ($auth.properties -and $auth.properties.identityProviders) { $aad = $auth.properties.identityProviders.azureActiveDirectory }
    if ((-not $aad) -or (-not $aad.registration)) {
        throw "No Microsoft Entra provider in the Easy Auth settings of $web - deploy the foundation first (bootstrap -From infra)."
    }
    $oldIssuer = "$($aad.registration.openIdIssuer)"
    if ($oldIssuer -eq $multiTenantIssuer) {
        Write-Host "  issuer $multiTenantIssuer (already)"
    } else {
        $aad.registration | Add-Member -NotePropertyName openIdIssuer -NotePropertyValue $multiTenantIssuer -Force
        $tmp = [System.IO.Path]::GetTempFileName()
        try {
            [System.IO.File]::WriteAllText($tmp, ([ordered]@{ properties = $auth.properties } | ConvertTo-Json -Depth 30), $utf8NoBom)
            Invoke-Az rest --method put --url $authUrl --body "@$tmp" --headers 'Content-Type=application/json' -o none | Out-Null
        } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        Write-Host "  issuer $oldIssuer -> $multiTenantIssuer"
    }

    $allowed = @(Get-AllowedTenants $homeTenant @(Get-ShippedConfigPaths $ClientsLocal))
    $want = $allowed -join ','
    $cur = Invoke-AzOptional webapp config appsettings list --resource-group $ResourceGroup --name $web --query "[?name=='WEBSITE_AUTH_AAD_ALLOWED_TENANTS'].value" -o tsv
    if ($cur -eq $want) {
        Write-Host "  allowed tenants $want (already)"
    } else {
        # Through a file: the comma-separated value must not cross az.cmd argument parsing.
        $tmp = [System.IO.Path]::GetTempFileName()
        try {
            [System.IO.File]::WriteAllText($tmp, ('{"WEBSITE_AUTH_AAD_ALLOWED_TENANTS": "' + $want + '"}'), $utf8NoBom)
            Invoke-Az webapp config appsettings set --resource-group $ResourceGroup --name $web --settings "@$tmp" -o none | Out-Null
        } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        Write-Host "  allowed tenants $cur -> $want"
    }

    # -----------------------------------------------------------------------------------
    if ($SkipWebApp) {
        Write-Step 'Web app code: not redeployed (-SkipWebApp)'
        Write-Host "  Before testing: .\deploy-webapp.ps1 -ResourceGroup $ResourceGroup -WebAppName $web -ClientsLocal $($ClientsLocal -join ',')"
    } else {
        Write-Step "Web app code with the client configs ($web)"
        & (Join-Path $root 'deploy-webapp.ps1') -ResourceGroup $ResourceGroup -WebAppName $web -ClientsLocal $ClientsLocal
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'az webapp deploy reported a failure - its status poll can fail on a real success (runbook 7): open the site before re-running.'
        }
    }

    # -----------------------------------------------------------------------------------
    Write-Step 'Done - one step left, in the other organization'
    Write-Host "  1. Once, a Global Administrator of tenant $TenantId approves the app for the organization:"
    Write-Host "     sign in to https://$webHost (private window), tick 'Consent on behalf of your organization', Accept -"
    Write-Host "     or open https://login.microsoftonline.com/$TenantId/adminconsent?client_id=$appId"
    Write-Host '     (whatever page that link lands on afterwards, the consent is recorded).'
    Write-Host "  2. Every user of that tenant then signs in to https://$webHost and gets $ClientId only."
    Write-Host "  Users of this tenant keep the clients of their groups; $ClientId is no longer one of them."
    Write-Host "  A bootstrap-new-tenant.ps1 re-run keeps this: it reads the tenant from $(Get-RelPath $cfg)."
} finally {
    Pop-Location
}
