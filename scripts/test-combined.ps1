# Build a local combined test branch from a base branch + feature branches, run tests/FYPA, then switch back.
#
# Config (first match wins):
#   scripts/test-combined.json          local override (gitignored)
#   team/test-combined.json             working tree
#   team/local:team/test-combined.json  from team/local branch via git show (no checkout)
#   scripts/test-combined.example.json  fallback
#
# Usage (from repo root, any branch):
#   pwsh scripts/test-combined.ps1
#   pwsh scripts/test-combined.ps1 --local-only
#   pwsh scripts/test-combined.ps1 -ConfigPath scripts/test-combined.json
#   pwsh scripts/test-combined.ps1 -PrjPcb path\to\YourBoard.PrjPcb
#
# By default baseBranch and extraFeatureBranches are fetched from origin and merged
# via origin/<branch>. Pass --local-only to use local branches only.
#
# Workflow:
#   1. Remember current branch
#   2. Optionally delete the test branch, then recreate it from the base branch
#   3. Merge every extra feature branch (.gitignore conflicts auto-resolved with --ours)
#   4. Run pytest topology suite, then uv run FYPA.py
#   5. Return to the branch you started on (even if a step exits with an error)

[CmdletBinding()]
param(
    [string] $ConfigPath,
    [string] $TeamConfigRef = "team/local",
    [string] $Remote = "origin",
    [switch] $LocalOnly,
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

function Get-BranchMergeRef {
    param(
        [string] $Branch,
        [string] $RemoteName,
        [bool] $UseLocalOnly
    )

    if ($UseLocalOnly) {
        return $Branch
    }
    return "$RemoteName/$Branch"
}

function Test-BranchAvailable {
    param(
        [string] $Branch,
        [string] $RemoteName,
        [bool] $UseLocalOnly
    )

    if ($UseLocalOnly) {
        return Test-GitRef "refs/heads/$Branch"
    }
    return Test-GitRef "refs/remotes/$RemoteName/$Branch"
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
    Invoke-Git @(@('fetch', $RemoteName) + $UniqueBranches)
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
    Write-Host "==> Branch source: $Remote (fetch + merge remote-tracking refs)"
}

$ReturnBranch = Get-CurrentBranch
if (-not $ReturnBranch) {
    throw "Could not determine the current branch."
}

if ($UseLocalOnly) {
    if (-not (Test-BranchAvailable -Branch $BaseBranch -RemoteName $Remote -UseLocalOnly $true)) {
        throw "Base branch '$BaseBranch' not found locally."
    }
}
else {
    Sync-RemoteBranches -RemoteName $Remote -Branches (@($BaseBranch) + $ExtraFeatureBranches)
    if (-not (Test-BranchAvailable -Branch $BaseBranch -RemoteName $Remote -UseLocalOnly $false)) {
        throw "Remote branch '$Remote/$BaseBranch' not found after fetch."
    }
}

$BaseRef = Get-BranchMergeRef -Branch $BaseBranch -RemoteName $Remote -UseLocalOnly $UseLocalOnly

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
    if ($DeleteTestBranchFirst -and (Test-GitRef "refs/heads/$TestBranch")) {
        Write-Host "==> Delete $TestBranch"
        if (Get-CurrentBranch -eq $TestBranch) {
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

    foreach ($ExtraBranch in $ExtraFeatureBranches) {
        if (-not $ExtraBranch) { continue }

        $MergeRef = Get-BranchMergeRef -Branch $ExtraBranch -RemoteName $Remote -UseLocalOnly $UseLocalOnly
        if (Test-BranchAvailable -Branch $ExtraBranch -RemoteName $Remote -UseLocalOnly $UseLocalOnly) {
            Write-Host "==> Merge $MergeRef into $TestBranch"
            Merge-FeatureBranch -MergeRef $MergeRef -ExtraBranch $ExtraBranch -IgnoredPaths $IgnoredPaths
        }
        else {
            $Label = if ($UseLocalOnly) { "local branch" } else { "remote branch" }
            Write-Warning "Extra feature $Label '$ExtraBranch' not found — continuing without it."
        }
    }

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
