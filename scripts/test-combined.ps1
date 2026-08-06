<#
.SYNOPSIS
    Build a local combined test branch, run tests/FYPA, then switch back.

.DESCRIPTION
    Merges a base branch plus feature branches into a disposable test branch,
    optionally runs the pytest topology suite and FYPA.py, then returns to the
    branch you started on (even if a step exits with an error).

    Config resolution (first match wins):
      scripts/test-combined.json          local override (gitignored)
      team/test-combined.json             working tree
      team/local:team/test-combined.json  from team/local via git show (no checkout)
      scripts/test-combined.example.json  fallback

    By default baseBranch and extraFeatureBranches are soft-fetched from origin.
    Each input is resolved to the tip that includes local work: if the local
    branch is ahead of (or diverged from) origin/<branch>, the local tip is
    merged; if local is behind, origin/<branch> is used; local-only or
    remote-only branches are accepted either way. If fetch fails (offline),
    existing refs are resolved the same way.

    When input SHAs match the stamp on an existing test branch, that branch is
    reused instead of rebuilt. Pass -Rebuild to force a clean recreate.
    Pass -LocalOnly to skip fetch and use local branches only.

    Workflow:
      1. Remember current branch
      2. Soft-fetch inputs (or local-only); resolve tips (prefer local when ahead);
         reuse test branch if stamp matches
      3. Otherwise optionally delete, recreate from base, merge feature branches
         (.gitignore conflicts auto-resolved with --ours); write stamp note
      4. Optionally run pytest topology suite (-SkipTests to skip), then uv run FYPA.py
      5. Return to the starting branch

.PARAMETER ConfigPath
    Path to a JSON config file. Overrides the default config search order.

.PARAMETER TeamConfigRef
    Git ref used when reading team/test-combined.json via git show.
    Default: team/local

.PARAMETER Remote
    Remote name used for soft-fetch and tip resolution. Default: origin

.PARAMETER LocalOnly
    Skip fetch; resolve and merge local branches only.

.PARAMETER Rebuild
    Force delete/recreate of the test branch even when the stamp matches.

.PARAMETER SkipTests
    Skip the pytest topology suite; still runs FYPA.py unless the script exits earlier.

.PARAMETER BaseBranch
    Override config baseBranch (branch the test branch is created from).

.PARAMETER TestBranch
    Override config testBranch (name of the disposable combined branch).

.PARAMETER ExtraFeatureBranches
    Override config extraFeatureBranches (branches merged onto the base).

.PARAMETER DeleteTestBranchFirst
    Override config deleteTestBranchFirst. When true, delete the existing test
    branch before recreating it.

.PARAMETER PrjPcb
    Path to a .PrjPcb passed through to FYPA.py.

.EXAMPLE
    pwsh scripts/test-combined.ps1

    Use default config resolution, soft-fetch, reuse or rebuild the test branch,
    run tests and FYPA, then switch back.

.EXAMPLE
    pwsh scripts/test-combined.ps1 -LocalOnly

    Skip remote fetch; use local branch tips only.

.EXAMPLE
    pwsh scripts/test-combined.ps1 -Rebuild

    Force a clean recreate of the test branch.

.EXAMPLE
    pwsh scripts/test-combined.ps1 -SkipTests

    Build/reuse the test branch and run FYPA without pytest.

.EXAMPLE
    pwsh scripts/test-combined.ps1 -ConfigPath scripts/test-combined.json

    Use an explicit local config file.

.EXAMPLE
    pwsh scripts/test-combined.ps1 -PrjPcb path\to\YourBoard.PrjPcb

    Pass a project file through to FYPA.py.

.EXAMPLE
    Get-Help .\scripts\test-combined.ps1 -Full

    Show this help. Equivalent: pwsh scripts/test-combined.ps1 -?

.NOTES
    Run from the repo root (or any branch); the script cds to the repo root itself.
#>

[CmdletBinding()]
param(
    [string] $ConfigPath,
    [string] $TeamConfigRef = "team/local",
    [string] $Remote = "origin",
    [switch] $LocalOnly,
    [switch] $Rebuild,
    [switch] $SkipTests,
    [string] $BaseBranch,
    [string] $TestBranch,
    [string[]] $ExtraFeatureBranches,
    [bool] $DeleteTestBranchFirst,
    [string] $PrjPcb
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

function Invoke-GitCore {
    param(
        [Parameter(Mandatory, ValueFromRemainingArguments)]
        [string[]] $GitArgs,
        [switch] $Quiet
    )
    if ($GitArgs.Count -eq 0) {
        throw "Invoke-GitCore: no arguments"
    }

    $Output = @(& git.exe @GitArgs 2>&1)
    $ExitCode = $LASTEXITCODE

    if (-not $Quiet) {
        foreach ($Line in $Output) {
            if ($Line -is [System.Management.Automation.ErrorRecord]) {
                Write-Warning $Line.ToString()
            }
            else {
                Write-Host $Line
            }
        }
    }

    $Stdout = @(
        $Output |
            Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } |
            ForEach-Object { [string] $_ }
    )

    return @{
        ExitCode = $ExitCode
        Output   = $Stdout
    }
}

