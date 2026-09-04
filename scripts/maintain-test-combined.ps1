<#
.SYNOPSIS
    Rebuild origin/test/combined from team/local config and optionally push.

.DESCRIPTION
    Reads team/test-combined.json (team/local by default), fetches base + extras
    from origin, updates the disposable test branch using remote tips only,
    and optionally force-pushes with lease.

    Default (no -Rebuild): if origin/test/combined exists, check it out and
    merge the base branch plus any extras whose tips are not already ancestors
    of HEAD (incremental — keeps prior conflict resolutions). Pass -Rebuild for
    a clean recreate from baseBranch (expect conflicts again).

    The published tip is stamped with its inputs in refs/notes/test-combined,
    which is fetched and pushed with the branch so any machine can tell an
    up-to-date tip from a stale one.

    Local unpushed commits are never merged — tips are always origin/<branch>.
    Missing remote extras abort the run.

    Config resolution (first match wins):
      scripts/test-combined.json          local override (gitignored)
      team/test-combined.json             working tree
      team/local:team/test-combined.json  from team/local via git show
      scripts/test-combined.example.json  fallback

.PARAMETER ConfigPath
    Path or ref:path to a JSON config. Overrides the default search order.

.PARAMETER TeamConfigRef
    Git ref for team/test-combined.json via git show. Default: team/local

.PARAMETER Remote
    Remote name. Default: origin

.PARAMETER Rebuild
    Delete/recreate from baseBranch even when a published tip exists.
    Prefer incremental updates without this switch.

.PARAMETER Abort
    Abort a stuck merge or rebase: hard-reset local test/combined to
    origin/test/combined (if present), clear merge/rebase state, and check out
    the branch the conflicted run started from (recorded in
    .git/fypa-test-combined-return; falls back to the current branch). Works
    from a detached HEAD. Does not push.

.PARAMETER Push
    After a successful update (or reuse), push --force-with-lease to Remote.

.PARAMETER BaseBranch / TestBranch / ExtraFeatureBranches / DeleteTestBranchFirst
    Override individual config fields.

.EXAMPLE
    pwsh scripts/maintain-test-combined.ps1 -Push

    Incremental update from origin/test/combined, then publish.

.EXAMPLE
    pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push

    Clean recreate from main (resolves conflicts from scratch).

.EXAMPLE
    pwsh scripts/maintain-test-combined.ps1 -Abort

    Escape a mid-merge worktree and return to the previous branch.
#>

