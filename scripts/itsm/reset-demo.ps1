<#
.SYNOPSIS
  Jalon 10 - resets the ITSM demo so it can be replayed: Entra demo users re-enabled and
  group memberships restored to demo-identities.json, the 7 ServiceNow demo tickets reopened,
  and the itsmtickets table partition emptied (the poll / propose Logic Apps then rebuild
  it within ~10 minutes: poll every 5 min, propose every 5 min).

.DESCRIPTION
  - Only touches the DEMO users and DEMO groups listed in demo-identities.json (other
    members of those groups, e.g. real admins, are left alone).
  - ServiceNow: tickets are reopened (not deleted) with the integration account
    svc_ke_itsm (password read from Key Vault) -> no admin basic-auth exception needed.
    Ticket numbers stay the same; the reset leaves a work note for traceability.
  - Table: deletes every row of the itsm-demo partition (proposal / decision / execution).
  Source is ASCII only (runbook 9.1).

.EXAMPLE
  .\scripts\itsm\reset-demo.ps1
#>
param(
    [string]$SnInstance = 'dev374242',
    [string]$KeyVaultName = 'kv-knowledgeengine-v9',
    [string]$SnSecretName = 'servicenow-svc-ke-itsm-password',
    [string]$SnUser = 'svc_ke_itsm',
    [string]$StorageAccount = 'stknowledgeengine2v9',
    [string]$Table = 'itsmtickets',
    [string]$Partition = 'itsm-demo'
)
$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$identities = Get-Content -Raw -Path (Join-Path $PSScriptRoot 'demo-identities.json') | ConvertFrom-Json
$tickets = Get-Content -Raw -Encoding UTF8 -Path (Join-Path $PSScriptRoot 'demo-tickets.json') | ConvertFrom-Json

function Invoke-Az([string[]]$AzArgs) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $out = & az @AzArgs 2>&1; $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    $text = ($out | ForEach-Object { "$_" }) -join "`n"
    return [pscustomobject]@{ code = $code; text = $text }
}
function Invoke-Graph([string]$Method, [string]$Path, $Body, [switch]$AllowFail) {
    $a = @('rest', '--method', $Method, '--url', "https://graph.microsoft.com/v1.0$Path")
    $tmp = $null
    if ($null -ne $Body) {
        $tmp = [System.IO.Path]::GetTempFileName()
        [System.IO.File]::WriteAllText($tmp, ($Body | ConvertTo-Json -Depth 5), $utf8NoBom)
        $a += @('--body', "@$tmp", '--headers', 'Content-Type=application/json')
    }
    $r = Invoke-Az $a
    if ($tmp) { Remove-Item $tmp -ErrorAction SilentlyContinue }
    if ($r.code -ne 0) {
        if ($AllowFail) { return [pscustomobject]@{ __error = $r.text } }
        throw "Graph $Method $Path failed: $($r.text)"
    }
    if ($r.text.Trim()) { return ($r.text | ConvertFrom-Json) }
}

$acct = az account show | ConvertFrom-Json
Write-Host "Signed in as $($acct.user.name) on tenant $($acct.tenantId)" -ForegroundColor Yellow
if ((Read-Host 'Reset the ITSM demo on this PERSONAL tenant? Type YES') -ne 'YES') { throw 'Aborted.' }
$domain = ((Invoke-Graph GET '/domains' $null).value | Where-Object { $_.isDefault }).id

# ---------------------------------------------------------------- Entra: users
$userIds = @{}
foreach ($u in $identities.users) {
    $upn = "$($u.nick)@$domain"
    $usr = Invoke-Graph GET "/users/$upn`?`$select=id,accountEnabled" $null
    $userIds[$u.nick] = $usr.id
    if (-not $usr.accountEnabled) {
        Invoke-Graph PATCH "/users/$($usr.id)" @{ accountEnabled = $true } | Out-Null
        Write-Host "[fix ] $($u.nick) re-enabled" -ForegroundColor Green
    }
}

