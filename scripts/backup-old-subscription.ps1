<#
.SYNOPSIS
    Read-only backup of the human-made data held by the disabled subscription (runbook 19.21), before Azure
    deletes it: the client's KB documents, the scrubbed tickets, the labels, the reference questions, the
    dictionary and exclusion decisions, the routing review decisions.

.DESCRIPTION
    Nothing is written to Azure: every call is a GET with the signed-in user's token (Storage data plane).
    Blob names, metadata and table rows are written to disk as the raw bytes Azure returned, so accents and
    en dashes survive Windows PowerShell 5.1 (az output captured in a variable would corrupt them).

    What is saved under -OutDir:
      manifests\<container>-list-NNNN.xml   blob listings with metadata (needed to restore the clientid metadata)
      blobs\kb-<client>\...                 the client's documents (audio/video transcripts are skipped:
                                            the new project drops audio and video)
      blobs\tickets-<client>\...            whatever is left of the raw ITSM export (usually nothing)
      blobs\kecore-<client>\...             dictionary-decisions.json and exclusion-config.json
                                            (-FullKecore: the whole container, model records and runs included,
                                            as a cold archive: none of it can be replayed in a new tenant)
      tables\<table>-NNNN.json              ticketlabels, refquestions, kefindpending, kefindreview, tickets,
                                            kecorescores (raw JSON pages, Edm types kept)
      summary.txt                           counts per container and per table

    The raw call recordings (audio-raw-*) and videos (video-raw-*) are NOT saved: they hold personal data and
    the new project drops audio and video.

    Prerequisites: az CLI signed in to the OLD tenant (az login --tenant <id>, add --allow-no-subscriptions if
    az says no subscription is found), and the signed-in user holding Storage Blob Data Reader and Storage
    Table Data Reader (or Contributor) on the account. A role cannot be granted while the subscription is
    disabled: if the probe says AuthorizationPermissionMismatch, the data cannot be read this way.

    The output holds personal data (tickets, labeler ids): keep -OutDir OUTSIDE any git clone, on an encrypted
    disk, and delete it once the restore into the new tenant is verified. Keep the path short (Windows 260-char
    limit).

.EXAMPLE
    az login --tenant a50d938b-4d51-449f-bfb7-ddd94df25268
    .\scripts\backup-old-subscription.ps1 -ProbeOnly
    .\scripts\backup-old-subscription.ps1
