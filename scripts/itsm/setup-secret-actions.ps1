<#
.SYNOPSIS
  Jalon 10 step 10.4b - tenant prerequisites for password_reset / mfa_reset by the executor
  Logic App identity. Idempotent.

  1. Graph application permissions on the executor identity:
       UserAuthenticationMethod.ReadWrite.All  (delete Authenticator methods, create TAP)
       User-PasswordProfile.ReadWrite.All      (reset a password, app-only)
  2. Entra directory role "Helpdesk Administrator" on the executor identity: app-only password
     resets require a directory role, and Helpdesk Administrator is deliberately the weakest
     one -- it CANNOT reset an administrator (built-in guardrail, on top of our own precheck).
  3. Temporary Access Pass authentication method policy enabled for all users (needed to issue TAPs).

.EXAMPLE
  .\scripts\itsm\setup-secret-actions.ps1 -PrincipalId 4c2b9713-e0e4-4ac3-bed5-e5ff67b24c7e
  Source is ASCII only (runbook 9.1).
#>
param([Parameter(Mandatory = $true)][string]$PrincipalId)
$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Invoke-Graph([string]$Method, [string]$Path, $Body) {
    $a = @('rest', '--method', $Method, '--url', "https://graph.microsoft.com/v1.0$Path")
    $tmp = $null
    if ($null -ne $Body) {
        $tmp = [System.IO.Path]::GetTempFileName()
        [System.IO.File]::WriteAllText($tmp, ($Body | ConvertTo-Json -Depth 6), $utf8NoBom)
        $a += @('--body', "@$tmp", '--headers', 'Content-Type=application/json')
    }
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $out = & az @a 2>&1; $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($tmp) { Remove-Item $tmp -ErrorAction SilentlyContinue }
    $text = ($out | ForEach-Object { "$_" }) -join "`n"
    if ($code -ne 0) { throw "Graph $Method $Path failed: $text" }
    if ($text.Trim()) { return ($text | ConvertFrom-Json) }
}

# 1. app roles (reuses the idempotent grant script)
& (Join-Path $PSScriptRoot 'grant-graph-app-roles.ps1') -PrincipalId $PrincipalId -Roles UserAuthenticationMethod.ReadWrite.All, User-PasswordProfile.ReadWrite.All

# 2. directory role Helpdesk Administrator
$roleName = 'Helpdesk Administrator'
$f = [uri]::EscapeDataString("displayName eq '$roleName'")
$def = (Invoke-Graph GET "/roleManagement/directory/roleDefinitions?`$filter=$f" $null).value[0]
$f2 = [uri]::EscapeDataString("principalId eq '$PrincipalId'")
$existing = @((Invoke-Graph GET "/roleManagement/directory/roleAssignments?`$filter=$f2" $null).value | Where-Object { $_.roleDefinitionId -eq $def.id })
if ($existing.Count -gt 0) { Write-Host "[skip] $roleName already assigned" }
else {
    Invoke-Graph POST '/roleManagement/directory/roleAssignments' @{ principalId = $PrincipalId; roleDefinitionId = $def.id; directoryScopeId = '/' } | Out-Null
    Write-Host "[new ] $roleName assigned to the executor identity" -ForegroundColor Green
}

# 3. Temporary Access Pass policy
# The az CLI token does NOT carry Policy.ReadWrite.AuthenticationMethod (Forbidden on this endpoint,
# hit 2026-09-25) -> non-fatal: print the portal steps instead.
$tapPath = '/policies/authenticationMethodsPolicy/authenticationMethodConfigurations/TemporaryAccessPass'
try {
    $tap = Invoke-Graph GET $tapPath $null
    if ($tap.state -eq 'enabled') { Write-Host '[skip] Temporary Access Pass policy already enabled' }
    else {
        Invoke-Graph PATCH $tapPath @{
            '@odata.type' = '#microsoft.graph.temporaryAccessPassAuthenticationMethodConfiguration'
            state = 'enabled'
            includeTargets = @(@{ targetType = 'group'; id = 'all_users'; isRegistrationRequired = $false })
        } | Out-Null
        Write-Host '[new ] Temporary Access Pass policy enabled (all users)' -ForegroundColor Green
    }
} catch {
    Write-Warning 'Cannot manage the Temporary Access Pass policy with the az CLI token. Enable it in the portal:'
    Write-Warning '  Entra admin center > Protection > Authentication methods > Policies > Temporary Access Pass > Enable, Target: All users > Save'
}
Write-Host 'Allow a few minutes for the directory role and app roles to take effect.'