[CmdletBinding()]
param(
    [string] $ConfigPath,
    [string] $TeamConfigRef = "team/local",
    [string] $Remote = "origin",
    [switch] $Rebuild,
    [switch] $Abort,
    [switch] $Push,
    [string] $BaseBranch,
    [string] $TestBranch,
    [string[]] $ExtraFeatureBranches,
    [bool] $DeleteTestBranchFirst
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

# Invoke-GitCore / Invoke-Git / Invoke-GitSoft / Get-CurrentBranch / Test-GitRef
# / Get-RefSha and the worktree-preservation helpers.
. (Join-Path $PSScriptRoot '_git-helpers.ps1')

# Notes ref carrying the input stamp of each published tip. Shared, so it has
# to travel with the branch — see Sync-StampNotes / Publish-StampNotes.
$StampNotesRef = 'refs/notes/test-combined'

function Test-GitAncestor {
    param(
        [string] $Ancestor,
        [string] $Descendant
    )
    return (Invoke-GitSoft -Quiet @(
        'merge-base', '--is-ancestor', $Ancestor, $Descendant
    )) -eq 0
}

function Sync-RemoteBranches {
    param(
        [string] $RemoteName,
        [string[]] $Branches
    )

    $UniqueBranches = @($Branches | Where-Object { $_ } | Select-Object -Unique)
    if ($UniqueBranches.Count -eq 0) {
        return
    }

    Write-Host "==> Fetch $RemoteName $($UniqueBranches -join ', ')"
    $Result = Invoke-GitCore -Quiet @(@('fetch', $RemoteName) + $UniqueBranches)
    if ($Result.ExitCode -ne 0) {
        $Detail = ($Result.Output -join "`n").Trim()
        if ($Detail) {
            throw "git fetch $RemoteName failed (exit $($Result.ExitCode)): $Detail"
        }
        throw "git fetch $RemoteName failed (exit $($Result.ExitCode))"
    }
}

function Resolve-RemoteTip {
    param(
        [string] $Branch,
        [string] $RemoteName
    )

    $RemoteRef = "$RemoteName/$Branch"
    if (-not (Test-GitRef "refs/remotes/$RemoteName/$Branch")) {
        return $null
    }
    $Sha = Get-RefSha -Ref $RemoteRef
    if (-not $Sha) { return $null }
    return @{
        Branch   = $Branch
        MergeRef = $RemoteRef
        Sha      = $Sha
        Source   = 'remote'
    }
}

function Get-InputStamp {
    param(
        [string] $ConfigIdentity,
        [string] $BaseName,
        [string] $BaseSha,
        [string[]] $ExtraPairs
    )

    $Parts = [System.Collections.Generic.List[string]]::new()
    $Parts.Add("config=$ConfigIdentity")
    $Parts.Add("base=$BaseName=$BaseSha")
    foreach ($Pair in $ExtraPairs) {
        if ($Pair) { $Parts.Add("extra=$Pair") }
    }
    return ($Parts -join '|')
}

function Get-TestCombinedStamp {
    param([string] $Commit)
    if (-not $Commit) { return $null }
    $Result = Invoke-GitCore -Quiet @('notes', "--ref=$StampNotesRef", 'show', $Commit)
    if ($Result.ExitCode -ne 0) {
        return $null
    }
    return (($Result.Output -join "`n").Trim())
}

function Set-TestCombinedStamp {
    param(
        [string] $Commit,
        [string] $Stamp
    )
    $ExitCode = Invoke-GitSoft @(
        'notes', "--ref=$StampNotesRef", 'add', '-f', '-m', $Stamp, $Commit
    )
    if ($ExitCode -ne 0) {
        Write-Warning "Could not write test-combined stamp note on $Commit"
    }
}

function Sync-StampNotes {
    <#
        Fetch the shared stamp notes. Without this the reuse fast path only ever
        engages on the machine that built the branch: notes live in their own
        ref, which a plain `git fetch <remote> <branch>` does not bring down, so
        a CI runner or a second maintainer would rebuild from scratch every run.
    #>
    param([string] $RemoteName)
    $ExitCode = Invoke-GitSoft -Quiet @(
        'fetch', $RemoteName, "+${StampNotesRef}:${StampNotesRef}"
    )
    if ($ExitCode -ne 0) {
        Write-Host "==> No published stamp notes on $RemoteName yet"
    }
}

function Publish-StampNotes {
    <# Push the stamp notes so other machines can reuse the published tip. #>
    param([string] $RemoteName)
    $ExitCode = Invoke-GitSoft @(
        'push', '--force', $RemoteName, "${StampNotesRef}:${StampNotesRef}"
    )
    if ($ExitCode -ne 0) {
        Write-Warning @"
Could not push $StampNotesRef to $RemoteName. The branch is published, but
other machines will rebuild it from scratch instead of reusing this tip.
"@
    }
    else {
        Write-Host "==> Pushed $StampNotesRef"
    }
}

function ConvertTo-NormalizedStamp {
    param([string] $Stamp)
    if (-not $Stamp) { return $null }
    $Normalized = $Stamp.Trim() -replace "`r`n", "`n" -replace "`r", "`n"
    if ($Normalized.Contains("`n")) {
        $Normalized = (($Normalized -split "`n") | ForEach-Object { $_.Trim() } | Where-Object { $_ }) -join '|'
    }
    return $Normalized
}

function Test-MergeInProgress {
    $Result = Invoke-GitCore -Quiet @('rev-parse', '-q', '--verify', 'MERGE_HEAD')
    return ($Result.ExitCode -eq 0 -and @($Result.Output | Where-Object { $_ }).Count -gt 0)
}

function Get-UnmergedPaths {
    $Result = Invoke-GitCore -Quiet @('diff', '--name-only', '--diff-filter=U')
    if ($Result.ExitCode -ne 0) {
        return @()
    }
    return @($Result.Output | Where-Object { $_ })
}

function Get-GitDir {
    $Result = Invoke-GitCore -Quiet @('rev-parse', '--git-dir')
    if ($Result.ExitCode -ne 0) { return $null }
    return (Get-FirstLine $Result.Output)
}

function Test-RebaseInProgress {
    $GitDir = Get-GitDir
    if (-not $GitDir) { return $false }
    return (
        (Test-Path -LiteralPath (Join-Path $GitDir 'rebase-merge')) -or
        (Test-Path -LiteralPath (Join-Path $GitDir 'rebase-apply'))
    )
}

# Where the pre-run branch is parked when a merge conflict leaves the worktree
# on the test branch, so -Abort can honour its promise to put you back. Lives
# in .git/, not the worktree, so it never shows up in `git status`.
function Get-ReturnBranchMemoPath {
    $GitDir = Get-GitDir
    if (-not $GitDir) { return $null }
    return (Join-Path $GitDir 'fypa-test-combined-return')
}

function Set-ReturnBranchMemo {
    param([string] $Branch)
    $Path = Get-ReturnBranchMemoPath
    if (-not $Path -or -not $Branch) { return }
    try { Set-Content -LiteralPath $Path -Value $Branch -Encoding ascii -NoNewline }
    catch { Write-Warning "Could not record the return branch: $_" }
}

function Get-ReturnBranchMemo {
    $Path = Get-ReturnBranchMemoPath
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $null }
    $Raw = Get-Content -LiteralPath $Path -Raw
    $Branch = if ($null -eq $Raw) { '' } else { ([string] $Raw).Trim() }
    if (-not $Branch) { return $null }
    return $Branch
}

