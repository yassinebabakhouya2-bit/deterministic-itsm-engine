# deploy-guard.ps1 -- dot-sourced by deploy-webapp.ps1 and scripts/deploy-kecore-function.ps1.
#
# Both scripts package the working tree as it is. On 2026-10-09 four deployments left a clone stuck in
# the middle of a `git revert`: every git command of the pasted block failed, PowerShell went on, and
# the Function and the Web App were deployed from that half-reverted tree (runbook 19.17, 19.18 #2).
# Assert-DeployableTree refuses to package unless what would ship is exactly a commit of origin/main:
#   - no merge / revert / cherry-pick / rebase in progress;
#   - no modified, staged or untracked file under the packaged paths (git-ignored files never count);
#   - HEAD is origin/main after a fetch (-AllowNotMain to ship another commit on purpose).
# -AllowedChanges lists path patterns whose local changes are shipped on purpose (the onboarding scripts
# rewrite config/engine.<client>.yaml and config/itsm.yaml just before deploying the Web App): they are
# printed and recorded, everything else still refuses.
# It returns @{ Commit; LocalChanges }: the scripts write "<commit>" or "<commit>+local-changes" into the
# package (BUILD_COMMIT).
# It throws (never `exit`): a throw stops the whole block an operator pasted, an exit only this script.
# ASCII only: Windows PowerShell 5.1 reads a .ps1 without BOM as ANSI.

function Assert-DeployableTree {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$Paths,
        [string[]]$AllowedChanges = @(),
        [switch]$AllowNotMain
    )
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "git not found: cannot check what would be deployed"
    }
    $inside = git -C $Root rev-parse --is-inside-work-tree
    if ($LASTEXITCODE -ne 0 -or "$inside".Trim() -ne 'true') { throw "$Root is not a git working tree" }

    foreach ($marker in @('MERGE_HEAD', 'REVERT_HEAD', 'CHERRY_PICK_HEAD', 'REBASE_HEAD', 'rebase-merge', 'rebase-apply')) {
        $path = "$(git -C $Root rev-parse --git-path $marker)".Trim()
        if (-not [System.IO.Path]::IsPathRooted($path)) { $path = Join-Path $Root $path }
        if (Test-Path -LiteralPath $path) {
            throw "a git operation is in progress ($marker): finish or abort it before deploying (runbook 19.17)"
        }
    }

    $dirty = @(git -C $Root status --porcelain --untracked-files=all -- $Paths)
    if ($LASTEXITCODE -ne 0) { throw "git status failed" }
    $allowed = @()
    if ($AllowedChanges) {
        # porcelain v1: "XY path" (or "XY old -> new"); a change is allowed when every path it names matches
        $allowed = @($dirty | Where-Object {
            $names = $_.Substring(3) -split ' -> '
            -not @($names | Where-Object { $n = $_.Trim('"'); -not @($AllowedChanges | Where-Object { $n -like $_ }) })
        })
        $dirty = @($dirty | Where-Object { $allowed -notcontains $_ })
        foreach ($line in $allowed) { Write-Warning "shipped with a local change, on purpose: $line" }
    }
    if ($dirty) {
        throw ("uncommitted changes in what would be deployed:`n" + ($dirty -join "`n") +
               "`ncommit them, or restore them (git restore / git clean), then deploy again")
    }

    $head = "$(git -C $Root rev-parse HEAD)".Trim()
    if ($LASTEXITCODE -ne 0 -or -not $head) { throw "git rev-parse HEAD failed" }
    if (-not $AllowNotMain) {
        git -C $Root fetch --quiet origin main
        if ($LASTEXITCODE -ne 0) { throw "git fetch origin main failed: cannot check that HEAD is origin/main" }
        $main = "$(git -C $Root rev-parse origin/main)".Trim()
        if ($head -ne $main) {
            throw ("HEAD ($($head.Substring(0, 7))) is not origin/main ($($main.Substring(0, 7))): " +
                   "run 'git pull --ff-only', or pass -AllowNotMain to ship another commit on purpose")
        }
    }
    $subject = "$(git -C $Root log -1 --format=%s $head)".Trim()
    $suffix = if ($allowed) { ' + local changes listed above' } else { '' }
    Write-Host "Commit to deploy: $($head.Substring(0, 7)) $subject$suffix"
    return [pscustomobject]@{ Commit = $head; LocalChanges = $allowed }
}

function Get-BuildStamp($Tree) {
    if ($Tree.LocalChanges) { return "$($Tree.Commit)+local-changes" }
    return $Tree.Commit
}

# A native command whose stderr is silenced (2>$null) while the script runs with ErrorActionPreference
# 'Stop': Windows PowerShell 5.1 turns redirected stderr lines into errors, which 'Stop' would make fatal.
function Invoke-Quietly([scriptblock]$Command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command 2>$null } finally { $ErrorActionPreference = $previous }
}

function Add-TextEntry([System.IO.Compression.ZipArchive]$Zip, [string]$Name, [string]$Text) {
    $entry = $Zip.CreateEntry($Name)
    $writer = New-Object System.IO.StreamWriter($entry.Open(), (New-Object System.Text.UTF8Encoding($false)))
    try { $writer.Write($Text) } finally { $writer.Dispose() }
}
