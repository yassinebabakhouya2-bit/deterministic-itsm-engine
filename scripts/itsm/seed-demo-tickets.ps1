<#
.SYNOPSIS
  Jalon 10 - creates the demo tickets (one per scenario) in the ServiceNow dev instance,
  from demo-tickets.json. Run AFTER seed-demo-identities.ps1 (callers must exist).

.DESCRIPTION
  - Incidents: table `incident`, caller = demo user, assignment group KE-Automation.
  - Requests: `sc_request` + `sc_req_item` created directly through the Table API
    (no catalog item / no catalog flow attached -> the module can close them via the
    Table API without a flow overriding the state; trade-off documented in runbook 11.3).
  - Idempotent: each ticket carries correlation_id = its key (KE-DEMO-...); existing keys are skipped.
  - -Reset deletes every ticket whose correlation_id starts with KE-DEMO- first
    (use before each demo rehearsal to start from a clean queue).

  Auth: an interactive-login user (admin) is blocked from basic-auth API calls on Zurich
  unless it temporarily holds the role snc_basic_auth_api_access (runbook 11.2).

  Source is ASCII only (runbook 9.1); ticket text with accents lives in demo-tickets.json.

.EXAMPLE
  .\scripts\itsm\seed-demo-tickets.ps1 -SnInstance dev374242
  .\scripts\itsm\seed-demo-tickets.ps1 -SnInstance dev374242 -Reset
#>
param(
    [string]$SnInstance = 'dev374242',
    [switch]$Reset
)
$ErrorActionPreference = 'Stop'
$tickets = Get-Content -Raw -Encoding UTF8 -Path (Join-Path $PSScriptRoot 'demo-tickets.json') | ConvertFrom-Json
$identities = Get-Content -Raw -Path (Join-Path $PSScriptRoot 'demo-identities.json') | ConvertFrom-Json

# ---------------------------------------------------------------- auth
$snCred = Get-Credential -UserName 'admin' -Message "ServiceNow admin for https://$SnInstance.service-now.com"
$snUser = $snCred.UserName.Trim().TrimStart('\')
$snPlain = $snCred.GetNetworkCredential().Password
Write-Host "User '$snUser', password length $($snPlain.Length)"
$basic = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes("${snUser}:$snPlain"))
$snPlain = $null
$snHeaders = @{ Authorization = "Basic $basic"; Accept = 'application/json' }
$snBase = "https://$SnInstance.service-now.com/api/now/table"
try {
    Invoke-RestMethod -Method GET -Uri "$snBase/sys_user?sysparm_limit=1" -Headers $snHeaders -UseBasicParsing | Out-Null
} catch {
    throw "ServiceNow authentication failed for '$snUser' on $SnInstance. If the password is right, the user needs the role snc_basic_auth_api_access (runbook 11.2). $_"
}
Write-Host "ServiceNow auth OK as $snUser" -ForegroundColor Green

function Invoke-Sn([string]$Method, [string]$Path, $Body) {
    $p = @{ Method = $Method; Uri = "$snBase/$Path"; Headers = $snHeaders; UseBasicParsing = $true }
    if ($null -ne $Body) {
        $p.Body = [System.Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 5))
        $p.ContentType = 'application/json; charset=utf-8'
    }
    try {
        $r = Invoke-RestMethod @p
    } catch {
        # Surface ServiceNow's JSON error body (PS 5.1 hides it behind "(500) Erreur interne")
        $detail = ''
        if ($_.Exception.Response) {
            $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream(), [System.Text.Encoding]::UTF8)
            $detail = $sr.ReadToEnd()
        }
        throw "ServiceNow $Method $Path failed: $($_.Exception.Message) $detail"
    }
    if ($r) { return $r.result }
}
function Get-SnAll([string]$Table, [string]$Query, [string]$Fields = 'sys_id') {
    if ($Table -in @('incident', 'sc_request', 'sc_req_item')) { $Fields = 'sys_id,number' }
    $q = [uri]::EscapeDataString($Query)
    return @(Invoke-Sn GET "$Table`?sysparm_query=$q&sysparm_fields=$Fields&sysparm_limit=200" $null)
}
function Get-SnFirst([string]$Table, [string]$Query) {
    # @() is required: in PS 5.1 a single [pscustomobject] has no .Count (returns $null)
    $r = @(Get-SnAll $Table $Query)
    if ($r.Count -gt 0 -and $r[0]) { return $r[0] }
    return $null
}