function Clear-ReturnBranchMemo {
    $Path = Get-ReturnBranchMemoPath
    if ($Path) { Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue }
}

function Resolve-IgnoredMergeConflicts {
    param(
        [string[]] $IgnoredPaths,
        [ValidateSet('ours', 'theirs')]
        [string] $Prefer = 'ours'
    )

    foreach ($Path in (Get-UnmergedPaths)) {
        if ($Path -in $IgnoredPaths) {
            Write-Host "==> Auto-resolve merge conflict in $Path ($Prefer)"
            Invoke-Git @('checkout', "--$Prefer", '--', $Path)
            Invoke-Git @('add', '--', $Path)
        }
    }

    return @(Get-UnmergedPaths)
}

function Merge-FeatureBranch {
    param(
        [string] $MergeRef,
        [string] $ExtraBranch,
        [string[]] $IgnoredPaths
    )

    $MergeMessage = "test: merge $ExtraBranch for combined testing"
    $ExitCode = Invoke-GitSoft @(
        'merge', $MergeRef, '--no-edit', '-m', $MergeMessage
    )
    if ($ExitCode -eq 0) {
        return
    }

    if (-not (Test-MergeInProgress)) {
        throw "git merge $MergeRef failed (exit $ExitCode)"
    }

    $Remaining = Resolve-IgnoredMergeConflicts -IgnoredPaths $IgnoredPaths -Prefer 'ours'
    if ($Remaining.Count -gt 0) {
        throw "Merge conflict in: $($Remaining -join ', ')"
    }

    Invoke-Git @('commit', '--no-edit')
}

function Restore-DevBranch {
    param([string] $Branch)
    if ($Branch) {
        Invoke-Git @('checkout', $Branch)
    }
}

