<#
.SYNOPSIS
  One-shot bootstrap of KnowledgeEngine v9 on a brand-new Entra tenant + Azure subscription
  (runbook section 12).

  Everything the first tenant ran, rebuilt from this repo:
  - Foundation: Storage, AI Search, AI Foundry (gpt-4o, gpt-4o-enrich,
    text-embedding-3-large), enrichment Function, Web App + Easy Auth.
  - FOUR clients: clienta / clientb / clientc (synthetic KBs uploaded straight to Blob) and
    client-s (the real SFIT/DXC client, shared group "KE-v9-clients").
    A client can instead belong to another organization's Entra tenant (every user of that
    tenant = that client, no group): -ExternalTenantClients, kept on later runs.
  - SharePoint ingestion (runbook 0bis / 1 / 2): Key Vault, the multi-tenant app
    knowledgeengine-sharepoint-ingestion (Sites.Selected), one SharePoint site per client of
    -SharePointClients on THIS tenant (yours via -SharePointSiteUrls, else a new one), read
    access for the app on it, logic-ingest-<client>.
  - Audio (Jalon 7) and video (Jalon 8) pipelines for -MediaClients (Speech runs on the
    Foundry account itself; Video Indexer account + its Logic App).
  - ITSM module (Jalon 10) on ServiceNow -SnInstance: demo identities, poll / propose /
    execute Logic Apps, their Graph permissions, the agent review tab access.
  client-v (VINCI) is permanently abandoned and is never referenced by this script.

.DESCRIPTION
  Idempotent: every step checks what already exists. After a failure, fix the cause and
  re-run, optionally with -From <phase> to skip what is already done:
      prereqs -> entra -> infra -> function -> data -> search -> ingestion -> audio -> video -> itsm -> webapp

  ingestion / audio / video / itsm run by default; -SkipSharePoint, -SkipAudio, -SkipVideo,
  -SkipItsm leave them out. itsm is interactive: it asks for the ServiceNow svc_ke_itsm
  password (once, stored in Key Vault) and the ServiceNow admin credentials (seeding).

  infra leaves the AI Foundry account and its model deployments untouched once they exist
  and are Succeeded (re-applying them hit RequestConflict, then the Azure anti-abuse block
  715-123420 on a new subscription - runbook 12.4). -ForceFoundry re-applies them anyway.

  External tenants (runbook 6.6): -ExternalTenantClients @{ '<client>' = '<tenant id>' } makes
  entra write that tenant into the client's config without a group (no KE-v9-* group for
  it); infra then allows the tenant in Easy Auth (WEBSITE_AUTH_AAD_ALLOWED_TENANTS, issuer
  /organizations) and makes the App Registration multi-tenant. A config already pointing to
  another tenant without a group stays so on any later run; '' brings the client back into
  this tenant. An admin of the other tenant consents once (link in the final summary). On a
  running deployment, scripts\attach-external-tenant.ps1 does the same in a few minutes.

  Prerequisites (once):
      az login --tenant <new-tenant-id>          # a Global Administrator of the new tenant
      az account set --subscription <sub-id>     # that account must be Owner of the subscription

  Windows PowerShell 5.1 or PowerShell 7 (Cloud Shell). ASCII-only source (runbook 9.1).
  A transcript is written to bootstrap-<timestamp>.log at the repo root (git-ignored).

.EXAMPLE
  .\scripts\bootstrap-new-tenant.ps1
.EXAMPLE
  .\scripts\bootstrap-new-tenant.ps1 -From search
.EXAMPLE
  .\scripts\bootstrap-new-tenant.ps1 -From ingestion
.EXAMPLE
  .\scripts\bootstrap-new-tenant.ps1 -From ingestion -SkipItsm
.EXAMPLE
  .\scripts\bootstrap-new-tenant.ps1 -From ingestion -SharePointSiteUrls @{ 'client-s' = 'https://contoso.sharepoint.com/sites/ClientS' }
.EXAMPLE
  .\scripts\bootstrap-new-tenant.ps1 -ExternalTenantClients @{ 'clientc' = '<tenant-id-of-the-other-organization>' }
#>
param(
    [ValidateSet('prereqs', 'entra', 'infra', 'function', 'data', 'search', 'ingestion', 'audio', 'video', 'itsm', 'webapp')]
    [string]$From = 'prereqs',
    [string]$NamePrefix = 'knowledgeengine3',
    [string]$ResourceGroup = 'rg-knowledgeengine-v9',
    [string]$Location = 'francecentral',
    [string[]]$Clients = @('clienta', 'clientb', 'clientc', 'client-s'),
    [string]$AppDisplayName = 'KnowledgeEngineV9-WebApp-Auth',
    [string]$DemoUserAlias = 'ke-demo',
    # Clients whose users sign in from another organization's Entra tenant - every user of it,
    # no group - e.g. -ExternalTenantClients @{ 'clientc' = '<tenant id>' }. Without it, a
    # client config already pointing to another tenant without a group stays so; '' brings a
    # client back into this tenant (its KE-v9-* group).
    [hashtable]$ExternalTenantClients = @{},
    # Re-apply the Foundry account / project / model deployments even when they already exist
    # and are Succeeded (e.g. to change capacities). Off by default - see the infra phase.
    [switch]$ForceFoundry,
    # SharePoint ingestion: one new site on THIS tenant per client listed here.
    [string[]]$SharePointClients = @('client-s'),
    # Existing site to use for a client instead of creating one, e.g.
    #   -SharePointSiteUrls @{ 'client-s' = 'https://<tenant>.sharepoint.com/sites/ClientS' }
    [hashtable]$SharePointSiteUrls = @{},
    [string]$IngestionAppName = 'knowledgeengine-sharepoint-ingestion',
    [switch]$RotateIngestionSecret,
    [switch]$SkipSharePoint,
    # Audio (Jalon 7) / video (Jalon 8) pipelines for these clients.
    [string[]]$MediaClients = @('client-s'),
    [switch]$SkipAudio,
    [switch]$SkipVideo,
    # ITSM (Jalon 10).
    [string]$SnInstance = 'dev374242',
    [string]$SnUser = 'svc_ke_itsm',
    [switch]$SkipItsm,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$guidPattern = '^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$'

if ((@($Clients) + @($SharePointClients) + @($MediaClients)) -contains 'client-v') { throw "client-v (VINCI) is abandoned - remove it from the client lists." }
foreach ($k in $ExternalTenantClients.Keys) {
    if ($Clients -notcontains $k) { throw "-ExternalTenantClients: '$k' is not one of -Clients ($($Clients -join ', '))." }
    $v = "$($ExternalTenantClients[$k])".Trim()
    if ($v -and ($v -notmatch $guidPattern)) { throw "-ExternalTenantClients: '$v' ($k) is not a tenant ID (GUID)." }
}

$phases = @('prereqs', 'entra', 'infra', 'function', 'data', 'search', 'ingestion', 'audio', 'video', 'itsm', 'webapp')
$startAt = [array]::IndexOf($phases, $From)
$script:demoPassword = $null

# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------
function Test-Phase([string]$Name) { return ([array]::IndexOf($phases, $Name) -ge $startAt) }

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }

function ConvertTo-AzText($Out) {
    if ($null -eq $Out) { return '' }
    return (($Out | ForEach-Object { "$_" }) -join "`n").Trim()
}

function ConvertFrom-NativeOutput($Out) {
    # Text of a native command's merged output (2>&1): stderr lines arrive as ErrorRecords.
    return ((@($Out | ForEach-Object {
                    if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
                }) | Where-Object { $_ -and $_.Trim() }) -join "`n")
}

function Invoke-AzTransientRetry([string]$What, [string]$RetryPattern, [int]$Attempts, [int]$DelaySeconds, [string[]]$AzArgs) {
    # az with retries only on errors matching $RetryPattern; any other error fails at once.
    # Calls az directly because the decision needs the error text (Invoke-Az keeps it out
    # of its exception on purpose). Retries print one line; the full error only at the end.
    for ($i = 1; $i -le $Attempts; $i++) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { $out = & az @AzArgs 2>&1 } finally { $ErrorActionPreference = $prev }
        if ($LASTEXITCODE -eq 0) { return }
        $text = ConvertFrom-NativeOutput $out
        if (($text -notmatch $RetryPattern) -or ($i -ge $Attempts)) {
            Write-Host $text
            throw "$What failed (exit code $LASTEXITCODE) - see the az error above."
        }
        Write-Host "  $What - transient error (attempt $i/$Attempts), retrying in $DelaySeconds s..."
        Start-Sleep -Seconds $DelaySeconds
    }
}

function Invoke-Az {
    # Runs az and returns its trimmed stdout; throws on a non-zero exit code.
    # Arguments are never echoed in the error message (some of them carry secrets).
    # Never pass an argument containing ( ) | & < > ^ : az.cmd is a batch file on Windows.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & az @args } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0]) $($args[1]) failed (exit code $LASTEXITCODE) - see the az error above." }
    return (ConvertTo-AzText $out)
}

