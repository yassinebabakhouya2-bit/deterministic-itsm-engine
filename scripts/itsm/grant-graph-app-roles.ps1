<#
.SYNOPSIS
  Grants Microsoft Graph APPLICATION permissions (app roles) to a managed identity
  (e.g. the ITSM proposal Logic App). ARM/Bicep cannot do this: it is a Graph
  operation (POST /servicePrincipals/{id}/appRoleAssignments). Idempotent.

.EXAMPLE
  .\scripts\itsm\grant-graph-app-roles.ps1 -PrincipalId <managedIdentityPrincipalId> -Roles Directory.Read.All

  Needs an account allowed to grant admin consent on the tenant (Global Admin /
  Privileged Role Administrator), signed in with az login on the PERSONAL tenant.
  Source is ASCII only (runbook 9.1).
#>
param(
    [Parameter(Mandatory = $true)][string]$PrincipalId,
    [Parameter(Mandatory = $true)][string[]]$Roles
)
$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$graphAppId = '00000003-0000-0000-c000-000000000000'

function Invoke-Graph([string]$Method, [string]$Path, $Body) {
    $azArgs = @('rest', '--method', $Method, '--url', "https://graph.microsoft.com/v1.0$Path")
    $tmp = $null
    if ($null -ne $Body) {
        $tmp = [System.IO.Path]::GetTempFileName()
        [System.IO.File]::WriteAllText($tmp, ($Body | ConvertTo-Json -Depth 5), $utf8NoBom)
        $azArgs += @('--body', "@$tmp", '--headers', 'Content-Type=application/json')
    }
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $out = & az @azArgs 2>&1; $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($tmp) { Remove-Item $tmp -ErrorAction SilentlyContinue }
    $text = ($out | ForEach-Object { "$_" }) -join "`n"
    if ($code -ne 0) { throw "Graph $Method $Path failed: $text" }
    if ($text.Trim()) { return ($text | ConvertFrom-Json) }
}

$acct = az account show | ConvertFrom-Json
Write-Host "Signed in as $($acct.user.name) on tenant $($acct.tenantId)" -ForegroundColor Yellow

# No '&' in URLs passed to az.cmd: cmd.exe would treat it as a command separator.
$graphSp = (Invoke-Graph GET "/servicePrincipals?`$filter=appId%20eq%20'$graphAppId'" $null).value[0]
$msi = Invoke-Graph GET "/servicePrincipals/$PrincipalId`?`$select=id,displayName" $null
Write-Host "Target identity: $($msi.displayName) ($($msi.id))"
$existing = @((Invoke-Graph GET "/servicePrincipals/$PrincipalId/appRoleAssignments" $null).value)

foreach ($r in $Roles) {
    $role = $graphSp.appRoles | Where-Object { $_.value -eq $r -and $_.allowedMemberTypes -contains 'Application' }
    if (-not $role) { throw "Graph application permission '$r' not found" }
    if ($existing | Where-Object { $_.appRoleId -eq $role.id -and $_.resourceId -eq $graphSp.id }) {
        Write-Host "[skip] $r already granted"
        continue
    }
    Invoke-Graph POST "/servicePrincipals/$PrincipalId/appRoleAssignments" @{
        principalId = $PrincipalId; resourceId = $graphSp.id; appRoleId = $role.id
    } | Out-Null
    Write-Host "[new ] $r granted" -ForegroundColor Green
}
Write-Host 'Note: a new Graph app role can take a few minutes (token cache) before calls stop returning 403.'