function Get-GitConfigJson {
    param(
        [string[]] $Refs,
        [string] $RepoPath = "team/test-combined.json"
    )

    foreach ($Ref in $Refs) {
        if (-not $Ref) { continue }
        $Spec = "${Ref}:${RepoPath}"
        $Result = Invoke-GitCore -Quiet @('show', $Spec)
        if ($Result.ExitCode -eq 0 -and $Result.Output.Count -gt 0) {
            return @{ Source = $Spec; Json = ($Result.Output -join "`n") }
        }
    }

    return $null
}

function Resolve-ConfigSource {
    param(
        [string] $ExplicitPath,
        [string] $TeamRef
    )

    if ($ExplicitPath) {
        if (Test-Path $ExplicitPath) {
            return @{
                Source = (Resolve-Path $ExplicitPath).Path
                Json   = $null
            }
        }
        if ($ExplicitPath -match ':') {
            $Result = Invoke-GitCore -Quiet @('show', $ExplicitPath)
            if ($Result.ExitCode -eq 0 -and $Result.Output.Count -gt 0) {
                return @{ Source = $ExplicitPath; Json = ($Result.Output -join "`n") }
            }
        }
        throw "Config file not found: $ExplicitPath"
    }

    $LocalCandidates = @(
        (Join-Path $RepoRoot "scripts/test-combined.json"),
        (Join-Path $RepoRoot "team/test-combined.json")
    )

    foreach ($Candidate in $LocalCandidates) {
        if (Test-Path $Candidate) {
            return @{
                Source = (Resolve-Path $Candidate).Path
                Json   = $null
            }
        }
    }

    $GitRefs = @(
        $TeamRef,
        "origin/$TeamRef"
    )
    $FromGit = Get-GitConfigJson -Refs $GitRefs
    if ($FromGit) {
        return $FromGit
    }

    $Example = Join-Path $RepoRoot "scripts/test-combined.example.json"
    if (Test-Path $Example) {
        Write-Warning "Using example config ($Example). Copy to scripts/test-combined.json or update team/local."
        return @{
            Source = (Resolve-Path $Example).Path
            Json   = $null
        }
    }

    throw @"
No test-combined config found.
Fetch team/local (git fetch origin team/local) or create scripts/test-combined.json from scripts/test-combined.example.json.
"@
}

function Read-TestCombinedConfig {
    param(
        [string] $Source,
        [string] $Json
    )

    try {
        if ($Json) {
            $Config = $Json | ConvertFrom-Json
        }
        else {
            $Config = Get-Content -Raw -Path $Source | ConvertFrom-Json
        }
    }
    catch {
        throw "Failed to parse config JSON at '$Source': $_"
    }

    foreach ($Required in @("baseBranch", "testBranch", "extraFeatureBranches")) {
        if (-not ($Config.PSObject.Properties.Name -contains $Required)) {
            throw "Config '$Source' is missing required field '$Required'."
        }
    }

    return $Config
}

function Clear-TestCombinedUpstream {
    param([string] $Branch)
    # Creating from origin/main sets upstream to main — confusing ("ahead of main").
    # Fails loudly when there is no upstream to clear, which is a normal state
    # here, so the exit code is discarded rather than allowed to surface.
    Invoke-GitSoft -Quiet @('branch', '--unset-upstream', $Branch) | Out-Null
}

if (-not (Test-Path "FYPA.py")) {
    throw "FYPA.py not found in $RepoRoot — run this script from the FYPA repo."
}

$ReturnBranch = Get-CurrentBranch
if (-not $ReturnBranch -and -not $Abort) {
    # -Abort is exempt: a detached HEAD is the stuck state it exists to escape,
    # so throwing here would make the documented escape hatch unusable.
    throw "Could not determine the current branch (detached HEAD?). Check out a branch first."
}