function Invoke-Git {
    param(
        [Parameter(Mandatory, ValueFromRemainingArguments)]
        [string[]] $GitArgs
    )
    $Result = Invoke-GitCore @GitArgs
    if ($Result.ExitCode -ne 0) {
        throw "git $($GitArgs -join ' ') failed (exit $($Result.ExitCode))"
    }
    return $Result.Output
}

function Invoke-GitSoft {
    param(
        [Parameter(Mandatory, ValueFromRemainingArguments)]
        [string[]] $GitArgs
    )
    return (Invoke-GitCore @GitArgs).ExitCode
}

function Test-GitRef {
    param([string] $Ref)
    & git show-ref --verify --quiet $Ref
    return $LASTEXITCODE -eq 0
}

function Test-GitAncestor {
    param(
        [string] $Ancestor,
        [string] $Descendant
    )
    & git.exe merge-base --is-ancestor $Ancestor $Descendant 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function Resolve-BranchMergeTarget {
    param(
        [string] $Branch,
        [string] $RemoteName,
        [bool] $UseLocalOnly
    )

    $LocalRef = $Branch
    $RemoteRef = "$RemoteName/$Branch"
    $HasLocal = Test-GitRef "refs/heads/$Branch"
    $HasRemote = Test-GitRef "refs/remotes/$RemoteName/$Branch"

    if ($UseLocalOnly) {
        if (-not $HasLocal) { return $null }
        $Sha = Get-RefSha -Ref $LocalRef
        if (-not $Sha) { return $null }
        return @{
            Branch   = $Branch
            MergeRef = $LocalRef
            Sha      = $Sha
            Source   = 'local'
        }
    }

    if ($HasLocal -and $HasRemote) {
        $LocalSha = Get-RefSha -Ref $LocalRef
        $RemoteSha = Get-RefSha -Ref $RemoteRef
        if (-not $LocalSha -and -not $RemoteSha) { return $null }
        if (-not $LocalSha) {
            return @{
                Branch   = $Branch
                MergeRef = $RemoteRef
                Sha      = $RemoteSha
                Source   = 'remote'
            }
        }
        if (-not $RemoteSha) {
            return @{
                Branch   = $Branch
                MergeRef = $LocalRef
                Sha      = $LocalSha
                Source   = 'local'
            }
        }
        if ($LocalSha -eq $RemoteSha) {
            return @{
                Branch   = $Branch
                MergeRef = $LocalRef
                Sha      = $LocalSha
                Source   = 'local'
            }
        }
        # Local contains remote → unpushed local commits; keep them.
        if (Test-GitAncestor -Ancestor $RemoteSha -Descendant $LocalSha) {
            Write-Host "==> ${Branch}: using local (ahead of $RemoteRef)"
            return @{
                Branch   = $Branch
                MergeRef = $LocalRef
                Sha      = $LocalSha
                Source   = 'local'
            }
        }
        # Remote contains local → local checkout is stale; take remote.
        if (Test-GitAncestor -Ancestor $LocalSha -Descendant $RemoteSha) {
            return @{
                Branch   = $Branch
                MergeRef = $RemoteRef
                Sha      = $RemoteSha
                Source   = 'remote'
            }
        }
        # Diverged: never drop local work.
        Write-Warning "${Branch}: local and $RemoteRef have diverged — using local tip"
        return @{
            Branch   = $Branch
            MergeRef = $LocalRef
            Sha      = $LocalSha
            Source   = 'local'
        }
    }

    if ($HasLocal) {
        $Sha = Get-RefSha -Ref $LocalRef
        if (-not $Sha) { return $null }
        Write-Host "==> ${Branch}: no $RemoteRef — using local"
        return @{
            Branch   = $Branch
            MergeRef = $LocalRef
            Sha      = $Sha
            Source   = 'local'
        }
    }

    if ($HasRemote) {
        $Sha = Get-RefSha -Ref $RemoteRef
        if (-not $Sha) { return $null }
        return @{
            Branch   = $Branch
            MergeRef = $RemoteRef
            Sha      = $Sha
            Source   = 'remote'
        }
    }

    return $null
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
    # git writes progress to stderr; don't surface it as PowerShell warnings.
    $Result = Invoke-GitCore -Quiet @(@('fetch', $RemoteName) + $UniqueBranches)
    if ($Result.ExitCode -ne 0) {
        $Detail = ($Result.Output -join "`n").Trim()
        if ($Detail) {
            throw "git fetch $RemoteName failed (exit $($Result.ExitCode)): $Detail"
        }
        throw "git fetch $RemoteName failed (exit $($Result.ExitCode))"
    }
}

function Get-RefSha {
    param([string] $Ref)
    $Sha = ([string] (& git.exe rev-parse --verify "$Ref^{commit}" 2>$null)).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $Sha) {
        return $null
    }
    return $Sha
}

