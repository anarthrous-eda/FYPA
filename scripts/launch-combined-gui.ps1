<#
.SYNOPSIS
    Check out origin/test/combined and launch the GUI (Altium bootstrap path).

.DESCRIPTION
    Used by Run_FYPA.ps1 after clone/uv sync. Fetches the shared combined branch,
    hard-resets to the remote tip, and runs Launch_GUI.py (or FYPA.py gui).

    Does not merge feature branches. Maintain the shared branch with:
      pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push

.PARAMETER PrjPcb
    Path to the focused .PrjPcb.

.PARAMETER LaunchGui
    Absolute path to Launch_GUI.py (outside the disposable clone). Optional.

.PARAMETER Remote
    Remote name. Default: origin

.PARAMETER TestBranch
    Combined branch name. Default: test/combined

.PARAMETER RepoRoot
    FYPA repo root. Default: parent of scripts/.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $PrjPcb,

    [string] $LaunchGui,

    [string] $Remote = "origin",

    [string] $TestBranch = "test/combined",

    [string] $RepoRoot
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
else {
    $RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
}
Set-Location $RepoRoot

if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'FYPA.py'))) {
    throw "FYPA.py not found in $RepoRoot"
}

if (-not (Test-Path -LiteralPath $PrjPcb)) {
    throw "PrjPcb not found: $PrjPcb"
}
$PrjPcbPath = (Resolve-Path -LiteralPath $PrjPcb).Path

function Invoke-GitLogged {
    param(
        [Parameter(Mandatory, ValueFromRemainingArguments)]
        [string[]] $GitArgs
    )
    Write-Host (">> git {0}" -f ($GitArgs -join ' ')) -ForegroundColor DarkGray
    & git.exe @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE)"
    }
}

$RemoteRef = "$Remote/$TestBranch"
Write-Host "==> Fetch $Remote $TestBranch"
& git.exe fetch $Remote $TestBranch 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "git fetch $Remote $TestBranch failed; trying existing $RemoteRef"
}

& git.exe rev-parse --verify "$RemoteRef^{commit}" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw @"
$RemoteRef not found.
Publish the shared branch first (on a maintainer machine or via the team/local Action):

  pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push
"@
}

$Tip = ([string](& git.exe rev-parse --verify "$RemoteRef^{commit}")).Trim()
Write-Host "==> Checkout $TestBranch @ $Tip"
Invoke-GitLogged @('checkout', '-B', $TestBranch, $RemoteRef)
Invoke-GitLogged @('reset', '--hard', $RemoteRef)

Write-Host "==> uv sync (on $TestBranch)"
& uv sync
if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    $venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) {
        Write-Warning "uv sync failed; reusing existing .venv"
    }
    else {
        throw "uv sync failed (exit $LASTEXITCODE)"
    }
}

Write-Host "==> Launch GUI"
$env:PYTHONUNBUFFERED = '1'
if ($LaunchGui -and (Test-Path -LiteralPath $LaunchGui)) {
    Write-Host "    Using Launch_GUI.py (File > Import style, GUI first)"
    & uv run --extra spacemouse python $LaunchGui $PrjPcbPath
}
else {
    if ($LaunchGui) {
        Write-Warning "Launch_GUI.py missing at $LaunchGui — falling back to FYPA.py gui"
    }
    & uv run --extra spacemouse FYPA.py gui $PrjPcbPath
}

if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