if ($Abort) {
    $AbortTestBranch = if ($PSBoundParameters.ContainsKey("TestBranch") -and $TestBranch) {
        $TestBranch
    }
    else {
        "test/combined"
    }
    Write-Host "==> Abort: clear merge/rebase state and reset $AbortTestBranch"
    # A conflicted run parks the branch you started on; prefer it over "whatever
    # branch I happen to be on now", which on the abort run is the test branch.
    $MemoBranch = Get-ReturnBranchMemo
    if ($MemoBranch) {
        $ReturnBranch = $MemoBranch
        Write-Host "==> Recorded return branch: $ReturnBranch"
    }
    $onAbortBranch = ((Get-CurrentBranch) -eq $AbortTestBranch)
    # An interrupted rebase leaves HEAD detached, so `-eq $AbortTestBranch` is
    # false exactly when the state most needs clearing. Treat detached-with-
    # rebase-state as ours too; a detached HEAD with no rebase in progress is
    # someone else's business and is left alone.
    $DetachedMidRebase = ((-not (Get-CurrentBranch)) -and (Test-RebaseInProgress))
    if ($onAbortBranch -or $DetachedMidRebase) {
        Invoke-GitSoft -Quiet @('merge', '--abort') | Out-Null
        Invoke-GitSoft -Quiet @('rebase', '--abort') | Out-Null
        Invoke-GitSoft -Quiet @('reset', '--merge') | Out-Null
        # rebase --abort lands us back on the pre-rebase branch.
        $onAbortBranch = ((Get-CurrentBranch) -eq $AbortTestBranch)
    }
    if (-not $ReturnBranch) {
        $ReturnBranch = Get-CurrentBranch
    }
    try {
        Sync-RemoteBranches -RemoteName $Remote -Branches @($AbortTestBranch)
    }
    catch {
        Write-Warning "Fetch $Remote/$AbortTestBranch failed — using existing refs if present."
    }
    $RemoteAbortRef = "$Remote/$AbortTestBranch"
    if (Test-GitRef "refs/remotes/$Remote/$AbortTestBranch") {
        if ($onAbortBranch) {
            Invoke-Git @('reset', '--hard', $RemoteAbortRef)
            Clear-TestCombinedUpstream -Branch $AbortTestBranch
            Write-Host "==> $AbortTestBranch reset to $RemoteAbortRef"
            # The single return-to-$ReturnBranch step is at the end of the
            # block, so it also covers the paths that reach here detached.
        }
        else {
            # Update the local branch tip without checking it out (worktree may be dirty).
            if (Test-GitRef "refs/heads/$AbortTestBranch") {
                Invoke-Git @('branch', '-f', $AbortTestBranch, $RemoteAbortRef)
            }
            else {
                Invoke-Git @('branch', $AbortTestBranch, $RemoteAbortRef)
            }
            Clear-TestCombinedUpstream -Branch $AbortTestBranch
            Write-Host "==> Local $AbortTestBranch forced to $RemoteAbortRef (no checkout)"
        }
    }
    elseif ($onAbortBranch) {
        Write-Host "==> No $RemoteAbortRef — checking out $ReturnBranch and deleting local tip"
        Restore-DevBranch -Branch $ReturnBranch
        if (Test-GitRef "refs/heads/$AbortTestBranch") {
            Invoke-Git @('branch', '-D', $AbortTestBranch)
        }
    }
    else {
        Write-Host "==> No local/remote $AbortTestBranch to reset"
    }
    $AbortCurrent = Get-CurrentBranch
    if ($ReturnBranch -and $AbortCurrent -ne $ReturnBranch) {
        Write-Host "==> Return to $ReturnBranch"
        Restore-DevBranch -Branch $ReturnBranch
        $AbortCurrent = Get-CurrentBranch
    }
    Clear-ReturnBranchMemo
    Write-Host "==> Abort done — on $AbortCurrent"
    exit 0
}