function Get-InputStamp {
    param(
        [string] $ConfigIdentity,
        [string] $BaseName,
        [string] $BaseSha,
        [string[]] $ExtraPairs
    )

    # Single-line stamp: PowerShell [string] casts of multi-line git output join
    # with spaces and would break equality checks if we used newlines.
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
    $Lines = @(& git.exe notes --ref=test-combined show $Commit 2>$null)
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    # Join exactly as written; Trim only outer whitespace.
    return (($Lines -join "`n").Trim())
}

function Set-TestCombinedStamp {
    param(
        [string] $Commit,
        [string] $Stamp
    )
    $ExitCode = Invoke-GitSoft @(
        'notes', '--ref=test-combined', 'add', '-f', '-m', $Stamp, $Commit
    )
    if ($ExitCode -ne 0) {
        Write-Warning "Could not write test-combined stamp note on $Commit"
    }
}

function ConvertTo-NormalizedStamp {
    param([string] $Stamp)
    if (-not $Stamp) { return $null }
    # Accept legacy multiline notes (joined with `n) and new single-line (`|`) form.
    $Normalized = $Stamp.Trim() -replace "`r`n", "`n" -replace "`r", "`n"
    if ($Normalized.Contains("`n")) {
        $Normalized = (($Normalized -split "`n") | ForEach-Object { $_.Trim() } | Where-Object { $_ }) -join '|'
    }
    return $Normalized
}

function Test-MergeInProgress {
    $MergeHead = & git.exe rev-parse -q --verify MERGE_HEAD 2>$null
    return [bool] $MergeHead
}

