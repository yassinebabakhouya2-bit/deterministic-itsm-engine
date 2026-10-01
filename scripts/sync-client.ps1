<#
.SYNOPSIS
  Brings one client's content in now instead of waiting for the daily Logic App runs (runbook 12):
  SharePoint -> Blob (logic-ingest-<client>), then indexes kb-<client> (ix-<client>-di, ix-<client>-text).
  -WithMedia also runs the audio and video pipelines (logic-transcribe-<client>,
  logic-video-index-<client>). -StatusOnly changes nothing and shows where the client stands:
  last run of each Logic App (with the failing step and its error when a run failed), files in
  Blob, transcription progress, indexers, chunks in idx-<client>.

.EXAMPLE
  .\scripts\sync-client.ps1 -ClientId client-s -WithMedia
.EXAMPLE
  .\scripts\sync-client.ps1 -ClientId client-s -StatusOnly
.EXAMPLE
  .\scripts\sync-client.ps1 -ClientId client-s -Trace video

  -Trace ingest|audio|video changes nothing either: it walks the last run of that Logic App step
  by step (status, HTTP code, Video Indexer state), including one iteration of each loop. It never
  prints tokens, secrets, SAS or download URLs, nor document / transcript content.

  Safe to re-run: files already in Blob are skipped, indexers resume where they stopped, and a
  Logic App run already in progress is followed instead of being started twice. Ctrl+C only
  stops the watching: the runs go on in Azure.
  Audio files are transcribed one at a time (a few minutes each), so a full audio library takes
  hours. The indexers also run by themselves every hour, so transcripts written after this script
  ends are picked up without it.
  Windows PowerShell 5.1 or PowerShell 7. ASCII-only source (runbook 9.1).