# Soft-fetch team config ref so git show origin/team/local:... works.
if (-not $ConfigPath) {
    try {
        Sync-RemoteBranches -RemoteName $Remote -Branches @($TeamConfigRef)
    }
    catch {
        Write-Warning "Fetch $Remote $TeamConfigRef failed; using existing refs if present."
        Write-Warning "$_"
    }
}

$ConfigSource = Resolve-ConfigSource -ExplicitPath $ConfigPath -TeamRef $TeamConfigRef
Write-Host "==> Config: $($ConfigSource.Source)"
$Config = Read-TestCombinedConfig -Source $ConfigSource.Source -Json $ConfigSource.Json

$BaseBranch = if ($PSBoundParameters.ContainsKey("BaseBranch")) { $BaseBranch } else { [string] $Config.baseBranch }
$TestBranch = if ($PSBoundParameters.ContainsKey("TestBranch")) { $TestBranch } else { [string] $Config.testBranch }
$ExtraFeatureBranches = if ($PSBoundParameters.ContainsKey("ExtraFeatureBranches")) {
    $ExtraFeatureBranches
}
else {
    @($Config.extraFeatureBranches | ForEach-Object { [string] $_ })
}
$DeleteTestBranchFirst = if ($PSBoundParameters.ContainsKey("DeleteTestBranchFirst")) {
    $DeleteTestBranchFirst
}
elseif ($Config.PSObject.Properties.Name -contains "deleteTestBranchFirst") {
    [bool] $Config.deleteTestBranchFirst
}
else {
    $false
}

if (-not $BaseBranch) { throw "baseBranch is empty." }
if (-not $TestBranch) { throw "testBranch is empty." }

Write-Host "==> Branch source: $Remote tips only (no local-ahead merge)"

Sync-RemoteBranches -RemoteName $Remote -Branches (@($BaseBranch) + $ExtraFeatureBranches)
# test/combined may not exist yet on first publish — soft-fetch only.
try {
    Sync-RemoteBranches -RemoteName $Remote -Branches @($TestBranch)
}
catch {
    Write-Host "==> $Remote/$TestBranch not fetched yet (ok on first publish)"
}

$BaseTarget = Resolve-RemoteTip -Branch $BaseBranch -RemoteName $Remote
if (-not $BaseTarget) {
    throw "Base branch '$BaseBranch' not found as $Remote/$BaseBranch. Push it first."
}

$BaseRef = $BaseTarget.MergeRef
$BaseSha = $BaseTarget.Sha
Write-Host "==> Base: $BaseRef"

$ExtraStampPairs = [System.Collections.Generic.List[string]]::new()
$ResolvedExtras = [System.Collections.Generic.List[hashtable]]::new()
$MissingExtras = [System.Collections.Generic.List[string]]::new()
foreach ($ExtraBranch in $ExtraFeatureBranches) {
    if (-not $ExtraBranch) { continue }
    $ExtraTarget = Resolve-RemoteTip -Branch $ExtraBranch -RemoteName $Remote
    if (-not $ExtraTarget) {
        $MissingExtras.Add($ExtraBranch)
        continue
    }
    Write-Host "==> Extra: $($ExtraTarget.MergeRef)"
    $ExtraStampPairs.Add("$ExtraBranch=$($ExtraTarget.Sha)")
    $ResolvedExtras.Add(@{
        Branch   = $ExtraTarget.Branch
        MergeRef = $ExtraTarget.MergeRef
        Sha      = $ExtraTarget.Sha
    })
}

if ($MissingExtras.Count -gt 0) {
    throw @"
Missing on ${Remote}: $($MissingExtras -join ', ').
Push each feature branch before maintaining $TestBranch.
"@
}

$ConfigIdentity = @(
    "base=$BaseBranch",
    "test=$TestBranch",
    "deleteFirst=$DeleteTestBranchFirst",
    "extras=$($ExtraFeatureBranches -join ',')"
) -join ';'

$DesiredStamp = ConvertTo-NormalizedStamp (Get-InputStamp `
    -ConfigIdentity $ConfigIdentity `
    -BaseName $BaseBranch `
    -BaseSha $BaseSha `
    -ExtraPairs @($ExtraStampPairs))

