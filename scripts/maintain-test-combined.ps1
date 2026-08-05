<#
.SYNOPSIS
    Rebuild origin/test/combined from team/local config and optionally push.

.DESCRIPTION
    Reads team/test-combined.json (team/local by default), fetches base + extras
    from origin, recreates the disposable test branch using remote tips only,
    and optionally force-pushes with lease.

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
    Force recreate even when the stamp on the existing local test branch matches.

.PARAMETER Push
    After a successful rebuild (or reuse), push --force-with-lease to Remote.

.PARAMETER BaseBranch / TestBranch / ExtraFeatureBranches / DeleteTestBranchFirst
    Override individual config fields.

.EXAMPLE
    pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push

.EXAMPLE
    pwsh scripts/maintain-test-combined.ps1
    Build locally without pushing (e.g. to resolve merge conflicts).
#>

[CmdletBinding()]
param(
    [string] $ConfigPath,
    [string] $TeamConfigRef = "team/local",
    [string] $Remote = "origin",
    [switch] $Rebuild,
    [switch] $Push,
    [string] $BaseBranch,
    [string] $TestBranch,
    [string[]] $ExtraFeatureBranches,
    [bool] $DeleteTestBranchFirst
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

function Get-RefSha {
    param([string] $Ref)
    $Sha = ([string] (& git.exe rev-parse --verify "$Ref^{commit}" 2>$null)).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $Sha) {
        return $null
    }
    return $Sha
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
    $Lines = @(& git.exe notes --ref=test-combined show $Commit 2>$null)
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
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

$ReturnBranch = Get-CurrentBranch
if (-not $ReturnBranch) {
    throw "Could not determine the current branch (detached HEAD?). Check out a branch first."
}

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

$TestBranchExists = Test-GitRef "refs/heads/$TestBranch"
$ExistingTip = if ($TestBranchExists) { Get-RefSha -Ref $TestBranch } else { $null }
$ExistingStamp = ConvertTo-NormalizedStamp (Get-TestCombinedStamp -Commit $ExistingTip)
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
Commit or stash them before running maintain-test-combined.
"@
}

$Returned = $false
$LeaveOnConflict = $false
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

    $Tip = Get-RefSha -Ref 'HEAD'
    Write-Host "==> $TestBranch tip: $Tip"

    if ($Push) {
        Write-Host "==> Push --force-with-lease $Remote $TestBranch"
        Invoke-Git @('push', '--force-with-lease', $Remote, "HEAD:refs/heads/$TestBranch")
        Write-Host "==> Pushed $Remote/$TestBranch"
    }
    else {
        Write-Host "==> Local only (pass -Push to update $Remote/$TestBranch)"
    }
}
catch {
    $msg = "$_"
    if ($msg -match 'Merge conflict' -and (Test-MergeInProgress)) {
        $LeaveOnConflict = $true
        Write-Host @"

==> Merge conflict — staying on $TestBranch with the conflict in place.
Resolve, commit, then:

  git notes --ref=test-combined add -f -m '<stamp>' HEAD   # optional
  git push --force-with-lease $Remote HEAD:refs/heads/$TestBranch

Or re-run: pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push
"@
    }
    elseif ((Get-CurrentBranch) -ne $ReturnBranch) {
        & git merge --abort 2>$null | Out-Null
        & git rebase --abort 2>$null | Out-Null
    }
    throw
}
finally {
    if (-not $LeaveOnConflict) {
        $Current = Get-CurrentBranch
        if ($Current -ne $ReturnBranch) {
            Write-Host "==> Return to $ReturnBranch"
            Restore-DevBranch -Branch $ReturnBranch
            $Returned = $true
        }
    }
}

if (-not $LeaveOnConflict) {
    if (-not $Returned) {
        Write-Host "==> Return to $ReturnBranch"
        Restore-DevBranch -Branch $ReturnBranch
    }
}
