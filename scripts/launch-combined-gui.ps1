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

. (Join-Path $PSScriptRoot '_git-helpers.ps1')

if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'FYPA.py'))) {
    throw "FYPA.py not found in $RepoRoot"
}

if (-not (Test-Path -LiteralPath $PrjPcb)) {
    throw "PrjPcb not found: $PrjPcb"
}
$PrjPcbPath = (Resolve-Path -LiteralPath $PrjPcb).Path

$RemoteRef = "$Remote/$TestBranch"
Write-Host "==> Fetch $Remote $TestBranch"
# Invoke-GitCore keeps git's stderr out of the success stream: under
# $ErrorActionPreference = 'Stop' a bare `2>&1` here makes fetch's normal
# progress output a terminating NativeCommandError, so the fallback below
# would never run.
$FetchResult = Invoke-GitCore -Quiet @('fetch', $Remote, $TestBranch)
if ($FetchResult.ExitCode -ne 0) {
    Write-Warning "git fetch $Remote $TestBranch failed; trying existing $RemoteRef"
}

$Tip = Get-RefSha -Ref $RemoteRef
if (-not $Tip) {
    throw @"
$RemoteRef not found.
Publish the shared branch first (on a maintainer machine or via the team/local Action):

  pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push
"@
}

Write-Host "==> $TestBranch @ $Tip"
Reset-ToRemoteTip -Branch $TestBranch -RemoteRef $RemoteRef

Sync-UvEnvironment -RepoRoot $RepoRoot

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