#>
param(
    [Parameter(Mandatory = $true)][string]$ClientId,
    [string]$ResourceGroup = 'rg-knowledgeengine-v9',
    [string]$NamePrefix = 'knowledgeengine3',
    [switch]$WithMedia,
    [switch]$StatusOnly,
    [ValidateSet('ingest', 'audio', 'video')][string]$Trace,
    [int]$TimeoutMinutes = 120
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Invoke-AzJson {
    # az with JSON output; throws with az's own error text on failure.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & az @args 2>&1; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $prev }
    $text = ((@($out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) | ForEach-Object { "$_" }) -join "`n").Trim()
    if ($code -ne 0) {
        $err = (@($out | ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" } }) -join "`n")
        throw "az $($args[0]) $($args[1]) failed: $err"
    }
    if ($text) { return ($text | ConvertFrom-Json) }
    return $null
}

function ConvertTo-Dto($Value) {
    # Windows PowerShell 5.1 leaves JSON dates as strings, PowerShell 7 turns them into DateTime.
    if ($Value -is [datetime]) { return [datetimeoffset]$Value }
    return [datetimeoffset]::Parse("$Value", [System.Globalization.CultureInfo]::InvariantCulture)
}

function Format-Time($Value) {
    if (-not $Value) { return '-' }
    return (ConvertTo-Dto $Value).ToLocalTime().ToString('yyyy-MM-dd HH:mm')
}

$sub = (Invoke-AzJson account show -o json).id
$wfBase = "https://management.azure.com/subscriptions/$sub/resourceGroups/$ResourceGroup/providers/Microsoft.Logic/workflows"
$la = 'api-version=2019-05-01'
$sa = 'api-version=2024-07-01'
$st = "st$($NamePrefix)v9"
$srch = "srch-$NamePrefix-v9"
$ingest = "logic-ingest-$ClientId"
$transcribe = "logic-transcribe-$ClientId"
$videoLa = "logic-video-index-$ClientId"
$ixDi = "ix-$ClientId-di"
$ixText = "ix-$ClientId-text"
$loopTypes = @('Foreach', 'Until')
$scopeTypes = @('If', 'Scope', 'Switch')

$script:armToken = $null
$script:armTokenExp = [datetimeoffset]::MinValue
function Update-ArmToken {
    # az returns its cached token, which can have only a few minutes left: keep its real expiry.
    $t = Invoke-AzJson account get-access-token --resource https://management.azure.com/ -o json
    $script:armToken = $t.accessToken
    $exp = $null
    if ($t.expires_on) { $exp = [datetimeoffset]::FromUnixTimeSeconds([long]$t.expires_on) }
    elseif ($t.expiresOn) { try { $exp = [datetimeoffset]::Parse("$($t.expiresOn)") } catch { } }
    if (-not $exp) { $exp = [datetimeoffset]::UtcNow.AddMinutes(10) }
    $script:armTokenExp = $exp
}

function Invoke-Arm([string]$Method, [string]$Url) {
    # Management REST call with the az token, renewed before it expires and once more on a 401.
    if (-not $script:armToken -or $script:armTokenExp -lt [datetimeoffset]::UtcNow.AddMinutes(3)) { Update-ArmToken }
    for ($try = 1; ; $try++) {
        $h = @{ Authorization = "Bearer $($script:armToken)" }
        try {
            if ($Method -eq 'POST') {
                return Invoke-RestMethod -Method Post -Uri $Url -Headers $h -Body '{}' -ContentType 'application/json' -UseBasicParsing
            }
            return Invoke-RestMethod -Method Get -Uri $Url -Headers $h -UseBasicParsing
        } catch {
            if ($try -ge 2 -or "$($_.Exception.Message)" -notmatch '401') { throw }
            Update-ArmToken
        }
    }
}

$searchKey = (Invoke-AzJson search admin-key show --service-name $srch --resource-group $ResourceGroup -o json).primaryKey
function Invoke-Search([string]$Method, [string]$Path, [string]$Body) {
    $h = @{ 'api-key' = $searchKey }
    $uri = "https://$srch.search.windows.net/$Path"
    if ($Method -eq 'POST') {
        return Invoke-RestMethod -Method Post -Uri $uri -Headers $h -Body $Body -ContentType 'application/json' -UseBasicParsing
    }
    return Invoke-RestMethod -Method Get -Uri $uri -Headers $h -UseBasicParsing
}

# ------------------------------------------------------------------ run diagnostics
function Get-LastRun([string]$Name) {
    return @((Invoke-Arm GET "$wfBase/$Name/runs?$la&`$top=1").value) | Select-Object -First 1
}

function Add-ActionTree($Actions, [string]$Parent, $Into) {
    # Flattens a workflow definition: action name -> type and enclosing action.
    if ($null -eq $Actions) { return }
    foreach ($p in $Actions.PSObject.Properties) {
        $a = $p.Value
        $Into[$p.Name] = [pscustomobject]@{ Type = "$($a.type)"; Parent = $Parent }
        Add-ActionTree $a.actions $p.Name $Into
        if ($a.'else') { Add-ActionTree $a.'else'.actions $p.Name $Into }
        if ($a.'default') { Add-ActionTree $a.'default'.actions $p.Name $Into }
        if ($a.cases) { foreach ($c in $a.cases.PSObject.Properties) { Add-ActionTree $c.Value.actions $p.Name $Into } }
    }
}

function Get-LoopOf($Tree, [string]$Name) {
    # Nearest enclosing Foreach/Until of an action; '' at top level.
    $p = $Tree[$Name].Parent
    while ($p) {
        if ($Tree[$p].Type -in $loopTypes) { return $p }
        $p = $Tree[$p].Parent
    }
    return ''
}

function Get-FirstLine($Text) {
    return (("$Text" -split "`n")[0]).Trim()
}

function Get-ArmList([string]$Url, [int]$MaxPages = 50) {
    # GET a list and follow nextLink (a big loop has several pages of iterations).
    $items = New-Object System.Collections.Generic.List[object]
    $next = $Url
    for ($i = 0; $next -and $i -lt $MaxPages; $i++) {
        $page = Invoke-Arm GET $next
        foreach ($v in @($page.value)) { if ($null -ne $v) { $items.Add($v) } }
        $next = "$($page.nextLink)"
    }
    return $items.ToArray()
}

function Get-StepOutput($Props) {
    # Outputs of a run step: inline when small, else behind outputsLink.
    if ($null -ne $Props.outputs) { return $Props.outputs }
    if ($Props.outputsLink -and $Props.outputsLink.uri) {
        try { return (Invoke-RestMethod -Method Get -Uri $Props.outputsLink.uri -UseBasicParsing) } catch { }
    }
    return $null
}

function Get-FailureText($Props) {
    $parts = New-Object System.Collections.Generic.List[string]
    if ($Props.code) { $parts.Add("[$($Props.code)]") }
    if ($Props.error -and $Props.error.message) { $parts.Add("$($Props.error.message)") }
    $o = Get-StepOutput $Props
    if ($null -ne $o -and $o -isnot [string]) {
        if ($null -ne $o.statusCode) { $parts.Add("HTTP $($o.statusCode)") }
        $body = $o.body
        if ($null -ne $body) {
            if ($body -isnot [string]) { $body = $body | ConvertTo-Json -Depth 8 -Compress }
            $parts.Add("$body")
        }
    }
    return (($parts -join ' ') -replace '\s+', ' ').Trim()
}

function Write-ActionFailure([string]$Label, $Props) {
    $line = ("$Label " + (Get-FailureText $Props)).Trim()
    if ($line.Length -gt 700) { $line = $line.Substring(0, 700) + '...' }
    Write-Host "    ! $line" -ForegroundColor Red
}

function Show-InnerFailure([string]$RunUrl, $Tree, [string]$Loop, [string]$OuterRep, [string]$Label) {
    # A loop nested in a failed iteration (e.g. Copy_blocks): shows the first failed step inside it.
    # Repetitions of nested steps are named <outer>-<inner>, e.g. 000012-000003.
    $inner = @($Tree.Keys | Where-Object { (Get-LoopOf $Tree $_) -eq $Loop -and $Tree[$_].Type -notin $scopeTypes })
    foreach ($im in $inner) {
        try { $reps = @(Get-ArmList "$RunUrl/actions/$im/repetitions?$la" 5) } catch { continue }
        $f = @($reps | Where-Object { "$($_.name)" -like "$OuterRep-*" -and "$($_.properties.status)" -in @('Failed', 'TimedOut') }) | Select-Object -First 1
        if ($f) { Write-ActionFailure "  $Label -> $Loop -> $im" $f.properties; return $true }
    }
    return $false
}

function Get-LoopStepFailure([string]$RunUrl, $Tree, [string]$Loop, $Acts) {
    # For a loop whose iterations cannot be listed (Until loops): the run's action list gives the
    # last state of each step of the loop; else each step's own repetitions are read.
    $steps = @($Tree.Keys | Where-Object { (Get-LoopOf $Tree $_) -eq $Loop -and $Tree[$_].Type -notin $scopeTypes -and $_ -ne 'Check_blob_exists' })
    foreach ($m in $steps) {
        if ($Acts.ContainsKey($m) -and "$($Acts[$m].properties.status)" -in @('Failed', 'TimedOut')) {
            return [pscustomobject]@{ Step = $m; Props = $Acts[$m].properties }
        }
    }
    foreach ($m in $steps) {
        try { $reps = @(Get-ArmList "$RunUrl/actions/$m/repetitions?$la" 3) } catch { continue }
        $f = @($reps | Where-Object { "$($_.properties.status)" -in @('Failed', 'TimedOut') }) | Select-Object -First 1
        if ($f) { return [pscustomobject]@{ Step = $m; Props = $f.properties } }
    }
    return $null
}

function Show-RunErrors([string]$Name, [string]$RunName) {
    # Prints the failing step(s) of a run with their error. Inside a loop, lists up to 5 failed
    # iterations with the file each one was working on. Returns how many problems were shown.
    # Check_blob_exists answering 404 is the normal "not copied yet" path of logic-ingest.
    $shown = 0
    try {
        $wf = Invoke-Arm GET "$wfBase/$($Name)?$la"
        $tree = [ordered]@{}
        Add-ActionTree $wf.properties.definition.actions '' $tree
        $runUrl = "$wfBase/$Name/runs/$RunName"
        $acts = @(Get-ArmList "$runUrl/actions?$la")
    } catch {
        Write-Host "    ! could not read run $RunName of ${Name}: $(Get-FirstLine $_.Exception.Message)" -ForegroundColor Red
        return 1
    }
    $actByName = @{}
    foreach ($x in $acts) { $actByName["$($x.name)"] = $x }
    foreach ($a in $acts) {
        if ($shown -ge 4) { break }
        $node = $tree[$a.name]
        if (-not $node) { continue }
        if ((Get-LoopOf $tree $a.name) -ne '') { continue }   # shown through its loop below
        $status = "$($a.properties.status)"
        if ($node.Type -notin $loopTypes) {
            if ($status -in @('Failed', 'TimedOut') -and $node.Type -notin $scopeTypes -and $a.name -ne 'Check_blob_exists') {
                Write-ActionFailure $a.name $a.properties
                $shown++
            }
            continue
        }
        if ($status -notin @('Failed', 'TimedOut', 'Running')) { continue }
        $reps = @()
        try { $reps = @(Get-ArmList "$runUrl/actions/$($a.name)/scopeRepetitions?$la") } catch { }
        if ($reps.Count -eq 0) {
            if ($status -eq 'Running') { continue }
            $f = Get-LoopStepFailure $runUrl $tree $a.name $actByName
            if ($f) { Write-ActionFailure "$($a.name) -> $($f.Step)" $f.Props } else { Write-ActionFailure $a.name $a.properties }
            $shown++
            continue
        }
        $bad = @($reps | Where-Object { "$($_.properties.status)" -in @('Failed', 'TimedOut') })
        if ($bad.Count -eq 0) {
            if ($status -ne 'Running') { Write-ActionFailure $a.name $a.properties; $shown++ }
            continue
        }
        Write-Host "    ! $($a.name): $($bad.Count) of $($reps.Count) iterations failed" -ForegroundColor Red
        $shown++
        $loopName = $a.name
        $members = @($tree.Keys | Where-Object { (Get-LoopOf $tree $_) -eq $loopName -and $tree[$_].Type -notin $scopeTypes -and $_ -ne 'Check_blob_exists' })
        foreach ($b in @($bad | Select-Object -First 5)) {
            $rep = $b.name
            $label = "iteration $rep"
            if ($members -contains 'Compose_blobName') {
                try {
                    $nr = Invoke-Arm GET "$runUrl/actions/Compose_blobName/repetitions/$($rep)?$la"
                    $v = Get-StepOutput $nr.properties
                    if ($null -ne $v -and $v -isnot [string] -and $null -ne $v.body) { $v = $v.body }
                    if ($v) { $label = "$v" }
                } catch { }
            }
            $found = $false
            foreach ($m in $members) {
                if ($m -eq 'Compose_blobName') { continue }
                try { $r = Invoke-Arm GET "$runUrl/actions/$m/repetitions/$($rep)?$la" } catch { continue }
                if ("$($r.properties.status)" -in @('Failed', 'TimedOut')) {
                    $inner = $false
                    if ($tree[$m].Type -in $loopTypes) { $inner = Show-InnerFailure $runUrl $tree $m $rep $label }
                    if (-not $inner) { Write-ActionFailure "  $label -> $m" $r.properties }
                    $found = $true
                    break
                }
            }
            if (-not $found) {
                # The failing step may sit in a nested loop, or be a condition that could not be evaluated.
                foreach ($m in @($members | Where-Object { $tree[$_].Type -in $loopTypes })) {
                    if (Show-InnerFailure $runUrl $tree $m $rep $label) { $found = $true; break }
                }
            }
            if (-not $found) {
                foreach ($m in @($tree.Keys | Where-Object { (Get-LoopOf $tree $_) -eq $loopName -and $tree[$_].Type -in $scopeTypes })) {
                    try { $r = Invoke-Arm GET "$runUrl/actions/$m/repetitions/$($rep)?$la" } catch { continue }
                    if ("$($r.properties.status)" -in @('Failed', 'TimedOut') -and ($r.properties.error -or "$($r.properties.code)" -ne 'ActionFailed')) {
                        Write-ActionFailure "  $label -> $m" $r.properties
                        $found = $true
                        break
                    }
                }
            }
            if (-not $found) { Write-ActionFailure "  $label (iteration)" $b.properties }
        }
        if ($bad.Count -gt 5) { Write-Host "      ... and $($bad.Count - 5) more" -ForegroundColor Red }
    }
    return $shown
}

# ------------------------------------------------------------------ trace (-Trace)
function Get-OkSummary([string]$Step, $Props) {
    # HTTP code and state fields of a successful HTTP step. No body content: bodies can hold
    # tokens, SAS / download URLs or client data, so only status-like fields are shown.
    $o = Get-StepOutput $Props
    if ($null -eq $o -or $o -is [string]) { return '' }
    $parts = @()
    if ($null -ne $o.statusCode) { $parts += "HTTP $($o.statusCode)" }
    if ($Step -match 'token|secret|sas|url|content') { return ($parts -join ' ') }
    $b = $o.body
    if ($null -eq $b -or $b -is [string]) { return ($parts -join ' ') }
    if ($b.videos) {
        $v = @($b.videos)[0]
        $parts += "video state=$($v.state) progress=$($v.processingProgress)"
        if ($v.failureCode -or $v.failureMessage) { $parts += "failure=$($v.failureCode) $($v.failureMessage)" }
    } elseif ($null -ne $b.state) {
        $parts += "state=$($b.state)"
    } elseif ($null -ne $b.status) {
        $parts += "status=$($b.status)"
    }
    return ($parts -join ' ')
}

function Write-TraceLine([string]$Indent, [string]$Step, [string]$Type, $Props) {
    $status = "$($Props.status)"
    $extra = ''
    $color = 'Gray'
    if ($status -in @('Failed', 'TimedOut')) { $color = 'Red'; $extra = Get-FailureText $Props }
    elseif ($status -in @('Running', 'Waiting')) { $color = 'Yellow' }
    elseif ($status -eq 'Skipped') { $color = 'DarkGray' }
    elseif ($Type -eq 'Http') { $extra = Get-OkSummary $Step $Props }
    $line = ("{0}{1,-36} {2} {3}" -f $Indent, $Step, $status, $extra).TrimEnd()
    if ($line.Length -gt 400) { $line = $line.Substring(0, 400) + '...' }
    Write-Host $line -ForegroundColor $color
}

function Show-IterationTrace([string]$RunUrl, $Tree, [string]$Loop, [string]$Rep, [string]$Indent) {
    foreach ($m in @($Tree.Keys | Where-Object { (Get-LoopOf $Tree $_) -eq $Loop })) {
        try { $r = Invoke-Arm GET "$RunUrl/actions/$m/repetitions/$($Rep)?$la" } catch { continue }   # did not run here
        Write-TraceLine $Indent $m $Tree[$m].Type $r.properties
        if ($Tree[$m].Type -notin $loopTypes) { continue }
        # Nested loop: its steps are repeated as <outer>-<inner>; show the count and the last one.
        foreach ($im in @($Tree.Keys | Where-Object { (Get-LoopOf $Tree $_) -eq $m })) {
            try { $all = @(Get-ArmList "$RunUrl/actions/$im/repetitions?$la" 5) } catch { continue }
            $mine = @($all | Where-Object { "$($_.name)" -like "$Rep-*" } | Sort-Object { "$($_.name)" })
            if ($mine.Count -eq 0) { continue }
            Write-TraceLine "$Indent  " "$im (x$($mine.Count), last)" $Tree[$im].Type $mine[-1].properties
        }
    }
}

function Show-RunTrace([string]$Name) {
    $run = Get-LastRun $Name
    if (-not $run) { Write-Host "$Name : no run yet"; return }
    Write-Host "`n== $Name - last run, started $(Format-Time $run.properties.startTime): $($run.properties.status)" -ForegroundColor Cyan
    $wf = Invoke-Arm GET "$wfBase/$($Name)?$la"
    $tree = [ordered]@{}
    Add-ActionTree $wf.properties.definition.actions '' $tree
    $runUrl = "$wfBase/$Name/runs/$($run.name)"
    $acts = @{}
    foreach ($a in @(Get-ArmList "$runUrl/actions?$la")) { $acts["$($a.name)"] = $a }
    foreach ($n in @($tree.Keys)) {
        if ((Get-LoopOf $tree $n) -ne '') { continue }
        $a = $acts[$n]
        if (-not $a) { continue }
        Write-TraceLine '  ' $n $tree[$n].Type $a.properties
        if ($tree[$n].Type -notin $loopTypes) { continue }
        $reps = @()
        try { $reps = @(Get-ArmList "$runUrl/actions/$n/scopeRepetitions?$la") } catch { }
        if ($reps.Count -eq 0) {
            # Until loops: iterations are not listed; show the last state of each step instead.
            foreach ($m in @($tree.Keys | Where-Object { (Get-LoopOf $tree $_) -eq $n })) {
                $p = $null
                if ($acts.ContainsKey($m)) { $p = $acts[$m].properties }
                else {
                    try {
                        $all = @(@(Get-ArmList "$runUrl/actions/$m/repetitions?$la" 3) | Sort-Object { "$($_.name)" })
                        if ($all.Count -gt 0) { $p = $all[-1].properties }
                    } catch { }
                }
                if ($p) { Write-TraceLine '        ' $m $tree[$m].Type $p }
            }
            continue
        }
        $counts = @($reps | Group-Object { "$($_.properties.status)" } | ForEach-Object { "$($_.Count) $($_.Name)" })
        # Show the first failed iteration, else the last one.
        $pick = @($reps | Where-Object { "$($_.properties.status)" -in @('Failed', 'TimedOut') }) | Select-Object -First 1
        if (-not $pick) { $pick = @($reps | Sort-Object { "$($_.name)" })[-1] }
        $label = "iteration $($pick.name)"
        if ($tree.Contains('Compose_blobName') -and (Get-LoopOf $tree 'Compose_blobName') -eq $n) {
            try {
                $v = Get-StepOutput (Invoke-Arm GET "$runUrl/actions/Compose_blobName/repetitions/$($pick.name)?$la").properties
                if ($null -ne $v -and $v -isnot [string] -and $null -ne $v.body) { $v = $v.body }
                if ($v) { $label = "$label ($v)" }
            } catch { }
        }
        Write-Host "      $($counts -join ', ') - $label :"
        Show-IterationTrace $runUrl $tree $n $pick.name '        '
    }
}

# ------------------------------------------------------------------ status
function Get-BlobCount([string]$Container, [string]$MetaKey) {
    try {
        $names = @(Invoke-AzJson storage blob list --account-name $st --container-name $Container --auth-mode login --query '[].name' -o json)
        if (-not $MetaKey) { return "$($names.Count) files" }
        # Projection keeps only the blobs that carry the flag.
        $flags = @(Invoke-AzJson storage blob list --account-name $st --container-name $Container --auth-mode login --include m --query "[].metadata.$MetaKey" -o json)
        $done = @($flags | Where-Object { "$_" -eq 'true' }).Count
        return "$($names.Count) files, $done $MetaKey"
    } catch {
        if ("$($_.Exception.Message)" -match 'ContainerNotFound|does not exist') { return 'no container' }
        return "unknown ($(("$($_.Exception.Message)" -split "`n")[0]))"
    }
}

function Write-IndexerLine([string]$Ix) {
    try {
        $lr = (Invoke-Search GET "indexers/$Ix/status?$sa" '').lastResult
    } catch {
        Write-Host ("  {0,-28} unknown ({1})" -f $Ix, (("$($_.Exception.Message)" -split "`n")[0]))
        return
    }
    if ($null -eq $lr) { Write-Host ("  {0,-28} never run" -f $Ix); return }
    $line = "  {0,-28} {1} (started {2}): {3} processed, {4} failed" -f $Ix, $lr.status, (Format-Time $lr.startTime), $lr.itemsProcessed, $lr.itemsFailed
    if ("$($lr.status)" -eq 'inProgress') { Write-Host $line -ForegroundColor Yellow; return }
    if ("$($lr.status)" -eq 'success' -and [int]$lr.itemsFailed -eq 0) { Write-Host $line -ForegroundColor Green; return }
    Write-Host $line -ForegroundColor Red
    if ($lr.errorMessage) { Write-Host "    ! $($lr.errorMessage)" -ForegroundColor Red }
    foreach ($e in @($lr.errors | Select-Object -First 3)) {
        if ($null -eq $e) { continue }
        $t = "$($e.name) $($e.errorMessage)".Trim()
        if ($t.Length -gt 300) { $t = $t.Substring(0, 300) + '...' }
        Write-Host "    ! $t" -ForegroundColor Red
    }
}

function Show-Status {
    Write-Host "`n== $ClientId - $((Get-Date).ToString('yyyy-MM-dd HH:mm'))" -ForegroundColor Cyan
    foreach ($n in @($ingest, $transcribe, $videoLa)) {
        try {
            $run = Get-LastRun $n
        } catch {
            Write-Host ("  {0,-28} not found ({1})" -f $n, (("$($_.Exception.Message)" -split "`n")[0]))
            continue
        }
        if (-not $run) { Write-Host ("  {0,-28} no run yet" -f $n); continue }
        $s = "$($run.properties.status)"
        $color = 'Red'
        if ($s -eq 'Succeeded') { $color = 'Green' } elseif ($s -in @('Running', 'Waiting')) { $color = 'Yellow' }
        Write-Host ("  {0,-28} {1} (started {2})" -f $n, $s, (Format-Time $run.properties.startTime)) -ForegroundColor $color
        $null = Show-RunErrors $n $run.name   # also for Succeeded: a last 'always run' step can hide failures
    }
    Write-Host ("  {0,-28} {1}" -f "kb-$ClientId", (Get-BlobCount "kb-$ClientId" ''))
    Write-Host ("  {0,-28} {1}" -f "audio-raw-$ClientId", (Get-BlobCount "audio-raw-$ClientId" 'transcribed'))
    Write-Host ("  {0,-28} {1}" -f "video-raw-$ClientId", (Get-BlobCount "video-raw-$ClientId" 'videoindexed'))
    Write-IndexerLine $ixDi
    Write-IndexerLine $ixText
    try {
        $c = Invoke-Search POST "indexes/idx-$ClientId/docs/search?$sa" '{"search":"*","count":true,"top":0}'
        Write-Host ("  {0,-28} {1} chunks searchable" -f "idx-$ClientId", $c.'@odata.count')
    } catch {
        Write-Host ("  {0,-28} unknown ({1})" -f "idx-$ClientId", (("$($_.Exception.Message)" -split "`n")[0]))
    }
}

# ------------------------------------------------------------------ actions
function Start-OrFollow([string]$Name) {
    # Returns the run to follow: the one already in progress, or a new one fired now.
    $recent = @((Invoke-Arm GET "$wfBase/$Name/runs?$la&`$top=5").value)
    $last = $recent | Select-Object -First 1
    if ($last -and "$($last.properties.status)" -in @('Running', 'Waiting')) {
        Write-Host "$Name already running (started $(Format-Time $last.properties.startTime)) - following that run; files it did not list wait for its next run"
        return $last.name
    }
    $before = @($recent | ForEach-Object { $_.name })
    $null = Invoke-Arm POST "$wfBase/$Name/triggers/Recurrence/run?$la"
    Write-Host "$Name started"
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 10
        # Run names are unique: the new run is the one that was not there before (no clock involved).
        $new = @(@((Invoke-Arm GET "$wfBase/$Name/runs?$la&`$top=5").value) | Where-Object { $before -notcontains $_.name })
        if ($new.Count -gt 0) { return ($new | Select-Object -Last 1).name }
    }
    throw "$Name was started but its run did not show up within 5 min - check its run history in the portal."
}

function Wait-Run([string]$Name, [string]$RunName, [int]$Minutes) {
    # Waits for a run to end and returns its status ('Running' if still going at the deadline).
    $deadline = (Get-Date).AddMinutes($Minutes)
    $lastNote = Get-Date
    while ($true) {
        $run = Invoke-Arm GET "$wfBase/$Name/runs/$($RunName)?$la"
        $status = "$($run.properties.status)"
        if ($status -notin @('Running', 'Waiting')) { return $status }
        if ((Get-Date) -gt $deadline) { return $status }
        if (((Get-Date) - $lastNote).TotalMinutes -ge 10) { Write-Host "  $Name : $status..."; $lastNote = Get-Date }
        Start-Sleep -Seconds 20
    }
}

$script:ixAfter = @{}
function Start-Indexer([string]$Ix) {
    # Remembers the start time of the previous run (server clock) to recognise ours when it ends.
    $prev = (Invoke-Search GET "indexers/$Ix/status?$sa" '').lastResult
    $script:ixAfter[$Ix] = [datetimeoffset]::MinValue
    try {
        $null = Invoke-Search POST "indexers/$Ix/run?$sa" ''
        if ($prev) { $script:ixAfter[$Ix] = ConvertTo-Dto $prev.startTime }
        Write-Host "$Ix started"
    } catch {
        if ("$($_.Exception.Message)" -notmatch '409|Conflict') { throw }
        Write-Host "$Ix already running - waiting for that run"
    }
}

function Wait-Indexers([string[]]$Names, [int]$Minutes) {
    $deadline = (Get-Date).AddMinutes($Minutes)
    $pending = New-Object System.Collections.Generic.List[string]
    foreach ($n in $Names) { $pending.Add($n) }
    $lastNote = Get-Date
    while ($true) {
        foreach ($ix in @($pending)) {
            $lr = (Invoke-Search GET "indexers/$ix/status?$sa" '').lastResult
            if ($null -eq $lr -or "$($lr.status)" -eq 'inProgress') { continue }
            if ((ConvertTo-Dto $lr.startTime) -le $script:ixAfter[$ix]) { continue }   # still the previous run
            [void]$pending.Remove($ix)
            Write-IndexerLine $ix
        }
        if ($pending.Count -eq 0) { return }
        if ((Get-Date) -gt $deadline) {
            Write-Warning "Still indexing after $Minutes min: $($pending -join ', '). It goes on in Azure; check later with -StatusOnly."
            return
        }
        if (((Get-Date) - $lastNote).TotalMinutes -ge 5) { Write-Host "  indexing $($pending -join ', ')..."; $lastNote = Get-Date }
        Start-Sleep -Seconds 30
    }
}

# ------------------------------------------------------------------ main
if ($Trace) {
    $traced = @{ ingest = $ingest; audio = $transcribe; video = $videoLa }[$Trace]
    Show-RunTrace $traced
    return
}
Show-Status
if ($StatusOnly) { return }

# 1. SharePoint -> Blob (documents to kb-, audio to audio-raw-, video to video-raw-<client>)
Write-Host "`n== SharePoint -> Blob" -ForegroundColor Cyan
$ingestRun = Start-OrFollow $ingest
$status = Wait-Run $ingest $ingestRun $TimeoutMinutes
if ($status -eq 'Succeeded') {
    Write-Host "  $ingest : Succeeded" -ForegroundColor Green
} else {
    Write-Host "  $ingest : $status" -ForegroundColor Red
    $null = Show-RunErrors $ingest $ingestRun
    Write-Warning 'Indexing what is already in Blob anyway.'
}

# 2. Index kb-<client>: Document Intelligence pipeline (PDF/Office/images) and native text pipeline.
Write-Host "`n== Indexing kb-$ClientId" -ForegroundColor Cyan
Start-Indexer $ixDi
Start-Indexer $ixText

if ($WithMedia) {
    # 3. Audio runs in the background (one file at a time); the hourly indexer run picks its
    #    transcripts up. Video is waited for, then ix-<client>-text runs once more for its text.
    Write-Host "`n== Audio / video" -ForegroundColor Cyan
    $audioRun = $null
    try { $audioRun = Start-OrFollow $transcribe } catch { Write-Warning "${transcribe}: $($_.Exception.Message)" }
    $videoDone = $false
    try {
        $videoRun = Start-OrFollow $videoLa
        $vs = Wait-Run $videoLa $videoRun $TimeoutMinutes
        if ($vs -eq 'Succeeded') { Write-Host "  $videoLa : Succeeded" -ForegroundColor Green; $videoDone = $true }
        elseif ($vs -in @('Running', 'Waiting')) { Write-Warning "$videoLa still running after $TimeoutMinutes min; the hourly indexer run will pick its text up." }
        else { Write-Host "  $videoLa : $vs" -ForegroundColor Red }
        $null = Show-RunErrors $videoLa $videoRun
    } catch { Write-Warning "${videoLa}: $($_.Exception.Message)" }
    if ($audioRun) {
        Write-Host "  $transcribe : running in the background (one file at a time, indexed by the hourly indexer run)"
        $n = Show-RunErrors $transcribe $audioRun
        if ($n -eq 0) { Write-Host '    no failure so far' }
    }
    Wait-Indexers @($ixText) $TimeoutMinutes
    if ($videoDone) {
        Start-Indexer $ixText
        Wait-Indexers @($ixText) $TimeoutMinutes
    }
    Wait-Indexers @($ixDi) $TimeoutMinutes
} else {
    Wait-Indexers @($ixDi, $ixText) $TimeoutMinutes
}

Show-Status
Write-Host "`nRe-run with -StatusOnly to follow the audio transcription and the indexers." -ForegroundColor Cyan
