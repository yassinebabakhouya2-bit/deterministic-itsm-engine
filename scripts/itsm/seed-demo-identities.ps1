<#
.SYNOPSIS
  Jalon 10 - creates the fictional demo identities used by the ITSM action module:
  Entra ID users/groups/role (via Microsoft Graph, through `az rest`) and the
  matching sys_user records + assignment group in the ServiceNow dev instance.

.DESCRIPTION
  Idempotent: existing users/groups/memberships/roles are detected and skipped.
  Initial passwords of NEWLY created Entra users are written to
  clients-local/itsm-demo-credentials.csv (git-ignored). Never commit that file.

  Prerequisites:
    - `az login --tenant <personal tenant>` with an account that is Global Admin
      (or User Administrator + Privileged Role Administrator) on the PERSONAL tenant.
      Check with `az account show` first - do NOT run this against a DXC/[CLIENT-PARENT] tenant.
    - ServiceNow dev instance admin credentials (prompted).

  Source is ASCII only on purpose (Windows PowerShell 5.1 pitfall, runbook 9.1).

.EXAMPLE
  .\scripts\itsm\seed-demo-identities.ps1 -SnInstance dev123456
  .\scripts\itsm\seed-demo-identities.ps1 -SkipServiceNow
#>
param(
    [string]$TenantDomain,
    [string]$SnInstance,
    [switch]$SkipServiceNow
)
$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$config = Get-Content -Raw -Path (Join-Path $PSScriptRoot 'demo-identities.json') | ConvertFrom-Json
$credFile = Join-Path $PSScriptRoot '..\..\clients-local\itsm-demo-credentials.csv'

# ---------------------------------------------------------------- Graph helpers
function Invoke-Graph {
    param([string]$Method, [string]$Path, $Body, [switch]$AllowFail)
    $azArgs = @('rest', '--method', $Method, '--url', "https://graph.microsoft.com/v1.0$Path")
    $tmp = $null
    if ($null -ne $Body) {
        $tmp = [System.IO.Path]::GetTempFileName()
        [System.IO.File]::WriteAllText($tmp, ($Body | ConvertTo-Json -Depth 10), $utf8NoBom)
        $azArgs += @('--body', "@$tmp", '--headers', 'Content-Type=application/json')
    }
    # PS 5.1: with ErrorActionPreference=Stop, native stderr redirected by 2>&1 becomes a
    # terminating NativeCommandError. Relax it just for the az call and use the exit code instead.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = & az @azArgs 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    $out = $out | ForEach-Object { "$_" }
    if ($tmp) { Remove-Item $tmp -ErrorAction SilentlyContinue }
    if ($code -ne 0) {
        $msg = ($out | Out-String)
        if ($AllowFail) { return [pscustomobject]@{ __error = $msg } }
        throw "Graph $Method $Path failed: $msg"
    }
    $text = ($out | Out-String).Trim()
    if ($text) { return ($text | ConvertFrom-Json) }
    return $null
}
function Get-GraphFirst([string]$Collection, [string]$Filter) {
    $f = [uri]::EscapeDataString($Filter)
    $r = Invoke-Graph -Method GET -Path "/$Collection`?`$filter=$f"
    if ($r.value -and $r.value.Count -gt 0) { return $r.value[0] }
    return $null
}
function New-DemoPassword {
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'.ToCharArray()
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 14
    $rng.GetBytes($bytes)
    $core = -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
    return "Ke$core!9"
}

# ---------------------------------------------------------------- Safety check
$acct = az account show | ConvertFrom-Json
Write-Host "Signed in as $($acct.user.name) on tenant $($acct.tenantId)" -ForegroundColor Yellow
$confirm = Read-Host "Is this the PERSONAL KnowledgeEngine tenant (not DXC/[CLIENT-PARENT])? Type YES to continue"
if ($confirm -ne 'YES') { throw 'Aborted by operator.' }

if (-not $TenantDomain) {
    $TenantDomain = ((Invoke-Graph -Method GET -Path '/domains').value | Where-Object { $_.isDefault }).id
}
Write-Host "Using domain: $TenantDomain"