$RemoteTestRef = "$Remote/$TestBranch"
$HasRemoteTest = Test-GitRef "refs/remotes/$Remote/$TestBranch"
$RemoteTestSha = if ($HasRemoteTest) { Get-RefSha -Ref $RemoteTestRef } else { $null }
if ($RemoteTestSha) {
    Sync-StampNotes -RemoteName $Remote
}
$RemoteStamp = ConvertTo-NormalizedStamp (Get-TestCombinedStamp -Commit $RemoteTestSha)

$CanReuse = (
    -not $Rebuild -and
    $RemoteStamp -and
    ($RemoteStamp -eq $DesiredStamp)
)

$UseIncremental = (
    -not $Rebuild -and
    -not $CanReuse -and
    $HasRemoteTest
)

if ($CanReuse) {
    Write-Host "==> Stamp matches $RemoteTestRef — reuse"
}
elseif ($UseIncremental) {
    Write-Host "==> Incremental update from $RemoteTestRef (pass -Rebuild for clean recreate)"
}
elseif ($Rebuild) {
    Write-Host "==> -Rebuild: clean recreate from $BaseRef"
}
else {
    Write-Host "==> No $RemoteTestRef yet — first create from $BaseRef"
}

$IgnoredPaths = $script:CombinedIgnoredPaths
try {
    $DirtyIgnored = Assert-CleanWorktree -IgnoredPaths $IgnoredPaths `
        -Context "maintain-test-combined"
}
catch {
    throw @"
$_
To escape a stuck merge: pwsh scripts/maintain-test-combined.ps1 -Abort
"@
}
# The gate waves those paths through; the checkout/reset below would then wipe
# them without a word. Snapshot now, restore once we are back on $ReturnBranch.
$IgnoredBackup = Backup-WorktreePath -Paths $DirtyIgnored

$LeaveOnConflict = $false
try {
    if ($CanReuse) {
        Reset-ToRemoteTip -Branch $TestBranch -RemoteRef $RemoteTestRef
        Clear-TestCombinedUpstream -Branch $TestBranch
    }
    elseif ($UseIncremental) {
        Reset-ToRemoteTip -Branch $TestBranch -RemoteRef $RemoteTestRef
        Clear-TestCombinedUpstream -Branch $TestBranch

        $HeadSha = Get-RefSha -Ref 'HEAD'
        # The base has to be merged before the extras, and before the stamp is
        # written. The stamp records the base SHA, so skipping this step would
        # mark the tip as built from a base it has never seen: every later run
        # would compute the same stamp, take the reuse path, and republish a
        # branch quietly missing everything main gained in the meantime.
        if (-not (Test-GitAncestor -Ancestor $BaseSha -Descendant $HeadSha)) {
            Write-Host "==> Merge $BaseRef into $TestBranch"
            Merge-FeatureBranch -MergeRef $BaseRef -ExtraBranch $BaseBranch -IgnoredPaths $IgnoredPaths
            $HeadSha = Get-RefSha -Ref 'HEAD'
        }
        else {
            Write-Host "==> Skip $BaseBranch (already in $TestBranch)"
        }

        foreach ($Extra in $ResolvedExtras) {
            if (Test-GitAncestor -Ancestor $Extra.Sha -Descendant $HeadSha) {
                Write-Host "==> Skip $($Extra.Branch) (already in $TestBranch)"
                continue
            }
            Write-Host "==> Merge $($Extra.MergeRef) into $TestBranch"
            Merge-FeatureBranch -MergeRef $Extra.MergeRef -ExtraBranch $Extra.Branch -IgnoredPaths $IgnoredPaths
            $HeadSha = Get-RefSha -Ref 'HEAD'
        }

        $NewTip = Get-RefSha -Ref 'HEAD'
        if ($NewTip) {
            Set-TestCombinedStamp -Commit $NewTip -Stamp $DesiredStamp
            Write-Host "==> Stamp written for $TestBranch"
        }
    }
    else {
        # Full recreate from base (first publish or -Rebuild).
        if ($DeleteTestBranchFirst -and (Test-GitRef "refs/heads/$TestBranch")) {
            Write-Host "==> Delete $TestBranch"
            if ((Get-CurrentBranch) -eq $TestBranch) {
                Invoke-Git @('checkout', $ReturnBranch)
            }
            Invoke-Git @('branch', '-D', $TestBranch)
        }

        if (Test-GitRef "refs/heads/$TestBranch") {
            Write-Host "==> Recreate $TestBranch from $BaseRef"
            Invoke-Git @('branch', '-f', $TestBranch, $BaseRef)
            Invoke-Git @('checkout', $TestBranch)
        }
        else {
            Write-Host "==> Create $TestBranch from $BaseRef"
            Invoke-Git @('checkout', '-b', $TestBranch, $BaseRef)
        }
        Clear-TestCombinedUpstream -Branch $TestBranch

        foreach ($Extra in $ResolvedExtras) {
            Write-Host "==> Merge $($Extra.MergeRef) into $TestBranch"
            Merge-FeatureBranch -MergeRef $Extra.MergeRef -ExtraBranch $Extra.Branch -IgnoredPaths $IgnoredPaths
        }

        $NewTip = Get-RefSha -Ref 'HEAD'
        if ($NewTip) {
            Set-TestCombinedStamp -Commit $NewTip -Stamp $DesiredStamp
            Write-Host "==> Stamp written for $TestBranch"
        }
    }

    $Tip = Get-RefSha -Ref 'HEAD'
    Write-Host "==> $TestBranch tip: $Tip"

    if ($Push) {
        Write-Host "==> Push --force-with-lease $Remote $TestBranch"
        Invoke-Git @('push', '--force-with-lease', $Remote, "HEAD:refs/heads/$TestBranch")
        Write-Host "==> Pushed $Remote/$TestBranch"
        # The stamp is what lets the next machine reuse this tip instead of
        # rebuilding it, so it has to be published alongside the branch.
        Publish-StampNotes -RemoteName $Remote
    }
    else {
        Write-Host "==> Local only (pass -Push to update $Remote/$TestBranch)"
    }
}
catch {
    $msg = "$_"
    if ($msg -match 'Merge conflict' -and (
            (Test-MergeInProgress) -or ((Get-UnmergedPaths).Count -gt 0)
        )) {
        $LeaveOnConflict = $true
        # Remember where the maintainer came from: -Abort runs as a separate
        # invocation, from the test branch, and otherwise has no way to know.
        Set-ReturnBranchMemo -Branch $ReturnBranch
        Write-Host @"

==> Merge conflict — still on $TestBranch (do not git switch away).

Finish:
  git add -u
  git commit --no-edit
  pwsh scripts/maintain-test-combined.ps1 -Push

Abort back to published tip + previous branch:
  pwsh scripts/maintain-test-combined.ps1 -Abort
"@
    }
    elseif ((Get-CurrentBranch) -ne $ReturnBranch) {
        Invoke-GitSoft -Quiet @('merge', '--abort') | Out-Null
        Invoke-GitSoft -Quiet @('rebase', '--abort') | Out-Null
    }
    throw
}
finally {
    if ($LeaveOnConflict) {
        if ($IgnoredBackup) {
            Write-Host "==> Local edits to $($IgnoredBackup.Files.Keys -join ', ') kept at $($IgnoredBackup.Dir)"
        }
    }
    else {
        Clear-ReturnBranchMemo
        if ((Get-CurrentBranch) -ne $ReturnBranch) {
            Write-Host "==> Return to $ReturnBranch"
            Restore-DevBranch -Branch $ReturnBranch
        }
        Restore-WorktreePath -Backup $IgnoredBackup
    }
}
