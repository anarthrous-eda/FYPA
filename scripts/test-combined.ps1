<#
.SYNOPSIS
    Check out origin/test/combined, optionally run tests/FYPA, then switch back.

.DESCRIPTION
    The combined branch is maintained centrally (see scripts/maintain-test-combined.ps1).
    This script only fetches and checks out the remote tip — it does not merge
    feature branches.

    To rebuild and push test/combined:
      pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push

.PARAMETER Remote
    Remote name. Default: origin

.PARAMETER TestBranch
    Combined branch name. Default: test/combined

.PARAMETER Rebuild
    Not supported here — prints how to run maintain-test-combined.ps1 and exits 1.

.PARAMETER SkipTests
    Skip the pytest topology suite; still runs FYPA.py unless the script exits earlier.

.PARAMETER SkipSync
    Skip `uv sync` after checkout. Only safe when the combined branch carries no
    dependency change relative to the branch you started on.

.PARAMETER PrjPcb
    Path to a .PrjPcb passed through to FYPA.py.

.EXAMPLE
    pwsh scripts/test-combined.ps1

.EXAMPLE
    pwsh scripts/test-combined.ps1 -SkipTests -PrjPcb path\to\Board.PrjPcb
#>

[CmdletBinding()]
param(
    [string] $Remote = "origin",
    [string] $TestBranch = "test/combined",
    [switch] $Rebuild,
    [switch] $SkipTests,
    [switch] $SkipSync,
    [string] $PrjPcb
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

. (Join-Path $PSScriptRoot '_git-helpers.ps1')

if ($Rebuild) {
    Write-Error @"
-Rebuild is no longer supported by test-combined.ps1.
Rebuild and publish the shared branch with:

  pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push

Then re-run this script to check out $Remote/$TestBranch.
"@
    exit 1
}

if (-not (Test-Path "FYPA.py")) {
    throw "FYPA.py not found in $RepoRoot — run this script from the FYPA repo."
}

$PrjPcbPath = $null
if ($PrjPcb) {
    if (-not (Test-Path -LiteralPath $PrjPcb)) {
        throw "PrjPcb not found: $PrjPcb"
    }
    $PrjPcbPath = (Resolve-Path -LiteralPath $PrjPcb).Path
}

$ReturnBranch = Get-CurrentBranch
if (-not $ReturnBranch) {
    throw "Could not determine the current branch (detached HEAD?). Check out a branch first."
}

# The gate waves through local .gitignore / FYPA.code-workspace edits, so the
# reset below would silently destroy them. Snapshot and restore instead.
$DirtyIgnored = Assert-CleanWorktree -Context "the test script"
$IgnoredBackup = Backup-WorktreePath -Paths $DirtyIgnored

$RemoteRef = "$Remote/$TestBranch"
Write-Host "==> Fetch $Remote $TestBranch"
$FetchResult = Invoke-GitCore -Quiet @('fetch', $Remote, $TestBranch)
if ($FetchResult.ExitCode -ne 0) {
    Write-Warning "Fetch failed; using existing $RemoteRef if present."
}

if (-not (Test-GitRef "refs/remotes/$Remote/$TestBranch")) {
    throw @"
$RemoteRef not found.
Publish the shared branch first:
  pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push
"@
}

$FypaExit = 0
try {
    Reset-ToRemoteTip -Branch $TestBranch -RemoteRef $RemoteRef

    # The combined branch may carry a feature branch's dependency change; the
    # GUI launcher syncs after the same checkout, so do it here too.
    if ($SkipSync) {
        Write-Host "==> Skip uv sync (-SkipSync)"
    }
    else {
        Sync-UvEnvironment -RepoRoot $RepoRoot
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
finally {
    if ((Get-CurrentBranch) -ne $ReturnBranch) {
        Write-Host "==> Return to $ReturnBranch"
        Invoke-Git @('checkout', $ReturnBranch)
    }
    Restore-WorktreePath -Backup $IgnoredBackup
}

if ($FypaExit -and $FypaExit -ne 0) {
    exit $FypaExit
}