function Invoke-AzOptional {
    # Same as Invoke-Az but returns $null on failure, stderr hidden (existence checks).
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & az @args 2>$null } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { return $null }
    return (ConvertTo-AzText $out)
}

function Invoke-AzRetry {
    # az with retries: absorbs Entra replication and RBAC propagation delays.
    for ($i = 1; $i -le 5; $i++) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { $out = & az @args } finally { $ErrorActionPreference = $prev }
        if ($LASTEXITCODE -eq 0) { return (ConvertTo-AzText $out) }
        if ($i -lt 5) { Write-Host "  (propagation delay?) retrying in 30 s - attempt $($i + 1)/5"; Start-Sleep -Seconds 30 }
    }
    throw "az $($args[0]) $($args[1]) still failing after 5 attempts - see the az error above."
}

function Grant-MyRole([string]$Role, [bool]$Required) {
    $existing = Invoke-AzOptional role assignment list --assignee $me --role $Role --scope $rgScope --query '[].id' -o tsv
    if ($existing) { Write-Host "  role ok       : $Role"; return }
    $res = Invoke-AzOptional role assignment create --assignee-object-id $me --assignee-principal-type User --role $Role --scope $rgScope -o none
    if ($null -ne $res) { Write-Host "  role assigned : $Role"; return }
    if ($Required) { throw "Could not assign '$Role' to the signed-in user on $rgScope." }
    Write-Warning "Optional role '$Role' not assigned (only needed to log eval runs to Foundry)."
}

function Add-GroupMember([string]$GroupId, [string]$MemberId) {
    $is = Invoke-AzRetry ad group member check --group $GroupId --member-id $MemberId --query value -o tsv
    if ($is -ne 'true') { Invoke-AzRetry ad group member add --group $GroupId --member-id $MemberId | Out-Null }
}

function New-DemoPassword {
    # Letters, digits and '_' only: safe through az.cmd / cmd.exe argument parsing.
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'.ToCharArray()
    $bytes = New-Object byte[] 16
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $core = -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
    return "Ke9_$core"
}

function Find-ClientConfigPath([string]$Client) {
    # engine.<client>.yaml: synthetic clients (a/b/c) live in config/, real clients (client-s)
    # in the git-ignored clients-local/. $null when neither exists.
    foreach ($d in @('config', 'clients-local')) {
        $p = Join-Path (Join-Path $root $d) "engine.$Client.yaml"
        if (Test-Path $p) { return [System.IO.Path]::GetFullPath($p) }
    }
    return $null
}

function Get-ClientConfigPath([string]$Client) {
    $p = Find-ClientConfigPath $Client
    if (-not $p) { throw "Missing config/engine.$Client.yaml and clients-local/engine.$Client.yaml" }
    return $p
}

function Get-LocalClients {
    # -Clients whose config is in clients-local/ (real clients): the webapp phase ships their
    # engine.<client>.yaml, and nothing else from that folder.
    return @($Clients | Where-Object {
            (-not (Test-Path (Join-Path $root "config/engine.$_.yaml"))) -and (Test-Path (Join-Path $root "clients-local/engine.$_.yaml"))
        })
}

function Get-AccessBlock([string]$Path) {
    # access.entraTenantId (lower case, '' if absent) and whether an entraGroup key exists
    # (none = every user of that tenant, app/auth.py).
    $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $t = [regex]::Match($text, '(?m)^[ \t]*entraTenantId:[ \t]*"?([^"\s#]*)')
    $tid = ''
    if ($t.Success) { $tid = $t.Groups[1].Value.ToLower() }
    return [pscustomobject]@{ TenantId = $tid; HasGroup = [regex]::IsMatch($text, '(?m)^[ \t]*entraGroup:') }
}

function Get-ExternalTenant([string]$Client) {
    # Another organization's tenant whose users all get <client> (no group), or ''.
    # -ExternalTenantClients decides when it names the client ('' = this tenant). Otherwise a
    # config already pointing to another tenant WITHOUT a group stays external: that tenant
    # is not ours and does not change when this deployment moves. A config WITH a group always
    # follows this tenant (the entra phase rewrites it).
    if ($ExternalTenantClients.ContainsKey($Client)) {
        $t = "$($ExternalTenantClients[$Client])".Trim().ToLower()
        if ($t -eq $tenantId) { return '' }
        return $t
    }
    $p = Find-ClientConfigPath $Client
    if (-not $p) { return '' }
    $a = Get-AccessBlock $p
    if ((-not $a.HasGroup) -and ($a.TenantId -match $guidPattern) -and ($a.TenantId -ne $tenantId)) { return $a.TenantId }
    return ''
}

function Get-AllowedTenants {
    # Easy Auth's WEBSITE_AUTH_AAD_ALLOWED_TENANTS: this tenant + every tenant a client config
    # shipped with the app points to (config/engine.*.yaml, clients-local/engine.<client>.yaml
    # of -Clients), read after the entra phase wrote them. Microsoft caps it at 10.
    $paths = @(Get-ChildItem -Path (Join-Path $root 'config') -Filter 'engine.*.yaml' -File | ForEach-Object { $_.FullName })
    $paths += @(Get-LocalClients | ForEach-Object { Get-ClientConfigPath $_ })
    $ids = New-Object System.Collections.Generic.List[string]
    $ids.Add($tenantId.ToLower())
    foreach ($p in $paths) {
        $t = (Get-AccessBlock $p).TenantId
        if (($t -match $guidPattern) -and (-not $ids.Contains($t))) { $ids.Add($t) }
    }
    if ($ids.Count -gt 10) { throw "$($ids.Count) Entra tenants in the client configs: Easy Auth accepts at most 10." }
    return $ids.ToArray()
}

function Set-AppAudience([string]$AppId, [bool]$MultiTenant) {
    # A user of another tenant can only sign in to a multi-tenant App Registration (else Entra
    # itself answers AADSTS50020); back to single-tenant once no client config needs one.
    $want = 'AzureADMyOrg'
    if ($MultiTenant) { $want = 'AzureADMultipleOrgs' }
    $cur = Invoke-Az ad app show --id $AppId --query signInAudience -o tsv
    if ($cur -eq $want) { return }
    Invoke-AzRetry ad app update --id $AppId --sign-in-audience $want | Out-Null
    Write-Host "  app registration sign-in audience: $cur -> $want"
}

function Set-AccessBlock([string]$Client, [string]$TenantId, [string]$GroupId, [string]$GroupName) {
    Set-AccessBlockFile (Get-ClientConfigPath $Client) $TenantId $GroupId $GroupName
}

function Set-AccessBlockFile([string]$Path, [string]$TenantId, [string]$GroupId, [string]$GroupName) {
    # Rewrites access.entraTenantId / access.entraGroup of a YAML config (engine.<client>.yaml,
    # config/itsm.yaml); inserts entraGroup when missing (e.g. a client back from an external
    # tenant). $GroupId '' = another organization's tenant, all its users: the entraGroup line
    # is removed. UTF-8 without BOM, original line endings kept.
    $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $tenantLine = 'entraTenantId: "' + $TenantId + '"   # set by scripts/bootstrap-new-tenant.ps1'
    $groupLine = 'entraGroup: "' + $GroupId + '"   # ' + $GroupName + ', set by scripts/bootstrap-new-tenant.ps1'
    $patTenant = '(?m)^([ \t]*)entraTenantId:[^\r\n]*'
    $patGroup = '(?m)^([ \t]*)entraGroup:[^\r\n]*'
    if (-not [regex]::IsMatch($text, $patTenant)) { throw "No access.entraTenantId line in $path" }
    if (-not $GroupId) {
        $tenantLine = 'entraTenantId: "' + $TenantId + '"   # external tenant, whole tenant = this client (no entraGroup), set by scripts/bootstrap-new-tenant.ps1'
        $text = [regex]::Replace($text, $patTenant, ('${1}' + $tenantLine))
        $text = [regex]::Replace($text, '(?m)^[ \t]*entraGroup:[^\r\n]*(\r?\n)?', '')
        [System.IO.File]::WriteAllText($path, $text, $utf8NoBom)
        Write-Host "  $($path.Substring($root.Length + 1)) -> external tenant $TenantId (all its users, no group)"
        return
    }
    $text = [regex]::Replace($text, $patTenant, ('${1}' + $tenantLine))
    if ([regex]::IsMatch($text, $patGroup)) {
        $text = [regex]::Replace($text, $patGroup, ('${1}' + $groupLine))
    } else {
        $nl = "`n"
        if ($text.Contains("`r`n")) { $nl = "`r`n" }
        $text = [regex]::Replace($text, $patTenant, ('$0' + $nl + '${1}' + $groupLine))
    }
    [System.IO.File]::WriteAllText($path, $text, $utf8NoBom)
    $rel = $path.Substring($root.Length + 1)
    Write-Host "  $rel -> this tenant, group $GroupName"
}