# ---------------------------------------------------------------- Entra groups
$groupIds = @{}
foreach ($g in $config.groups) {
    $existing = Get-GraphFirst 'groups' "displayName eq '$($g.name)'"
    if ($existing) {
        $groupIds[$g.name] = $existing.id
        Write-Host "[skip] group $($g.name) exists"
    } else {
        $new = Invoke-Graph -Method POST -Path '/groups' -Body @{
            displayName = $g.name; description = $g.description
            mailEnabled = $false; mailNickname = $g.name; securityEnabled = $true
        }
        $groupIds[$g.name] = $new.id
        Write-Host "[new ] group $($g.name)" -ForegroundColor Green
    }
}

# ---------------------------------------------------------------- Entra users
$userIds = @{}
$createdCreds = @()
foreach ($u in $config.users) {
    $upn = "$($u.nick)@$TenantDomain"
    $existing = Get-GraphFirst 'users' "userPrincipalName eq '$upn'"
    if ($existing) {
        $userIds[$u.nick] = $existing.id
        Write-Host "[skip] user $upn exists"
        continue
    }
    $initPwd = New-DemoPassword
    $new = Invoke-Graph -Method POST -Path '/users' -Body @{
        accountEnabled    = $true
        displayName       = $u.displayName
        mailNickname      = ($u.nick -replace '\.', '')
        userPrincipalName = $upn
        usageLocation     = $config.usageLocation
        jobTitle          = $u.jobTitle
        department        = $u.department
        companyName       = $config.companyName
        passwordProfile   = @{ password = $initPwd; forceChangePasswordNextSignIn = $false }
    }
    $userIds[$u.nick] = $new.id
    $createdCreds += [pscustomobject]@{ upn = $upn; initialPassword = $initPwd; scenario = $u.scenario }
    Write-Host "[new ] user $upn" -ForegroundColor Green
}

# managers
foreach ($u in $config.users) {
    if (-not $u.manager) { continue }
    $r = Invoke-Graph -Method PUT -Path "/users/$($userIds[$u.nick])/manager/`$ref" -AllowFail -Body @{
        '@odata.id' = "https://graph.microsoft.com/v1.0/users/$($userIds[$u.manager])"
    }
    if ($r.__error) { Write-Warning "manager of $($u.nick): $($r.__error)" }
}

# group memberships
foreach ($u in $config.users) {
    foreach ($gn in $u.groups) {
        $r = Invoke-Graph -Method POST -Path "/groups/$($groupIds[$gn])/members/`$ref" -AllowFail -Body @{
            '@odata.id' = "https://graph.microsoft.com/v1.0/directoryObjects/$($userIds[$u.nick])"
        }
        if ($r.__error) {
            if ($r.__error -match 'already exist') { Write-Host "[skip] $($u.nick) already in $gn" }
            else { Write-Warning "add $($u.nick) to ${gn}: $($r.__error)" }
        } else { Write-Host "[new ] $($u.nick) -> $gn" -ForegroundColor Green }
    }
}

# directory roles (guardrail persona)
foreach ($u in $config.users) {
    if (-not $u.entraRole) { continue }
    $def = Get-GraphFirst 'roleManagement/directory/roleDefinitions' "displayName eq '$($u.entraRole)'"
    if (-not $def) { Write-Warning "role '$($u.entraRole)' not found"; continue }
    $uid = $userIds[$u.nick]
    $has = Get-GraphFirst 'roleManagement/directory/roleAssignments' "principalId eq '$uid' and roleDefinitionId eq '$($def.id)'"
    if ($has) { Write-Host "[skip] $($u.nick) already $($u.entraRole)"; continue }
    Invoke-Graph -Method POST -Path '/roleManagement/directory/roleAssignments' -Body @{
        principalId = $uid; roleDefinitionId = $def.id; directoryScopeId = '/'
    } | Out-Null
    Write-Host "[new ] $($u.nick) granted $($u.entraRole)" -ForegroundColor Green
}

