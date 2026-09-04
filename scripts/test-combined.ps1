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
    [string] $PrjPcb
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if ($Rebuild) {
    Write-Error @"
-Rebuild is no longer supported by test-combined.ps1.
Rebuild and publish the shared branch with:

  pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push

Then re-run this script to check out $Remote/$TestBranch.
"@
    exit 1
}

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

function Get-CurrentBranch {
    return ([string] (Invoke-Git @('branch', '--show-current') | Select-Object -First 1)).Trim()
}

function Test-GitRef {
    param([string] $Ref)
    & git show-ref --verify --quiet $Ref
    return $LASTEXITCODE -eq 0
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
    throw "Could not determine the current branch."
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

$Returned = $false
$FypaExit = 0
try {
    Write-Host "==> Checkout $TestBranch from $RemoteRef"
    Invoke-Git @('checkout', '-B', $TestBranch, $RemoteRef)
    Invoke-Git @('reset', '--hard', $RemoteRef)

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
    throw
}
finally {
    $Current = Get-CurrentBranch
    if ($Current -ne $ReturnBranch) {
        Write-Host "==> Return to $ReturnBranch"
        Invoke-Git @('checkout', $ReturnBranch)
        $Returned = $true
    }
}

if (-not $Returned) {
    Write-Host "==> Return to $ReturnBranch"
    Invoke-Git @('checkout', $ReturnBranch)
}

if ($FypaExit -and $FypaExit -ne 0) {
    exit $FypaExit
}