# ---------------------------------------------------------------- Entra: demo group memberships
foreach ($g in $identities.groups) {
    $f = [uri]::EscapeDataString("displayName eq '$($g.name)'")
    $grp = (Invoke-Graph GET "/groups?`$filter=$f" $null).value[0]
    $members = @((Invoke-Graph GET "/groups/$($grp.id)/members?`$select=id" $null).value | ForEach-Object { $_.id })
    foreach ($u in $identities.users) {
        $uid = $userIds[$u.nick]
        $want = @($u.groups) -contains $g.name
        $has = $members -contains $uid
        if ($want -and -not $has) {
            Invoke-Graph POST "/groups/$($grp.id)/members/`$ref" @{ '@odata.id' = "https://graph.microsoft.com/v1.0/directoryObjects/$uid" } | Out-Null
            Write-Host "[fix ] $($u.nick) -> $($g.name)" -ForegroundColor Green
        } elseif (-not $want -and $has) {
            Invoke-Graph DELETE "/groups/$($grp.id)/members/$uid/`$ref" $null | Out-Null
            Write-Host "[fix ] $($u.nick) removed from $($g.name)" -ForegroundColor Green
        }
    }
}
Write-Host 'Note: the Microsoft 365 group KnowledgeEngineV9 removed by an offboarding test is not a demo group; re-add manually if needed.' -ForegroundColor DarkYellow

# ---------------------------------------------------------------- ServiceNow: reopen the demo tickets
# stdout only: az warnings on stderr must never end up inside the password
$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$pw = az keyvault secret show --vault-name $KeyVaultName --name $SnSecretName --query value -o tsv 2>$null
$code = $LASTEXITCODE; $ErrorActionPreference = $prev
if ($code -ne 0 -or -not $pw) { throw "Cannot read $SnSecretName from Key Vault (need Key Vault Secrets User/Officer on $KeyVaultName)" }
$basic = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes("${SnUser}:$($pw.Trim())"))
$pw = $null
$snHeaders = @{ Authorization = "Basic $basic"; Accept = 'application/json' }
$snBase = "https://$SnInstance.service-now.com/api/now/table"
function Reopen([string]$TableName, [string]$Key, [string]$State) {
    $q = [uri]::EscapeDataString("correlation_id=$Key")
    $rec = @((Invoke-RestMethod -Method GET -Uri "$snBase/$TableName`?sysparm_query=$q&sysparm_fields=sys_id,number,state&sysparm_limit=1" -Headers $snHeaders -UseBasicParsing).result)
    if ($rec.Count -eq 0 -or -not $rec[0]) { Write-Warning "$Key not found in $TableName"; return }
    $body = [System.Text.Encoding]::UTF8.GetBytes((@{ state = $State; work_notes = '[KnowledgeEngine] demo reset - ticket reopened for a new demo run' } | ConvertTo-Json))
    Invoke-RestMethod -Method PATCH -Uri "$snBase/$TableName/$($rec[0].sys_id)" -Headers $snHeaders -Body $body -ContentType 'application/json; charset=utf-8' -UseBasicParsing | Out-Null
    Write-Host "[fix ] $($rec[0].number) reopened (state $($rec[0].state) -> $State)" -ForegroundColor Green
}
foreach ($t in $tickets.incidents) { Reopen 'incident' $t.key '1' }
foreach ($t in $tickets.requests) { Reopen 'sc_req_item' $t.key '1' }

# ---------------------------------------------------------------- Table: empty the demo partition
$r = Invoke-Az @('storage', 'entity', 'query', '--account-name', $StorageAccount, '--table-name', $Table,
    '--filter', "PartitionKey eq '$Partition'", '--select', 'RowKey', '--query', 'items[].RowKey', '-o', 'tsv')
if ($r.code -ne 0) { throw "Table query failed: $($r.text)" }
foreach ($rk in ($r.text -split "`n" | Where-Object { $_ -match '^(INC|RITM)\d+$' })) {
    $d = Invoke-Az @('storage', 'entity', 'delete', '--account-name', $StorageAccount, '--table-name', $Table,
        '--partition-key', $Partition, '--row-key', $rk)
    if ($d.code -eq 0) { Write-Host "[del ] table row $rk" -ForegroundColor DarkYellow } else { Write-Warning "delete $rk failed: $($d.text)" }
}
Write-Host "`nDone. Tickets reappear in /itsm within ~5 min (poll) and get proposals ~5 min later (propose)." -ForegroundColor Cyan