function Confirm-Container([string]$Name) {
    # Idempotent: an existing container is left as is.
    Invoke-AzRetry storage container create --account-name $st --name $Name --auth-mode login -o none | Out-Null
}

function Confirm-KbContainer([string]$Client) {
    # kb-<client> must exist before an upload into it AND before the client's search pipeline:
    # an indexer on a missing container is refused with a misleading "Unable to retrieve blob
    # container ... using your managed identity" (client-s, 2026-09-26). storage.bicep only
    # creates kb-clienta/b/c; on the first tenant kb-client-s came from the SharePoint
    # ingestion.
    Confirm-Container "kb-$Client"
}

function Invoke-Graph([string]$Method, [string]$Path, $Body) {
    # Microsoft Graph with the signed-in az session (delegated). Never put '&' in $Path:
    # az.cmd is a batch file and cmd.exe would split the command there.
    $azArgs = @('rest', '--method', $Method, '--url', "https://graph.microsoft.com/v1.0$Path")
    $tmp = $null
    if ($null -ne $Body) {
        $tmp = [System.IO.Path]::GetTempFileName()
        [System.IO.File]::WriteAllText($tmp, ($Body | ConvertTo-Json -Depth 8), $utf8NoBom)
        $azArgs += @('--body', "@$tmp", '--headers', 'Content-Type=application/json')
    }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & az @azArgs 2>&1; $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $prev; if ($tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue } }
    if ($code -ne 0) { throw "Graph $Method $Path failed: $(ConvertFrom-NativeOutput $out)" }
    $stdout = ((@($out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) | ForEach-Object { "$_" }) -join "`n").Trim()
    if ($stdout) { return ($stdout | ConvertFrom-Json) }
    return $null
}

function Invoke-BicepDeploy([string]$Name, [string]$Template, [hashtable]$Parameters) {
    # Parameters go through a temporary parameters file, never the command line: values such
    # as a SharePoint site id (commas) or a secret must not cross az.cmd / cmd.exe parsing.
    $tpl = Join-Path $root $Template
    if (-not (Test-Path $tpl)) { throw "Missing $Template" }
    $p = [ordered]@{
        '$schema'      = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
        contentVersion = '1.0.0.0'
        parameters     = [ordered]@{}
    }
    foreach ($k in $Parameters.Keys) { $p.parameters[$k] = @{ value = $Parameters[$k] } }
    $file = Join-Path ([System.IO.Path]::GetTempPath()) ('ke-' + $Name + '-' + [guid]::NewGuid().ToString('N') + '.json')
    [System.IO.File]::WriteAllText($file, ($p | ConvertTo-Json -Depth 8), $utf8NoBom)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & az deployment group create --name $Name --resource-group $ResourceGroup --template-file $tpl --parameters "@$file" --query properties.outputs -o json 2>&1
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prev; Remove-Item $file -Force -ErrorAction SilentlyContinue }
    if ($code -ne 0) {
        Write-Host (ConvertFrom-NativeOutput $out)
        throw "Deployment '$Name' ($Template) failed - see the az error above."
    }
    $stdout = ((@($out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) | ForEach-Object { "$_" }) -join "`n").Trim()
    if ($stdout) { return ($stdout | ConvertFrom-Json) }
    return $null
}

function Confirm-KeyVault {
    # kv-<prefix>-v9: secrets of the ingestion app, Speech, ServiceNow. Not in infra/main.bicep
    # (on the first tenant it was created by hand, runbook 0). RBAC mode; your Key Vault
    # Secrets Officer role comes from the resource group (prereqs phase).
    if (Invoke-AzOptional keyvault show --name $kv --resource-group $ResourceGroup --query id -o tsv) { return }
    Write-Host "  creating Key Vault $kv (RBAC authorization)"
    Invoke-Az keyvault create --name $kv --resource-group $ResourceGroup --location $Location --enable-rbac-authorization true -o none | Out-Null
}

function Test-KvSecret([string]$Name) {
    return [bool](Invoke-AzOptional keyvault secret show --vault-name $kv --name $Name --query id -o tsv)
}

function Set-KvSecret([string]$Name, [string]$Value) {
    # Through a temporary file: the value never appears on a command line or in the transcript.
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($tmp, $Value, $utf8NoBom)
        Invoke-AzRetry keyvault secret set --vault-name $kv --name $Name --file $tmp --encoding utf-8 --query id -o tsv | Out-Null
    } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}

function Confirm-EntraGroup([string]$Name) {
    $gid = (Invoke-Az ad group list --display-name $Name --query '[].id' -o tsv) -split "`n" | Where-Object { $_ } | Select-Object -First 1
    if ($gid) { Write-Host "  group reused  : $Name"; return $gid.Trim() }
    $gid = Invoke-Az ad group create --display-name $Name --mail-nickname $Name.ToLower() --query id -o tsv
    Write-Host "  group created : $Name"
    return $gid
}

function Get-AppToken([string]$AppId, [string]$Secret) {
    # Client-credentials token for Microsoft Graph (app-only).
    $body = @{ client_id = $AppId; client_secret = $Secret; grant_type = 'client_credentials'; scope = 'https://graph.microsoft.com/.default' }
    return (Invoke-RestMethod -Method Post -Uri "https://login.microsoftonline.com/$tenantId/oauth2/v2.0/token" -Body $body -ContentType 'application/x-www-form-urlencoded' -UseBasicParsing).access_token
}

function Invoke-GraphBearer([string]$Token, [string]$Method, [string]$Path, $Body) {
    $p = @{ Method = $Method; Uri = "https://graph.microsoft.com/v1.0$Path"; Headers = @{ Authorization = "Bearer $Token" }; UseBasicParsing = $true }
    if ($null -ne $Body) {
        $p.Body = [System.Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 8))
        $p.ContentType = 'application/json'
    }
    return (Invoke-RestMethod @p)
}

function Get-FileSha256([byte[]]$Bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($Bytes)) } finally { $sha.Dispose() }
}

function Test-ZipMatchesSource([string]$ZipPath) {
    # True when the vendored package carries exactly the current enrichment/ sources.
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        foreach ($name in @('function_app.py', 'requirements.txt', 'host.json')) {
            $entry = $zip.GetEntry($name)
            if ($null -eq $entry) { return $false }
            $ms = New-Object System.IO.MemoryStream
            $s = $entry.Open()
            try { $s.CopyTo($ms) } finally { $s.Dispose() }
            $inZip = Get-FileSha256 $ms.ToArray()
            $onDisk = Get-FileSha256 ([System.IO.File]::ReadAllBytes((Join-Path $root "enrichment/$name")))
            if ($inZip -ne $onDisk) { Write-Warning "$name in $(Split-Path -Leaf $ZipPath) differs from enrichment/$name"; return $false }
        }
        return $true
    } finally { $zip.Dispose() }
}

function New-ZipFromFiles([string]$ZipPath, [string]$Dir, [string[]]$Names) {
    # Entry-by-entry with '/' names (Compress-Archive writes '\', unreadable on Linux App Service).
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
    $zip = [System.IO.Compression.ZipFile]::Open($ZipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($n in $Names) {
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, (Join-Path $Dir $n), $n) | Out-Null
        }
    } finally { $zip.Dispose() }
}