# ---------------------------------------------------------------- reset
if ($Reset) {
    foreach ($table in @('sc_req_item', 'sc_request', 'incident')) {
        foreach ($rec in (Get-SnAll $table 'correlation_idSTARTSWITHKE-DEMO-')) {
            if (-not $rec) { continue }
            Invoke-Sn DELETE "$table/$($rec.sys_id)" $null | Out-Null
            Write-Host "[del ] $table $($rec.number)" -ForegroundColor DarkYellow
        }
    }
}

# ---------------------------------------------------------------- lookups
$userSysId = @{}
foreach ($u in $identities.users) {
    $rec = Get-SnFirst 'sys_user' "user_name=$($u.nick)"
    if (-not $rec) { throw "ServiceNow user $($u.nick) not found - run seed-demo-identities.ps1 first." }
    $userSysId[$u.nick] = $rec.sys_id
}
$grp = Get-SnFirst 'sys_user_group' "name=$($identities.servicenow.assignmentGroup)"
if (-not $grp) { throw "Assignment group $($identities.servicenow.assignmentGroup) not found - run seed-demo-identities.ps1 first." }
$groupSysId = $grp.sys_id

$created = @()

# ---------------------------------------------------------------- incidents
foreach ($t in $tickets.incidents) {
    $existing = Get-SnFirst 'incident' "correlation_id=$($t.key)"
    if ($existing) { Write-Host "[skip] $($t.key) -> $($existing.number)"; $created += [pscustomobject]@{ key = $t.key; number = $existing.number }; continue }
    $r = Invoke-Sn POST 'incident' @{
        caller_id           = $userSysId[$t.caller]
        opened_by           = $userSysId[$t.caller]
        short_description   = $t.short_description
        description         = $t.description
        category            = $t.category
        impact              = $t.impact
        urgency             = $t.urgency
        contact_type        = 'self-service'
        assignment_group    = $groupSysId
        correlation_id      = $t.key
        correlation_display = 'KnowledgeEngine demo'
    }
    Write-Host "[new ] $($t.key) -> $($r.number)" -ForegroundColor Green
    $created += [pscustomobject]@{ key = $t.key; number = $r.number }
}

# ---------------------------------------------------------------- requests (REQ + RITM)
foreach ($t in $tickets.requests) {
    $existing = Get-SnFirst 'sc_req_item' "correlation_id=$($t.key)"
    if ($existing) { Write-Host "[skip] $($t.key) -> $($existing.number)"; $created += [pscustomobject]@{ key = $t.key; number = $existing.number }; continue }
    $req = Invoke-Sn POST 'sc_request' @{
        requested_for     = $userSysId[$t.requested_for]
        opened_by         = $userSysId[$t.opened_by]
        short_description = $t.short_description
        description       = $t.description
        approval          = 'approved'
        correlation_id    = $t.key
    }
    $ritm = Invoke-Sn POST 'sc_req_item' @{
        request           = $req.sys_id
        requested_for     = $userSysId[$t.requested_for]
        opened_by         = $userSysId[$t.opened_by]
        short_description = $t.short_description
        description       = $t.description
        assignment_group  = $groupSysId
        approval          = 'approved'
        state             = '1'
        correlation_id    = $t.key
    }
    Write-Host "[new ] $($t.key) -> $($req.number) / $($ritm.number)" -ForegroundColor Green
    $created += [pscustomobject]@{ key = $t.key; number = $ritm.number }
}

Write-Host ''
$created | Format-Table -AutoSize
Write-Host "Queue: https://$SnInstance.service-now.com/task_list.do?sysparm_query=correlation_idSTARTSWITHKE-DEMO-" -ForegroundColor Cyan
Write-Host 'Reminder: remove the role snc_basic_auth_api_access from admin when done.' -ForegroundColor Yellow