if ($createdCreds.Count -gt 0) {
    $exists = Test-Path $credFile
    $createdCreds | Export-Csv -Path $credFile -NoTypeInformation -Append:$exists -Encoding UTF8
    Write-Host "Initial passwords written to $credFile (git-ignored). Do not commit." -ForegroundColor Yellow
}

# ---------------------------------------------------------------- ServiceNow
if ($SkipServiceNow) { Write-Host 'ServiceNow skipped.'; return }
if (-not $SnInstance) { $SnInstance = Read-Host 'ServiceNow instance name (e.g. dev123456)' }
# Get-Credential in PS 5.1 can return the user name as "\admin" (empty domain prefix),
# which ServiceNow rejects with 401 -> strip it. Length is printed to catch truncated pastes.
$snCred = Get-Credential -UserName 'admin' -Message "ServiceNow admin for https://$SnInstance.service-now.com"
$snUser = $snCred.UserName.Trim().TrimStart('\')
$snPlain = $snCred.GetNetworkCredential().Password
Write-Host "User '$snUser', password length $($snPlain.Length)"
$basic = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes("${snUser}:$snPlain"))
$snPlain = $null
# Fail fast with a clear message if the credentials are wrong
try {
    Invoke-RestMethod -Method GET -Uri "https://$SnInstance.service-now.com/api/now/table/sys_user?sysparm_limit=1" `
        -Headers @{ Authorization = "Basic $basic"; Accept = 'application/json' } -UseBasicParsing | Out-Null
} catch {
    throw "ServiceNow authentication failed for '$snUser' on $SnInstance (check password on the developer portal, and that the instance is awake). $_"
}
Write-Host "ServiceNow auth OK as $snUser" -ForegroundColor Green
$snHeaders = @{ Authorization = "Basic $basic"; Accept = 'application/json' }
$snBase = "https://$SnInstance.service-now.com/api/now/table"

function Invoke-Sn([string]$Method, [string]$Path, $Body) {
    $p = @{ Method = $Method; Uri = "$snBase/$Path"; Headers = $snHeaders; UseBasicParsing = $true }
    if ($null -ne $Body) {
        $p.Body = [System.Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 5))
        $p.ContentType = 'application/json; charset=utf-8'
    }
    return (Invoke-RestMethod @p).result
}
function Get-SnFirst([string]$Table, [string]$Query) {
    $q = [uri]::EscapeDataString($Query)
    $r = Invoke-Sn GET "$Table`?sysparm_query=$q&sysparm_limit=1" $null
    if ($r -and @($r).Count -gt 0) { return @($r)[0] }
    return $null
}

$grpName = $config.servicenow.assignmentGroup
$snGroup = Get-SnFirst 'sys_user_group' "name=$grpName"
if ($snGroup) { Write-Host "[skip] SN group $grpName exists" }
else {
    $snGroup = Invoke-Sn POST 'sys_user_group' @{ name = $grpName; description = $config.servicenow.assignmentGroupDescription }
    Write-Host "[new ] SN group $grpName" -ForegroundColor Green
}

$snUserIds = @{}
foreach ($u in $config.users) {
    $upn = "$($u.nick)@$TenantDomain"
    $existing = Get-SnFirst 'sys_user' "user_name=$($u.nick)"
    if ($existing) { $snUserIds[$u.nick] = $existing.sys_id; Write-Host "[skip] SN user $($u.nick) exists"; continue }
    $first, $last = $u.displayName -split " ", 2
    $new = Invoke-Sn POST 'sys_user' @{
        user_name = $u.nick; first_name = $first; last_name = $last
        email = $upn; title = $u.jobTitle; active = 'true'
    }
    $snUserIds[$u.nick] = $new.sys_id
    Write-Host "[new ] SN user $($u.nick) (email=$upn)" -ForegroundColor Green
}
foreach ($u in $config.users) {
    if (-not $u.manager) { continue }
    Invoke-Sn PATCH "sys_user/$($snUserIds[$u.nick])" @{ manager = $snUserIds[$u.manager] } | Out-Null
}

Write-Host "`nDone. Entra users and ServiceNow callers are linked by email = UPN (@$TenantDomain)." -ForegroundColor Cyan