function Get-UnmergedPaths {
    $Output = & git.exe diff --name-only --diff-filter=U 2>$null
    if ($LASTEXITCODE -ne 0) {
        return @()
    }
    return @($Output | Where-Object { $_ })
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

    $MergeMessage = "test: merge $ExtraBranch for local testing"
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

function Get-CurrentBranch {
    return ([string] (Invoke-Git @('branch', '--show-current') | Select-Object -First 1)).Trim()
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
        $Json = & git show $Spec 2>$null
        if ($LASTEXITCODE -eq 0 -and $Json) {
            return @{ Source = $Spec; Json = [string] $Json }
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
            $Json = & git show $ExplicitPath 2>$null
            if ($LASTEXITCODE -eq 0 -and $Json) {
                return @{ Source = $ExplicitPath; Json = [string] $Json }
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

if (-not (Test-Path "FYPA.py")) {
    throw "FYPA.py not found in $RepoRoot — run this script from the FYPA repo."
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

$PrjPcbPath = $null
if ($PrjPcb) {
    if (-not (Test-Path -LiteralPath $PrjPcb)) {
        throw "PrjPcb not found: $PrjPcb"
    }
    $PrjPcbPath = (Resolve-Path -LiteralPath $PrjPcb).Path
}

if (-not $BaseBranch) { throw "baseBranch is empty." }
if (-not $TestBranch) { throw "testBranch is empty." }

$UseLocalOnly = [bool] $LocalOnly
if ($UseLocalOnly) {
    Write-Host "==> Branch source: local only"
}
else {
    Write-Host "==> Branch source: $Remote (soft-fetch; prefer local when ahead/diverged)"
}

$ReturnBranch = Get-CurrentBranch
if (-not $ReturnBranch) {
    throw "Could not determine the current branch."
}

if (-not $UseLocalOnly) {
    try {
        Sync-RemoteBranches -RemoteName $Remote -Branches (@($BaseBranch) + $ExtraFeatureBranches)
    }
    catch {
        Write-Warning "Fetch from $Remote failed; resolving from existing local/remote-tracking refs."
        Write-Warning "$_"
    }
}

$BaseTarget = Resolve-BranchMergeTarget -Branch $BaseBranch -RemoteName $Remote -UseLocalOnly $UseLocalOnly
if (-not $BaseTarget) {
    $Where = if ($UseLocalOnly) { "locally" } else { "locally or as $Remote/$BaseBranch" }
    throw "Base branch '$BaseBranch' not found $Where."
}

$BaseRef = $BaseTarget.MergeRef
$BaseSha = $BaseTarget.Sha
Write-Host "==> Base: $BaseRef ($($BaseTarget.Source))"

$ExtraStampPairs = [System.Collections.Generic.List[string]]::new()
$ResolvedExtras = [System.Collections.Generic.List[hashtable]]::new()
foreach ($ExtraBranch in $ExtraFeatureBranches) {
    if (-not $ExtraBranch) { continue }
    $ExtraTarget = Resolve-BranchMergeTarget -Branch $ExtraBranch -RemoteName $Remote -UseLocalOnly $UseLocalOnly
    if (-not $ExtraTarget) {
        $Where = if ($UseLocalOnly) { "locally" } else { "locally or on $Remote" }
        Write-Warning "Extra feature branch '$ExtraBranch' not found $Where — continuing without it."
        continue
    }
    Write-Host "==> Extra: $($ExtraTarget.MergeRef) ($($ExtraTarget.Source))"
    $ExtraStampPairs.Add("$ExtraBranch=$($ExtraTarget.Sha)")
    $ResolvedExtras.Add(@{
        Branch   = $ExtraTarget.Branch
        MergeRef = $ExtraTarget.MergeRef
    })
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

$TestBranchExists = Test-GitRef "refs/heads/$TestBranch"
$ExistingTip = if ($TestBranchExists) { Get-RefSha -Ref $TestBranch } else { $null }
$ExistingStampRaw = $null
if ($ExistingTip) {
    $ExistingStampRaw = Get-TestCombinedStamp -Commit $ExistingTip
}
$ExistingStamp = ConvertTo-NormalizedStamp $ExistingStampRaw
$CanReuse = (
    -not $Rebuild -and
    $TestBranchExists -and
    $ExistingStamp -and
    ($ExistingStamp -eq $DesiredStamp)
)

if (-not $CanReuse -and $TestBranchExists -and -not $Rebuild) {
    if (-not $ExistingStamp) {
        Write-Host "==> No reuse stamp on $TestBranch — will rebuild"
    }
    else {
        Write-Host "==> Stamp mismatch on $TestBranch — will rebuild"
    }
}

$IgnoredPaths = @('.gitignore', 'FYPA.code-workspace')
$Status = @(Invoke-Git @('status', '--porcelain'))
$BlockingStatus = @($Status | Where-Object {
    $path = $_.Substring(3).Trim()
    if ($path -match ' -> ') { $path = ($path -split ' -> ', 2)[-1].Trim() }
    elseif ($path -match "`t") { $path = ($path -split "`t", 2)[-1].Trim() }
    $path -notin $IgnoredPaths
})
if ($BlockingStatus.Count -gt 0) {
    throw @"
Uncommitted changes detected on '$ReturnBranch'.
Commit or stash them before running the test script.
"@
}

$Returned = $false
$FypaExit = 0
try {
    if ($CanReuse) {
        Write-Host "==> Reuse $TestBranch (inputs unchanged)"
        if ((Get-CurrentBranch) -ne $TestBranch) {
            Invoke-Git @('checkout', $TestBranch)
        }
    }
    else {
        if ($Rebuild) {
            Write-Host "==> Rebuild requested — recreating $TestBranch"
        }
        elseif (-not $TestBranchExists) {
            Write-Host "==> $TestBranch missing — creating from $BaseRef"
        }
        else {
            Write-Host "==> Inputs changed — recreating $TestBranch from $BaseRef"
        }

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

    if ($SkipTests) {
        Write-Host "==> Skip pytest (-SkipTests)"
    }
    else {
        Write-Host "==> pytest topology tests"
        & uv run python -m pytest `
            tests/test_topology_invariants.py `
            tests/test_topology_regressions.py `
            tests/test_topology_layout.py `
            tests/test_topology_geometry.py `
            tests/test_topology_labels.py `
            tests/test_pdn_topology.py -q
        if ($LASTEXITCODE -ne 0) {
            throw "pytest failed (exit $LASTEXITCODE)"
        }
    }

    Write-Host "==> uv run FYPA.py"
    if ($PrjPcbPath) {
        Write-Host "    Project: $PrjPcbPath"
        & uv run --extra spacemouse FYPA.py gui $PrjPcbPath
    }
    else {
        & uv run --extra spacemouse FYPA.py
    }
    $FypaExit = $LASTEXITCODE
}
catch {
    if (Get-CurrentBranch -ne $ReturnBranch) {
        & git merge --abort 2>$null | Out-Null
        & git rebase --abort 2>$null | Out-Null
    }
    throw
}
finally {
    $Current = Get-CurrentBranch
    if ($Current -ne $ReturnBranch) {
        Write-Host "==> Return to $ReturnBranch"
        Restore-DevBranch -Branch $ReturnBranch
        $Returned = $true
    }
}

if (-not $Returned) {
    Write-Host "==> Return to $ReturnBranch"
    Restore-DevBranch -Branch $ReturnBranch
}

if ($FypaExit -and $FypaExit -ne 0) {
    exit $FypaExit
}