# ---------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------
Push-Location $root
$logFile = Join-Path $root ("bootstrap-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
$transcribing = $false
try { Start-Transcript -Path $logFile | Out-Null; $transcribing = $true } catch { Write-Warning "No transcript: $($_.Exception.Message)" }
try {
    Write-Step 'Preflight'
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Azure CLI (az) not found in PATH.' }
    foreach ($f in @('infra/main.bicep', 'search/deploy.ps1', 'search/generate-synonyms.ps1', 'deploy-webapp.ps1', 'orchestration/answer.py')) {
        if (-not (Test-Path (Join-Path $root $f))) { throw "Missing $f - run this script from the repo (scripts/)." }
    }
    $answerPy = [System.IO.File]::ReadAllText((Join-Path $root 'orchestration/answer.py'))
    if (-not $answerPy.Contains("srch-$NamePrefix-v9")) {
        throw "orchestration/answer.py does not point to srch-$NamePrefix-v9: code and -NamePrefix disagree (see runbook 12.2)."
    }

    $st = "st$($NamePrefix)v9"
    $srch = "srch-$NamePrefix-v9"
    $web = "app-$NamePrefix-v9"
    $fn = "fn-$NamePrefix-v9"
    $kv = "kv-$NamePrefix-v9"
    $foundryAcct = "aif-$NamePrefix-v9"
    if ($st.Length -gt 24) { throw "Storage account name '$st' is longer than 24 characters - shorten -NamePrefix." }
    if ($kv.Length -gt 24) { throw "Key Vault name '$kv' is longer than 24 characters - shorten -NamePrefix." }
    $spSites = @{}   # client -> SharePoint site web URL, for the final summary

    $acct = (Invoke-Az account show -o json) | ConvertFrom-Json
    $tenantId = $acct.tenantId
    $subId = $acct.id
    $rgScope = "/subscriptions/$subId/resourceGroups/$ResourceGroup"
    $me = Invoke-Az ad signed-in-user show --query id -o tsv
    $domain = (Invoke-Az rest --method get --url 'https://graph.microsoft.com/v1.0/domains' --query 'value[?isInitial].id' -o tsv) -split "`n" | Select-Object -First 1
    $demoUpn = "$DemoUserAlias@$domain"

    # Clients of another organization's tenant (all its users, no group) - Get-ExternalTenant.
    $externalTenants = [ordered]@{}
    foreach ($c in $Clients) {
        $t = Get-ExternalTenant $c
        if (-not $t) { continue }
        foreach ($k in @($externalTenants.Keys)) {
            if ($externalTenants[$k] -eq $t) { throw "$k and $c would both get every user of tenant $t - app/auth.py allows one client per external tenant." }
        }
        $externalTenants[$c] = $t
    }

    Write-Host "  Tenant       : $tenantId ($domain)"
    Write-Host "  Subscription : $($acct.name) ($subId)"
    Write-Host "  Signed in as : $($acct.user.name)"
    Write-Host "  Target       : $ResourceGroup, $Location, prefix $NamePrefix, clients $($Clients -join ', '), from phase '$From'"
    foreach ($c in $externalTenants.Keys) { Write-Host "  External     : $c <- every user of tenant $($externalTenants[$c]) (no group)" }
    if (-not $Yes) {
        $answer = Read-Host 'Deploy into THIS tenant and subscription? (y/N)'
        if ($answer -notin @('y', 'Y', 'yes', 'o', 'O', 'oui')) { throw 'Aborted.' }
    }

    # -----------------------------------------------------------------------------------
    if (Test-Phase 'prereqs') {
        Write-Step 'Resource providers, resource group, your data-plane roles'
        $providers = @('Microsoft.Storage', 'Microsoft.Search', 'Microsoft.CognitiveServices', 'Microsoft.Web',
            'Microsoft.OperationalInsights', 'Microsoft.Insights', 'Microsoft.AlertsManagement',
            'Microsoft.KeyVault', 'Microsoft.Logic', 'Microsoft.VideoIndexer')
        foreach ($p in $providers) { Invoke-Az provider register --namespace $p | Out-Null }
        $deadline = (Get-Date).AddMinutes(15)
        do {
            $pending = @($providers | Where-Object { (Invoke-Az provider show --namespace $_ --query registrationState -o tsv) -ne 'Registered' })
            if ($pending.Count -eq 0) { break }
            Write-Host "  waiting for provider registration: $($pending -join ', ')"
            Start-Sleep -Seconds 20
        } while ((Get-Date) -lt $deadline)
        if ($pending.Count -gt 0) { throw "Providers still not registered after 15 min: $($pending -join ', ')" }
        Write-Host '  providers registered'

        Invoke-Az group create --name $ResourceGroup --location $Location -o none | Out-Null
        Write-Host "  resource group $ResourceGroup ok"

        # Owner carries no data-plane rights: upload, local answer.py / eval runs, tables, Key Vault.
        foreach ($r in @('Storage Blob Data Contributor', 'Storage Table Data Contributor',
                'Search Index Data Contributor', 'Cognitive Services OpenAI User', 'Key Vault Secrets Officer')) {
            Grant-MyRole $r $true
        }
        Grant-MyRole 'Azure AI User' $false
    }

    # -----------------------------------------------------------------------------------
    if (Test-Phase 'entra') {
        Write-Step 'Entra ID: Easy Auth app registration, groups, demo user, client configs'
        $redirect = "https://$web.azurewebsites.net/.auth/login/aad/callback"
        $appId = Invoke-Az ad app list --display-name $AppDisplayName --query '[0].appId' -o tsv
        if (-not $appId) {
            $appId = Invoke-Az ad app create --display-name $AppDisplayName --sign-in-audience AzureADMyOrg --web-redirect-uris $redirect --enable-id-token-issuance true --query appId -o tsv
            Write-Host "  app registration created : $appId"
        } else {
            Invoke-Az ad app update --id $appId --web-redirect-uris $redirect --enable-id-token-issuance true | Out-Null
            Write-Host "  app registration reused  : $appId"
        }
        Invoke-AzRetry ad app update --id $appId --set groupMembershipClaims=SecurityGroup | Out-Null
        if ($null -eq (Invoke-AzOptional ad sp show --id $appId --query id -o tsv)) { Invoke-AzRetry ad sp create --id $appId -o none | Out-Null }

        # Tenant-wide consent for the sign-in scopes, so demo users never see a consent prompt.
        $graph = '00000003-0000-0000-c000-000000000000'
        $added = Invoke-AzOptional ad app permission add --id $appId --api $graph --api-permissions `
            e1fe6dd8-ba31-4d61-89e7-88639da4683d=Scope 37f7f235-527c-4136-accd-4a02d197296e=Scope `
            14dad69e-099b-42c9-810b-d002981feec1=Scope 64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0=Scope `
            7427e0e9-2fba-42fe-b0c0-848c9e6a8182=Scope
        $granted = Invoke-AzOptional ad app permission grant --id $appId --api $graph --scope 'openid profile email offline_access User.Read'
        if ($null -eq $added -or $null -eq $granted) {
            Write-Warning 'Tenant-wide consent not confirmed: at first sign-in, accept the consent prompt (as admin: "consent on behalf of your organization").'
        }

        # Native demo user (reliable sign-in whatever kind of account created the tenant).
        $newPassword = New-DemoPassword
        $demoId = Invoke-AzOptional ad user show --id $demoUpn --query id -o tsv
        if (-not $demoId) {
            $demoId = Invoke-Az ad user create --display-name 'KE Demo' --user-principal-name $demoUpn --password $newPassword --force-change-password-next-sign-in false --query id -o tsv
            Write-Host "  demo user created : $demoUpn"
        } else {
            Invoke-AzRetry ad user update --id $demoUpn --password $newPassword --force-change-password-next-sign-in false | Out-Null
            Write-Host "  demo user reused  : $demoUpn (password reset)"
        }
        $script:demoPassword = $newPassword

        foreach ($c in $Clients) {
            if ($externalTenants.Contains($c)) {
                # Another organization's tenant: all its users, no group in this tenant.
                Set-AccessBlock $c $externalTenants[$c] '' ''
                continue
            }
            # client-s uses a shared group across the real-client family (config/engine.client-s.yaml);
            # every other client gets its own KE-v9-<client> group.
            $gName = if ($c -eq 'client-s') { 'KE-v9-clients' } else { "KE-v9-$c" }
            $nick = $gName.ToLower()
            $gid = (Invoke-Az ad group list --display-name $gName --query '[].id' -o tsv) -split "`n" | Where-Object { $_ } | Select-Object -First 1
            if (-not $gid) {
                $gid = Invoke-Az ad group create --display-name $gName --mail-nickname $nick --query id -o tsv
                Write-Host "  group created : $gName"
            } else {
                Write-Host "  group reused  : $gName"
            }
            Add-GroupMember $gid $me
            Add-GroupMember $gid $demoId
            Set-AccessBlock $c $tenantId $gid $gName
        }
    }

    # -----------------------------------------------------------------------------------
    if (Test-Phase 'infra') {
        Write-Step 'Foundation (infra/main.bicep): Storage, AI Search, AI Foundry + models, Function, Web App, RBAC'
        $appId = Invoke-Az ad app list --display-name $AppDisplayName --query '[0].appId' -o tsv
        if (-not $appId) { throw "App registration '$AppDisplayName' not found - run with -From entra." }

        # Foundry account + model deployments. Re-applying them when they already exist is not a
        # no-op for Cognitive Services: every run PUTs the account again, and on a new subscription
        # a series of those hit RequestConflict, then the anti-abuse block 715-123420 ("unusual
        # activity", runbook 12.4). So main.bicep deploys them (deployFoundry) only when something
        # is missing, or with -ForceFoundry. Everything else in main.bicep is re-applied as usual.
        $foundryProj = "proj-$NamePrefix-v9"
        $gen = 30; $enr = 20; $emb = 120
        $deployFoundry = $true
        $deps = @()
        $acctState = Invoke-AzOptional cognitiveservices account show --resource-group $ResourceGroup --name $foundryAcct --query properties.provisioningState -o tsv
        if ($acctState) {
            $depJson = Invoke-AzOptional cognitiveservices account deployment list --resource-group $ResourceGroup --name $foundryAcct -o json
            # Assign first, then wrap: Windows PowerShell 5.1's ConvertFrom-Json emits a JSON
            # array as ONE object, so @(... | ConvertFrom-Json) would nest it there.
            if ($depJson) { $parsedDeps = $depJson | ConvertFrom-Json; $deps = @($parsedDeps) }
            $notReady = @(foreach ($n in @('gpt-4o', 'text-embedding-3-large', 'gpt-4o-enrich')) {
                    $d = @($deps | Where-Object { $_.name -eq $n }) | Select-Object -First 1
                    if ((-not $d) -or ($d.properties.provisioningState -ne 'Succeeded')) { $n }
                })
            if (($acctState -eq 'Succeeded') -and ($notReady.Count -eq 0) -and (-not $ForceFoundry)) {
                $deployFoundry = $false
                Write-Host "  $foundryAcct + gpt-4o, text-embedding-3-large, gpt-4o-enrich already Succeeded: left untouched"
                $projUrl = "https://management.azure.com$rgScope/providers/Microsoft.CognitiveServices/accounts/$foundryAcct/projects/$($foundryProj)?api-version=2025-04-01-preview"
                $projState = Invoke-AzOptional rest --method get --url $projUrl --query properties.provisioningState -o tsv
                if ($projState -ne 'Succeeded') {
                    Write-Warning "Foundry project $foundryProj not found - only eval/evaluate_rag.py uses it (eval runs logged to Foundry), not the app. Add it later with -From infra -ForceFoundry, once Cognitive Services accepts writes again."
                }
            } else {
                $why = '-ForceFoundry'
                if ($notReady.Count -gt 0) { $why = "not ready: $($notReady -join ', ')" }
                Write-Host "  $foundryAcct is $acctState ($why) - re-applying the Foundry module"
            }
        }

        if ($deployFoundry) {
            # Fit the model capacities to the subscription's quota instead of failing the deploy.
            # Capacity this account's own deployments already hold is not "used up" for a
            # re-deploy of the same account (Bicep re-applies the same value): add it back.
            $ownGen = 0; $ownEnr = 0; $ownEmb = 0
            foreach ($d in $deps) {
                $cap = 0
                if ($d.sku -and $d.sku.capacity) { $cap = [int]$d.sku.capacity }
                switch ($d.name) {
                    'gpt-4o' { $ownGen = $cap }
                    'gpt-4o-enrich' { $ownEnr = $cap }
                    'text-embedding-3-large' { $ownEmb = $cap }
                }
            }
            if ($ownGen -or $ownEnr -or $ownEmb) {
                Write-Host "  $foundryAcct already holds: gpt-4o $ownGen, gpt-4o-enrich $ownEnr, embedding $ownEmb K TPM (reclaimed by the re-deploy, not new quota)"
            }
            $usageJson = Invoke-AzOptional cognitiveservices usage list --location $Location -o json
            if ($usageJson) {
                $usage = $usageJson | ConvertFrom-Json
                $q4o = @($usage | Where-Object { $_.name.value -eq 'OpenAI.Standard.gpt-4o' }) | Select-Object -First 1
                $qEmb = @($usage | Where-Object { $_.name.value -eq 'OpenAI.Standard.text-embedding-3-large' }) | Select-Object -First 1
                if ($q4o) {
                    $free = [int]($q4o.limit - $q4o.currentValue) + $ownGen + $ownEnr
                    Write-Host "  gpt-4o Standard quota free in ${Location}: $free K TPM (wanted $($gen + $enr))"
                    if ($free -lt 2) { throw "No gpt-4o Standard quota left in $Location - request quota (Foundry portal > Quotas) or use another -Location." }
                    if ($free -lt ($gen + $enr)) { $enr = [int][Math]::Max(1, [Math]::Floor($free * 0.4)); $gen = $free - $enr }
                } else {
                    Write-Warning "gpt-4o Standard quota not listed for $Location - deploying with 30 + 20 K TPM."
                }
                if ($qEmb) {
                    $freeE = [int]($qEmb.limit - $qEmb.currentValue) + $ownEmb
                    Write-Host "  text-embedding-3-large Standard quota free: $freeE K TPM (wanted $emb)"
                    if ($freeE -lt 1) { throw "No text-embedding-3-large Standard quota left in $Location." }
                    if ($freeE -lt $emb) { $emb = $freeE }
                }
            }
            Write-Host "  capacities: gpt-4o $gen, gpt-4o-enrich $enr, text-embedding-3-large $emb (K TPM)"
        }

        # Sign-in tenants: this one + the tenant of every external-tenant client config. More than
        # one = multi-tenant App Registration + /organizations issuer (webapp.bicep); the
        # allowlist is enforced by the platform before the app code runs.
        $allowedTenants = @(Get-AllowedTenants)
        $multiTenant = ($allowedTenants.Count -gt 1)
        Set-AppAudience $appId $multiTenant
        Write-Host "  sign-in tenants: $($allowedTenants -join ', ')"

        $secret = Invoke-Az ad app credential reset --id $appId --display-name easyauth --years 1 --append --query password -o tsv
        $params = [ordered]@{
            '$schema'      = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
            contentVersion = '1.0.0.0'
            parameters     = [ordered]@{
                namePrefix               = @{ value = $NamePrefix }
                location                 = @{ value = $Location }
                enableEasyAuth           = @{ value = $true }
                easyAuthClientId         = @{ value = $appId }
                easyAuthTenantId         = @{ value = $tenantId }
                easyAuthClientSecret     = @{ value = $secret }
                easyAuthMultiTenant      = @{ value = $multiTenant }
                easyAuthAllowedTenantIds = @{ value = $allowedTenants }
                generationCapacity       = @{ value = $gen }
                enrichCapacity           = @{ value = $enr }
                embeddingCapacity        = @{ value = $emb }
                deployFoundry            = @{ value = $deployFoundry }
            }
        }
        $paramsFile = Join-Path ([System.IO.Path]::GetTempPath()) ('ke-main-params-' + [guid]::NewGuid().ToString('N') + '.json')
        [System.IO.File]::WriteAllText($paramsFile, ($params | ConvertTo-Json -Depth 8), $utf8NoBom)
        try {
            Write-Host '  deploying, 10-20 min...'
            # az is called directly (not via Invoke-Az) because the decision below needs the
            # actual error text, which Invoke-Az deliberately keeps out of its exception.
            # RequestConflict right after the Foundry account is created is retried a little;
            # the anti-abuse block (715-123420) is never retried - retries are what feed it.
            $attempts = 3
            for ($i = 1; $i -le $attempts; $i++) {
                $prevEAP = $ErrorActionPreference
                $ErrorActionPreference = 'Continue'
                try { $out = & az deployment group create --name main --resource-group $ResourceGroup --template-file (Join-Path $root 'infra/main.bicep') --parameters "@$paramsFile" -o none 2>&1 } finally { $ErrorActionPreference = $prevEAP }
                if ($LASTEXITCODE -eq 0) { break }
                $outText = ConvertFrom-NativeOutput $out
                if ($outText -match '715-123420|unusual activity') {
                    Write-Host $outText
                    throw "Azure anti-abuse block on Cognitive Services (715-123420). Do not retry in a loop. If $foundryAcct and its 3 deployments exist, re-run -From infra (they are then left untouched); otherwise wait a few hours, retry ONCE, then open an Azure support ticket (runbook 12.4)."
                }
                if (($outText -notmatch 'RequestConflict|[Aa]nother operation is in progress') -or ($i -ge $attempts)) {
                    Write-Host $outText
                    throw "az deployment group create (main) failed (exit code $LASTEXITCODE) - see the az error above."
                }
                Write-Host "  Foundry account busy (attempt $i/$attempts) - waiting 120 s before retrying..."
                Start-Sleep -Seconds 120
            }
        } finally {
            Remove-Item $paramsFile -Force -ErrorAction SilentlyContinue
        }
        Write-Host '  foundation deployed'

        # The platform may hand out a different default host name: keep the redirect URI in sync.
        $webHost = Invoke-Az webapp show --resource-group $ResourceGroup --name $web --query defaultHostName -o tsv
        $callback = "https://$webHost/.auth/login/aad/callback"
        $uris = (Invoke-Az ad app show --id $appId --query 'web.redirectUris' -o tsv) -split '\s+'
        if ($uris -notcontains $callback) {
            Invoke-Az ad app update --id $appId --web-redirect-uris $callback | Out-Null
            Write-Host "  redirect URI set to $callback"
        }
    }

    # -----------------------------------------------------------------------------------
    if (Test-Phase 'function') {
        Write-Step "Enrichment function code ($fn)"
        $pkg = Get-ChildItem -Path $root -Filter 'enrichment-vendored*.zip' | Where-Object { $_.Length -gt 0 } |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($pkg -and (Test-ZipMatchesSource $pkg.FullName)) {
            Write-Host "  vendored package (no remote build): $($pkg.Name)"
            $zipArgs = @('functionapp', 'deployment', 'source', 'config-zip', '--resource-group', $ResourceGroup, '--name', $fn, '--src', $pkg.FullName, '-o', 'none')
        } else {
            $srcZip = Join-Path ([System.IO.Path]::GetTempPath()) 'ke-enrichment-src.zip'
            New-ZipFromFiles $srcZip (Join-Path $root 'enrichment') @('function_app.py', 'host.json', 'requirements.txt')
            Write-Host '  source package with remote build'
            $zipArgs = @('functionapp', 'deployment', 'source', 'config-zip', '--resource-group', $ResourceGroup, '--name', $fn, '--src', $srcZip, '--build-remote', 'true', '-o', 'none')
        }
        # A Function App created minutes earlier has a cold SCM (Kudu) site: az gives up after a
        # 30 s read timeout while it waits for SCM to see SCM_DO_BUILD_DURING_DEPLOYMENT (seen
        # 2026-09-26, nothing uploaded yet). The setting is in place by the next attempt, so the
        # retry goes straight to the upload.
        Invoke-AzTransientRetry 'az functionapp deployment source config-zip' 'timed out|[Tt]imeout|ConnectionError|Connection aborted|RemoteDisconnected|50[234]|Bad Gateway|Service Unavailable' 4 30 $zipArgs
        $deadline = (Get-Date).AddMinutes(10)
        $fnKey = $null
        do {
            $fnKey = Invoke-AzOptional functionapp keys list --resource-group $ResourceGroup --name $fn --query functionKeys.default -o tsv
            if ($fnKey) { break }
            Write-Host '  waiting for the function host (keys)...'
            Start-Sleep -Seconds 20
        } while ((Get-Date) -lt $deadline)
        if (-not $fnKey) { throw "Function $fn has no default key after 10 min - check Portal > $fn > Log stream." }
        Write-Host '  function host up'
    }

    # -----------------------------------------------------------------------------------
    if (Test-Phase 'data') {
        Write-Step "Demo knowledge bases -> $st"
        foreach ($c in $Clients) {
            Confirm-KbContainer $c
            $src = Join-Path $root "kb/$c"
            if (-not (Test-Path $src)) {
                if ($c -eq 'client-s') {
                    # Real client: no synthetic kb/client-s folder is expected. Either a local
                    # export exists at kb/client-s (upload it like the others - fastest path), or
                    # the content is brought in live via SharePoint in the 'ingestion' phase below.
                    Write-Warning "kb/client-s not found - skipping direct upload. Either export client-s's real KB to kb/client-s and re-run -From data, or run -From ingestion with -SharePointSiteId to pull it live from SharePoint."
                    continue
                }
                throw "Missing $src"
            }
            Invoke-AzRetry storage blob upload-batch --account-name $st --destination "kb-$c" --source $src --metadata "clientid=$c" --auth-mode login --overwrite true -o none | Out-Null
            Write-Host "  kb/$c -> kb-$c"
        }
        $multi = Join-Path $root 'kb/clientc-multiformat'
        if (($Clients -contains 'clientc') -and (Test-Path $multi)) {
            # Jalon 6 multi-format set, uploaded directly instead of via SharePoint + Logic App.
            Invoke-AzRetry storage blob upload-batch --account-name $st --destination kb-clientc --source $multi --metadata clientid=clientc --auth-mode login --overwrite true -o none | Out-Null
            Write-Host '  kb/clientc-multiformat -> kb-clientc'
        }
    }

    # -----------------------------------------------------------------------------------
    if (Test-Phase 'search') {
        Write-Step "Search pipelines on $srch"
        foreach ($c in $Clients) {
            Confirm-KbContainer $c
            & (Join-Path $root 'search/deploy.ps1') -ClientId $c -Service $srch -ResourceGroup $ResourceGroup -StorageAccount $st -FunctionApp $fn -SubscriptionId $subId
        }

        $key = Invoke-Az search admin-key show --service-name $srch --resource-group $ResourceGroup --query primaryKey -o tsv
        $hdr = @{ 'api-key' = $key }
        $names = foreach ($c in $Clients) { "ix-$c-di"; "ix-$c-text" }
        $done = @{}
        $deadline = (Get-Date).AddMinutes(25)
        Write-Host '  waiting for the first indexer runs...'
        while ($done.Count -lt $names.Count -and (Get-Date) -lt $deadline) {
            foreach ($n in $names) {
                if ($done.ContainsKey($n)) { continue }
                $s = Invoke-RestMethod -Method Get -Uri "https://$srch.search.windows.net/indexers/$n/status?api-version=2024-07-01" -Headers $hdr
                $lr = $s.lastResult
                if ($null -eq $lr -or $lr.status -eq 'inProgress') { continue }
                $done[$n] = $true
                $msg = "  $n : $($lr.status), $($lr.itemsProcessed) processed, $($lr.itemsFailed) failed"
                if ($lr.status -eq 'success' -and [int]$lr.itemsFailed -eq 0) {
                    Write-Host $msg
                } else {
                    Write-Warning "$msg $($lr.errorMessage)"
                    foreach ($e in @($lr.errors | Select-Object -First 3)) { if ($e) { Write-Warning "    $($e.errorMessage)" } }
                }
            }
            if ($done.Count -lt $names.Count) { Start-Sleep -Seconds 20 }
        }
        if ($done.Count -lt $names.Count) { Write-Warning 'Some indexers are still running - check them in the portal before a demo.' }

        # Shared synonym map (ranking gain only, never blocking).
        try {
            & (Join-Path $root 'search/generate-synonyms.ps1') -Service $srch -ResourceGroup $ResourceGroup
        } catch {
            Write-Warning "generate-synonyms.ps1 failed (non-blocking): $($_.Exception.Message)"
        }
    }

    # -----------------------------------------------------------------------------------
    if (Test-Phase 'ingestion') {
        if ($SkipSharePoint -or -not $SharePointClients) {
            Write-Step 'SharePoint ingestion (skipped: -SkipSharePoint or empty -SharePointClients)'
        } else {
            Write-Step "SharePoint ingestion on this tenant: $($SharePointClients -join ', ') (runbook 0bis / 1 / 2)"
            Confirm-KeyVault

            # 1. Ingestion app (runbook 0bis.1): multi-tenant, nativeclient redirect URI, Graph
            #    application permission Sites.Selected (consent = app role assignment), secret in
            #    Key Vault only.
            $ingAppId = Invoke-Az ad app list --display-name $IngestionAppName --query '[0].appId' -o tsv
            if (-not $ingAppId) {
                $ingAppId = Invoke-Az ad app create --display-name $IngestionAppName --sign-in-audience AzureADMultipleOrgs --web-redirect-uris 'https://login.microsoftonline.com/common/oauth2/nativeclient' --query appId -o tsv
                Write-Host "  app registration created : $IngestionAppName ($ingAppId)"
            } else {
                Write-Host "  app registration reused  : $IngestionAppName ($ingAppId)"
            }
            $ingSpId = Invoke-AzOptional ad sp show --id $ingAppId --query id -o tsv
            if (-not $ingSpId) { $ingSpId = Invoke-AzRetry ad sp create --id $ingAppId --query id -o tsv }
            $graphApi = '00000003-0000-0000-c000-000000000000'
            $sitesSelected = Invoke-Az ad sp show --id $graphApi --query "appRoles[?value=='Sites.Selected'].id | [0]" -o tsv
            Invoke-AzOptional ad app permission add --id $ingAppId --api $graphApi --api-permissions "$sitesSelected=Role" | Out-Null
            & (Join-Path $root 'scripts/itsm/grant-graph-app-roles.ps1') -PrincipalId $ingSpId -Roles Sites.Selected
            $ingSecretName = 'ingestion-secret-v2'
            if ($RotateIngestionSecret -or -not (Test-KvSecret $ingSecretName)) {
                $s = Invoke-Az ad app credential reset --id $ingAppId --display-name ingestion --years 1 --append --query password -o tsv
                Set-KvSecret $ingSecretName $s
                $s = $null
                Write-Host "  client secret stored in $kv/$ingSecretName (never displayed)"
            }

            # 2. A temporary app holding Sites.FullControl.All (application): reading a site by URL and
            #    granting Sites.Selected on it both need that permission, which the az session does not
            #    carry (the first tenant did the grant by hand in Cloud Shell, runbook 1.1). Deleted in
            #    the finally block below, whatever happens.
            $helperName = 'ke-bootstrap-site-grant'
            foreach ($old in @((Invoke-Az ad app list --display-name $helperName --query '[].appId' -o tsv) -split "`n" | Where-Object { $_.Trim() })) {
                Invoke-AzOptional ad app delete --id $old.Trim() | Out-Null   # leftover of an interrupted run
            }
            $helperAppId = $null
            $helperSecret = $null
            $token = $null
            $sites = @{}
            try {
                $helperAppId = Invoke-Az ad app create --display-name $helperName --sign-in-audience AzureADMyOrg --query appId -o tsv
                $helperSpId = Invoke-AzRetry ad sp create --id $helperAppId --query id -o tsv
                & (Join-Path $root 'scripts/itsm/grant-graph-app-roles.ps1') -PrincipalId $helperSpId -Roles Sites.FullControl.All
                $helperSecret = Invoke-Az ad app credential reset --id $helperAppId --display-name bootstrap --years 1 --query password -o tsv
                for ($i = 1; $i -le 24; $i++) {
                    try {
                        $token = Get-AppToken $helperAppId $helperSecret
                        Invoke-GraphBearer $token GET '/sites/root' $null | Out-Null
                        break
                    } catch {
                        $token = $null
                        if ($i -ge 24) { throw "The temporary grant app is still not authorized after 6 min: $($_.Exception.Message)" }
                        Write-Host "  waiting for the temporary grant app (Entra replication, attempt $i/24)..."
                        Start-Sleep -Seconds 15
                    }
                }

                # 3. One site per client: the one named in -SharePointSiteUrls, else the team site of a
                #    new private Microsoft 365 group "KE <client> KB".
                foreach ($c in $SharePointClients) {
                    $site = $null
                    $url = $null
                    if ($SharePointSiteUrls -and $SharePointSiteUrls.ContainsKey($c)) { $url = [string]$SharePointSiteUrls[$c] }
                    if ($url) {
                        $u = [uri]$url
                        $m = [regex]::Match($u.AbsolutePath, '^/(sites|teams)/[^/]+')
                        if (-not $m.Success) { throw "SharePointSiteUrls['$c'] must look like https://<tenant>.sharepoint.com/sites/<SiteName> (got $url)" }
                        $site = Invoke-GraphBearer $token GET "/sites/$($u.Host):$($m.Value)" $null
                        Write-Host "  existing site : $($site.webUrl)"
                    } else {
                        $nick = "ke-$c-kb"
                        $grp = @((Invoke-Graph GET "/groups?`$filter=mailNickname%20eq%20'$nick'" $null).value) | Select-Object -First 1
                        if (-not $grp) {
                            $grp = Invoke-Graph POST '/groups' @{
                                displayName          = "KE $c KB"
                                description          = "KnowledgeEngine v9 - knowledge base of $c, read by logic-ingest-$c"
                                mailNickname         = $nick
                                mailEnabled          = $true
                                securityEnabled      = $false
                                groupTypes           = @('Unified')
                                visibility           = 'Private'
                                'owners@odata.bind'  = @("https://graph.microsoft.com/v1.0/users/$me")
                                'members@odata.bind' = @("https://graph.microsoft.com/v1.0/users/$me")
                            }
                            Write-Host "  Microsoft 365 group created : KE $c KB"
                        } else {
                            Write-Host "  Microsoft 365 group reused  : $($grp.displayName)"
                        }
                        $deadline = (Get-Date).AddMinutes(15)
                        while ($true) {
                            try { $site = Invoke-Graph GET "/groups/$($grp.id)/sites/root" $null } catch { $site = $null }
                            if ($site -and $site.id) { break }
                            if ((Get-Date) -gt $deadline) { throw "The SharePoint site of group $nick is still not provisioned after 15 min (normal on a tenant created the same day) - re-run -From ingestion later." }
                            Write-Host '  waiting for SharePoint to provision the site...'
                            Start-Sleep -Seconds 30
                        }
                        Write-Host "  site ready : $($site.webUrl)"
                    }
                    $sites[$c] = $site.id
                    $spSites[$c] = $site.webUrl
                }

                # Read access for the ingestion app on each site (Sites.Selected, runbook 1.1).
                foreach ($c in $SharePointClients) {
                    $has = $false
                    foreach ($perm in @((Invoke-GraphBearer $token GET "/sites/$($sites[$c])/permissions" $null).value)) {
                        foreach ($g in (@($perm.grantedToIdentitiesV2) + @($perm.grantedToIdentities))) {
                            if ($g -and $g.application -and ($g.application.id -eq $ingAppId)) { $has = $true }
                        }
                    }
                    if ($has) {
                        Write-Host "  $IngestionAppName already reads the $c site"
                    } else {
                        Invoke-GraphBearer $token POST "/sites/$($sites[$c])/permissions" @{
                            roles               = @('read')
                            grantedToIdentities = @(@{ application = @{ id = $ingAppId; displayName = $IngestionAppName } })
                        } | Out-Null
                        Write-Host "  $IngestionAppName granted read on the $c site (Sites.Selected)"
                    }
                }
            } finally {
                $helperSecret = $null
                $token = $null
                if ($helperAppId) {
                    Invoke-AzOptional ad app delete --id $helperAppId | Out-Null
                    Write-Host '  temporary grant app deleted'
                }
            }

            # 4. Containers + the ingestion Logic App (daily; routes audio / video to their raw containers).
            foreach ($c in $SharePointClients) {
                Confirm-KbContainer $c
                Confirm-Container "audio-raw-$c"
                Confirm-Container "video-raw-$c"
                $out = Invoke-BicepDeploy "ingestion-$c" 'ingestion/main.bicep' @{
                    clientCode = $c; siteId = $sites[$c]; tenantId = $tenantId; appClientId = $ingAppId
                    namePrefix = $NamePrefix; keyVaultName = $kv; secretName = $ingSecretName; createRoleAssignments = $true
                }
                Write-Host "  $($out.logicAppName.value) deployed (daily; SharePoint -> kb-$c, audio-raw-$c, video-raw-$c)"
                # Keep clients-local/<client>.parameters.json (manual redeploys, runbook 2) on this tenant.
                $pf = Join-Path $root "clients-local/$c.parameters.json"
                if (Test-Path $pf) {
                    $j = [System.IO.File]::ReadAllText($pf, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
                    $upd = @{ siteId = $sites[$c]; tenantId = $tenantId; appClientId = $ingAppId }
                    foreach ($k in $upd.Keys) { if ($j.parameters.PSObject.Properties[$k]) { $j.parameters.$k.value = $upd[$k] } }
                    [System.IO.File]::WriteAllText($pf, ($j | ConvertTo-Json -Depth 10), $utf8NoBom)
                    Write-Host "  clients-local/$c.parameters.json -> this tenant's site and app"
                }
            }
        }
    }

    # -----------------------------------------------------------------------------------
    if (Test-Phase 'audio') {
        if ($SkipAudio -or -not $MediaClients) {
            Write-Step 'audio (skipped: -SkipAudio or empty -MediaClients)'
        } else {
            Write-Step "Audio transcription (Jalon 7): $($MediaClients -join ', ')"
            Confirm-KeyVault
            if (-not (Test-KvSecret 'speech-key')) {
                # Speech runs on the Foundry (AIServices) account itself, as on the first tenant
                # (jalon 7): no separate Speech resource, so no new Cognitive Services account.
                $k = Invoke-Az cognitiveservices account keys list --name $foundryAcct --resource-group $ResourceGroup --query key1 -o tsv
                Set-KvSecret 'speech-key' $k
                $k = $null
                Write-Host "  speech-key stored in $kv (key1 of $foundryAcct)"
            }
            foreach ($c in $MediaClients) {
                Confirm-KbContainer $c
                Confirm-Container "audio-raw-$c"
                $out = Invoke-BicepDeploy "audio-$c" 'ingestion/audio-transcribe/main.bicep' @{
                    clientCode = $c; speechEndpoint = "https://$foundryAcct.cognitiveservices.azure.com"
                    namePrefix = $NamePrefix; keyVaultName = $kv; createRoleAssignments = $true
                }
                Write-Host "  $($out.logicAppName.value) deployed (daily; audio-raw-$c -> transcripts in kb-$c)"
            }
        }
    }

    # -----------------------------------------------------------------------------------
    if (Test-Phase 'video') {
        if ($SkipVideo -or -not $MediaClients) {
            Write-Step 'video (skipped: -SkipVideo or empty -MediaClients)'
        } else {
            Write-Step "Video indexing (Jalon 8): $($MediaClients -join ', ')"
            if ((Invoke-Az provider show --namespace Microsoft.VideoIndexer --query registrationState -o tsv) -ne 'Registered') {
                Invoke-Az provider register --namespace Microsoft.VideoIndexer | Out-Null
                $deadline = (Get-Date).AddMinutes(10)
                while ((Invoke-Az provider show --namespace Microsoft.VideoIndexer --query registrationState -o tsv) -ne 'Registered') {
                    if ((Get-Date) -gt $deadline) { throw 'Microsoft.VideoIndexer still not registered after 10 min.' }
                    Write-Host '  waiting for the Microsoft.VideoIndexer provider registration...'
                    Start-Sleep -Seconds 20
                }
            }
            $viName = "vi-$NamePrefix-v9"
            Invoke-BicepDeploy 'videoindexer' 'infra/modules/videoindexer.bicep' @{ videoIndexerAccountName = $viName; location = $Location; storageAccountName = $st } | Out-Null
            # The Logic App wants the account's internal GUID (properties.accountId), not its ARM id (runbook 10).
            $viAccountId = Invoke-Az resource show --resource-group $ResourceGroup --name $viName --resource-type Microsoft.VideoIndexer/accounts --query properties.accountId -o tsv
            Write-Host "  Video Indexer account $viName ready (accountId $viAccountId)"
            foreach ($c in $MediaClients) {
                Confirm-KbContainer $c
                Confirm-Container "video-raw-$c"
                $out = Invoke-BicepDeploy "video-$c" 'ingestion/video-index/main.bicep' @{
                    clientCode = $c; videoIndexerAccountId = $viAccountId; videoIndexerAccountName = $viName
                    videoIndexerLocation = $Location; namePrefix = $NamePrefix; createRoleAssignments = $true
                }
                Write-Host "  $($out.logicAppName.value) deployed (video-raw-$c -> indexed text in kb-$c)"
            }
        }
    }

    # -----------------------------------------------------------------------------------
    if (Test-Phase 'itsm') {
        if ($SkipItsm) {
            Write-Step 'itsm (skipped: -SkipItsm)'
        } else {
            Write-Step "ITSM module (Jalon 10) on ServiceNow $SnInstance (runbook 11)"
            Confirm-KeyVault
            $itsmScripts = Join-Path $root 'scripts/itsm'

            # 1. svc_ke_itsm password: asked once, checked against ServiceNow, kept in Key Vault only.
            $snSecretName = 'servicenow-svc-ke-itsm-password'
            if (-not (Test-KvSecret $snSecretName)) {
                $sec = Read-Host "  Password of ServiceNow user $SnUser on $SnInstance (goes to $kv, never displayed)" -AsSecureString
                $plain = (New-Object System.Net.NetworkCredential('', $sec)).Password
                $basic = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes("${SnUser}:$plain"))
                $probe = $null
                try {
                    $probe = Invoke-RestMethod -Method Get -Uri "https://$SnInstance.service-now.com/api/now/table/sys_user_group?sysparm_limit=1" -Headers @{ Authorization = "Basic $basic"; Accept = 'application/json' } -UseBasicParsing
                } catch {
                    throw "ServiceNow refused $SnUser on $SnInstance (wrong password?): $($_.Exception.Message)"
                }
                # A hibernating PDI answers 200 with an HTML page, not JSON.
                if (-not (($probe -is [psobject]) -and $probe.PSObject.Properties['result'])) {
                    throw "No JSON from $SnInstance - the developer instance is probably hibernating: wake it up on developer.servicenow.com, then re-run -From itsm."
                }
                Set-KvSecret $snSecretName $plain
                $plain = $null; $basic = $null; $sec = $null
                Write-Host "  $SnUser password checked and stored in $kv/$snSecretName"
            }

            # 2. Fictional demo identities on THIS tenant; ServiceNow callers re-pointed to the new UPNs.
            & (Join-Path $itsmScripts 'seed-demo-identities.ps1') -TenantDomain $domain -SnInstance $SnInstance

            # 3. Poller (+ itsmtickets table), proposer (GPT-4o), executor (real actions after an agent
            #    validates), with their Graph application permissions (runbook 11.4 - 11.9).
            $poll = Invoke-BicepDeploy 'itsm-poll' 'itsm/poll/main.bicep' @{
                snInstance = $SnInstance; snUser = $SnUser; namePrefix = $NamePrefix; keyVaultName = $kv; createRoleAssignments = $true
            }
            Write-Host "  $($poll.logicAppName.value) deployed (every 5 min)"
            $prop = Invoke-BicepDeploy 'itsm-propose' 'itsm/propose/main.bicep' @{
                namePrefix = $NamePrefix; startEnabled = $true; createRoleAssignments = $true
            }
            & (Join-Path $itsmScripts 'grant-graph-app-roles.ps1') -PrincipalId $prop.managedIdentityPrincipalId.value -Roles Directory.Read.All
            Write-Host "  $($prop.logicAppName.value) deployed (Enabled)"
            $exe = Invoke-BicepDeploy 'itsm-execute' 'itsm/execute/main.bicep' @{
                namePrefix = $NamePrefix; keyVaultName = $kv; snInstance = $SnInstance; snUser = $SnUser; webAppName = $web
                dryRun = $false; startEnabled = $true; createRoleAssignments = $true
            }
            $exeId = $exe.managedIdentityPrincipalId.value
            & (Join-Path $itsmScripts 'grant-graph-app-roles.ps1') -PrincipalId $exeId -Roles Directory.Read.All, GroupMember.ReadWrite.All, User.EnableDisableAccount.All, User.RevokeSessions.All, LicenseAssignment.ReadWrite.All
            & (Join-Path $itsmScripts 'setup-secret-actions.ps1') -PrincipalId $exeId
            Write-Host "  $($exe.logicAppName.value) deployed (Enabled, real actions; delivery vault $($exe.deliveryVaultName.value))"

            # 4. Agent review tab (/itsm): KE-v9-itsm-agents + config/itsm.yaml, deployed by the webapp phase.
            $agents = Confirm-EntraGroup 'KE-v9-itsm-agents'
            Add-GroupMember $agents $me
            $itsmYaml = Join-Path $root 'config/itsm.yaml'
            Set-AccessBlockFile $itsmYaml $tenantId $agents 'KE-v9-itsm-agents'
            $y = [System.IO.File]::ReadAllText($itsmYaml, [System.Text.Encoding]::UTF8)
            $y2 = [regex]::Replace($y, '(?m)^serviceNowInstance:[^\r\n]*', "serviceNowInstance: $SnInstance")
            if ($y2 -ne $y) { [System.IO.File]::WriteAllText($itsmYaml, $y2, $utf8NoBom) }

            # 5. Reopen the demo tickets and empty the queue (the last demo on the old tenant closed some).
            & (Join-Path $itsmScripts 'reset-demo.ps1') -SnInstance $SnInstance -KeyVaultName $kv -SnUser $SnUser -StorageAccount $st
        }
    }

    # -----------------------------------------------------------------------------------
    if (Test-Phase 'webapp') {
        Write-Step "Web app code ($web)"
        # config/ plus, from the git-ignored clients-local/, only engine.<client>.yaml of the real
        # clients of -Clients (never the demo credentials, eval data or other clients there).
        $localClients = @(Get-LocalClients)
        if ($localClients.Count -gt 0) {
            & (Join-Path $root 'deploy-webapp.ps1') -ResourceGroup $ResourceGroup -WebAppName $web -ClientsLocal $localClients
        } else {
            & (Join-Path $root 'deploy-webapp.ps1') -ResourceGroup $ResourceGroup -WebAppName $web -SkipClientsLocal
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'az webapp deploy reported a failure - its status poll can fail on a real success (runbook 7): open the site before re-running.'
        }
    }

    Write-Step 'Done'
    $webHost = Invoke-AzOptional webapp show --resource-group $ResourceGroup --name $web --query defaultHostName -o tsv
    Write-Host "  App          : https://$webHost"
    Write-Host "  Sign in with : $demoUpn (member of the KE-v9-* groups), or your own admin account"
    foreach ($c in $spSites.Keys) {
        Write-Host "  SharePoint   : $c -> $($spSites[$c])"
        Write-Host "                 documents / audio / video put in its Documents library are copied about 1 h after this run, then daily,"
        Write-Host "                 and indexed within the hour. To do it right away: .\scripts\sync-client.ps1 -ClientId $c -WithMedia"
    }
    if ($externalTenants.Count -gt 0) {
        $authAppId = Invoke-AzOptional ad app list --display-name $AppDisplayName --query '[0].appId' -o tsv
        foreach ($c in $externalTenants.Keys) {
            $t = $externalTenants[$c]
            Write-Host "  External     : $c <- every user of tenant $t. Once, a Global Administrator of that tenant"
            Write-Host "                 signs in to the app and accepts for the organization, or opens"
            Write-Host "                 https://login.microsoftonline.com/$t/adminconsent?client_id=$authAppId"
        }
    }
    Write-Host '  Commit       : config/engine.client*.yaml and config/itsm.yaml now carry this tenant and group IDs'
    $skipped = New-Object System.Collections.Generic.List[string]
    if ($SkipSharePoint) { $skipped.Add('SharePoint ingestion (-SkipSharePoint)') }
    if ($SkipAudio) { $skipped.Add('audio (-SkipAudio)') }
    if ($SkipVideo) { $skipped.Add('video (-SkipVideo)') }
    if ($SkipItsm) { $skipped.Add('ITSM (-SkipItsm)') }
    if ($skipped.Count -gt 0) { Write-Host "  Not deployed : $($skipped -join '; ')" }
} finally {
    if ($transcribing) { Stop-Transcript | Out-Null }
    Pop-Location
    if ($script:demoPassword) {
        Write-Host ''
        Write-Host "  Demo password for $demoUpn : $($script:demoPassword)" -ForegroundColor Yellow
        Write-Host '  (shown once, kept out of the log - re-run with -From entra to reset it)' -ForegroundColor Yellow
    }
}