#>
[CmdletBinding()]
param(
    [string]$Tenant = 'a50d938b-4d51-449f-bfb7-ddd94df25268',   # KnowledgeEngineV9655.onmicrosoft.com
    [string]$StorageAccount = 'stknowledgeengine3v9',
    [string]$Client = 'client-s',
    [string]$OutDir = ('C:\ke-backup\' + (Get-Date -Format 'yyyyMMdd-HHmm')),
    [string[]]$Tables = @('ticketlabels', 'refquestions', 'kefindpending', 'kefindreview', 'tickets', 'kecorescores'),
    [switch]$FullKecore,
    [switch]$ProbeOnly
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Windows PowerShell 5.1: the progress bar makes every web call very slow

if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'az CLI not found on PATH' }
$BlobBase = 'https://' + $StorageAccount + '.blob.core.windows.net/'
$TableBase = 'https://' + $StorageAccount + '.table.core.windows.net/'

# ------------------------------------------------------------------------------------------------ helpers
$script:Token = $null
$script:TokenAt = [DateTime]::MinValue
function Get-StorageToken {
    # az returns its cached token while it is valid for at least 5 more minutes: asking again every 4 minutes
    # never hands out an expired one.
    if (-not $script:Token -or $script:TokenAt -lt (Get-Date).AddMinutes(-4)) {
        $t = az account get-access-token --tenant $Tenant --resource 'https://storage.azure.com/' --query accessToken -o tsv
        if ($LASTEXITCODE -or -not $t) { throw "no storage token for tenant $Tenant : run az login --tenant $Tenant (add --allow-no-subscriptions if needed)" }
        $script:Token = ([string]$t).Trim()
        $script:TokenAt = Get-Date
    }
    return $script:Token
}

function Get-ErrorText($Err) {
    $code = 0
    if ($Err.Exception.Response) { $code = [int]$Err.Exception.Response.StatusCode }
    $body = ''
    if ($Err.ErrorDetails -and $Err.ErrorDetails.Message) { $body = $Err.ErrorDetails.Message }
    if (-not $body) { $body = $Err.Exception.Message }
    $m = [regex]::Match($body, '<Code>([^<]+)</Code>|"code"\s*:\s*"([^"]+)"')
    $what = if ($m.Success) { ($m.Groups[1].Value + $m.Groups[2].Value) } else { $body.Substring(0, [Math]::Min(300, $body.Length)) }
    return @{ Status = $code; Text = $what }
}

function Invoke-StorageGet([string]$Uri, [hashtable]$Extra) {
    $h = @{ 'Authorization' = 'Bearer ' + (Get-StorageToken); 'x-ms-date' = [DateTime]::UtcNow.ToString('R') }
    foreach ($k in $Extra.Keys) { $h[$k] = $Extra[$k] }
    return Invoke-WebRequest -Uri $Uri -Headers $h -Method Get -UseBasicParsing
}

function Get-Header($Resp, [string]$Name) {
    foreach ($k in $Resp.Headers.Keys) {
        if ($k -ieq $Name) { return (@($Resp.Headers[$k]) -join ',') }
    }
    return ''
}

$BlobHeaders = @{ 'x-ms-version' = '2021-08-06' }
$TableHeaders = @{
    'x-ms-version' = '2019-02-02'
    'Accept' = 'application/json;odata=minimalmetadata'
    'DataServiceVersion' = '3.0;NetFx'
    'MaxDataServiceVersion' = '3.0;NetFx'
}

function ConvertTo-LocalPath([string]$Root, [string]$BlobName) {
    # One file per blob; characters Windows refuses in a file name become '_' (the manifest keeps the real name).
    $parts = foreach ($seg in ($BlobName -split '/')) {
        $s = [regex]::Replace($seg, '[<>:"\\|?*\x00-\x1F]', '_')
        $s = $s.TrimEnd('.', ' ')
        if (-not $s) { $s = '_' }
        $s
    }
    return [IO.Path]::Combine($Root, ($parts -join [IO.Path]::DirectorySeparatorChar))
}

function Save-Container([string]$Container, [string]$Prefix, [string[]]$OnlyNames, [switch]$SkipMedia) {
    # Lists the container (raw XML pages kept as the manifest), then downloads each blob as bytes.
    $manifestDir = Join-Path $OutDir 'manifests'
    $dest = Join-Path $OutDir ('blobs\' + $Container)
    [void][IO.Directory]::CreateDirectory($manifestDir)
    $marker = ''; $page = 0
    $listed = 0; $saved = 0; $skipped = 0; $failed = @()
    do {
        $page++
        $uri = $BlobBase + $Container + '?restype=container&comp=list&include=metadata&maxresults=1000'
        if ($Prefix) { $uri += '&prefix=' + [Uri]::EscapeDataString($Prefix) }
        if ($marker) { $uri += '&marker=' + [Uri]::EscapeDataString($marker) }
        try { $r = Invoke-StorageGet $uri $BlobHeaders }
        catch {
            $e = Get-ErrorText $_
            if ($e.Status -eq 404 -and $page -eq 1) { Write-Warning "$Container : no such container"; return [pscustomobject]@{ Container = $Container; Listed = 0; Saved = 0; Skipped = 0; Failed = 0; Note = 'absent' } }
            throw "listing $Container refused: HTTP $($e.Status) $($e.Text)"
        }
        $bytes = $r.RawContentStream.ToArray()
        $tag = if ($Prefix) { '-' + [regex]::Replace($Prefix, '[^A-Za-z0-9._-]', '_') } else { '' }
        [IO.File]::WriteAllBytes((Join-Path $manifestDir ('{0}{1}-list-{2:D4}.xml' -f $Container, $tag, $page)), $bytes)
        $xml = [xml]([Text.Encoding]::UTF8.GetString($bytes).TrimStart([char]0xFEFF))
        foreach ($b in @($xml.SelectNodes('/EnumerationResults/Blobs/Blob'))) {
            $name = $b.SelectSingleNode('Name').InnerText
            if ($OnlyNames -and ($OnlyNames -notcontains $name)) { continue }
            $listed++
            $kind = $b.SelectSingleNode('Metadata/sourcetype')
            if ($SkipMedia -and $kind -and ($kind.InnerText -in @('audio', 'video'))) { $skipped++; continue }
            $blobUri = $BlobBase + $Container + '/' + ((($name -split '/') | ForEach-Object { [Uri]::EscapeDataString($_) }) -join '/')
            try {
                $resp = Invoke-StorageGet $blobUri $BlobHeaders
                $local = ConvertTo-LocalPath $dest $name
                [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($local))
                [IO.File]::WriteAllBytes($local, $resp.RawContentStream.ToArray())
                $saved++
            } catch {
                $e = Get-ErrorText $_
                $failed += ('{0} (HTTP {1} {2})' -f $name, $e.Status, $e.Text)
            }
        }
        $node = $xml.SelectSingleNode('/EnumerationResults/NextMarker')
        $marker = if ($node) { $node.InnerText } else { '' }
    } while ($marker)
    foreach ($f in $failed) { Write-Warning "$Container : not saved: $f" }
    $note = ''
    if ($OnlyNames -and $listed -eq 0) { $note = 'absent (normal if never written)' }
    return [pscustomobject]@{ Container = $Container; Listed = $listed; Saved = $saved; Skipped = $skipped; Failed = $failed.Count; Note = $note }
}

function Save-Table([string]$Table) {
    # Raw JSON pages, 100 rows each, following the continuation headers.
    $dir = Join-Path $OutDir 'tables'
    [void][IO.Directory]::CreateDirectory($dir)
    $npk = ''; $nrk = ''; $page = 0; $rows = 0
    do {
        $page++
        $uri = $TableBase + $Table + '()?' + '$top=100'
        if ($npk) { $uri += '&NextPartitionKey=' + [Uri]::EscapeDataString($npk) }
        if ($nrk) { $uri += '&NextRowKey=' + [Uri]::EscapeDataString($nrk) }
        try { $r = Invoke-StorageGet $uri $TableHeaders }
        catch {
            $e = Get-ErrorText $_
            if ($e.Status -eq 404 -and $page -eq 1) { Write-Warning "$Table : no such table (never created)"; return [pscustomobject]@{ Table = $Table; Rows = 0; Pages = 0; Note = 'absent' } }
            throw "reading table $Table refused: HTTP $($e.Status) $($e.Text)"
        }
        $bytes = $r.RawContentStream.ToArray()
        [IO.File]::WriteAllBytes((Join-Path $dir ('{0}-{1:D4}.json' -f $Table, $page)), $bytes)
        $rows += ([regex]::Matches([Text.Encoding]::UTF8.GetString($bytes), '"RowKey"\s*:')).Count
        $npk = Get-Header $r 'x-ms-continuation-NextPartitionKey'
        $nrk = Get-Header $r 'x-ms-continuation-NextRowKey'
    } while ($npk)
    return [pscustomobject]@{ Table = $Table; Rows = $rows; Pages = $page; Note = '' }
}

# ------------------------------------------------------------------------------------------------ 1. probe
"tenant $Tenant, storage account $StorageAccount, client $Client"
[void](Get-StorageToken)
try {
    $r = Invoke-StorageGet ($BlobBase + '?comp=list&maxresults=200') $BlobHeaders
    $xml = [xml]([Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()).TrimStart([char]0xFEFF))
    $containers = @($xml.SelectNodes('/EnumerationResults/Containers/Container/Name') | ForEach-Object { $_.InnerText })
    'blob data plane: readable, ' + $containers.Count + ' containers: ' + (($containers | Where-Object { $_ -notlike 'azure-webjobs*' }) -join ', ')
} catch {
    $e = Get-ErrorText $_
    throw "blob data plane refused: HTTP $($e.Status) $($e.Text). AuthorizationPermissionMismatch = this user has no Storage Blob Data role on the account (it cannot be granted while the subscription is disabled)."
}
try {
    $r = Invoke-StorageGet ($TableBase + 'Tables') $TableHeaders
    $names = @(([Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()) | ConvertFrom-Json).value | ForEach-Object { $_.TableName })
    'table data plane: readable, ' + $names.Count + ' tables: ' + ($names -join ', ')
} catch {
    $e = Get-ErrorText $_
    throw "table data plane refused: HTTP $($e.Status) $($e.Text). AuthorizationPermissionMismatch = this user has no Storage Table Data role on the account."
}
if ($ProbeOnly) { 'probe only: nothing downloaded'; return }

# ------------------------------------------------------------------------------------------------ 2. blobs and tables
[void][IO.Directory]::CreateDirectory($OutDir)
"saving to $OutDir"
$blobResults = @()
$blobResults += Save-Container ('kb-' + $Client) '' $null -SkipMedia
$blobResults += Save-Container ('tickets-' + $Client) '' $null
if ($FullKecore) {
    $blobResults += Save-Container ('kecore-' + $Client) '' $null
} else {
    foreach ($n in 'dictionary-decisions.json', 'exclusion-config.json') {
        $res = Save-Container ('kecore-' + $Client) $n @($n)
        $res.Container = 'kecore-' + $Client + '/' + $n
        $blobResults += $res
    }
}
$tableResults = @()
foreach ($t in $Tables) { $tableResults += Save-Table $t }

# ------------------------------------------------------------------------------------------------ 3. summary
$lines = @()
$lines += 'backup of ' + $StorageAccount + ' (tenant ' + $Tenant + ', client ' + $Client + ') taken ' + (Get-Date -Format 'yyyy-MM-dd HH:mm')
$lines += ''
$lines += '{0,-48} {1,7} {2,7} {3,8} {4,7}  {5}' -f 'container', 'listed', 'saved', 'skipped', 'failed', 'note'
foreach ($b in $blobResults) { $lines += '{0,-48} {1,7} {2,7} {3,8} {4,7}  {5}' -f $b.Container, $b.Listed, $b.Saved, $b.Skipped, $b.Failed, $b.Note }
$lines += ''
$lines += '{0,-48} {1,7} {2,7}  {3}' -f 'table', 'rows', 'pages', 'note'
foreach ($t in $tableResults) { $lines += '{0,-48} {1,7} {2,7}  {3}' -f $t.Table, $t.Rows, $t.Pages, $t.Note }
$lines += ''
$lines += 'Skipped = audio/video transcripts (dropped by the new project). Raw recordings were not read.'
[IO.File]::WriteAllLines((Join-Path $OutDir 'summary.txt'), [string[]]$lines, (New-Object System.Text.UTF8Encoding $false))
$lines
$bad = @($blobResults | Where-Object { $_.Failed -gt 0 })
if ($bad.Count) { throw ('some blobs were not saved (see the warnings above): ' + (($bad | ForEach-Object { $_.Container }) -join ', ') + '. Run the script again: it rewrites the same files.') }
'done: keep ' + $OutDir + ' outside any git clone, on an encrypted disk, until the restore is verified.'
