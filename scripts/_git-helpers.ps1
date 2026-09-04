<#
.SYNOPSIS
    Shared git plumbing for the test/combined scripts. Dot-source, don't run.

.DESCRIPTION
    test-combined.ps1, maintain-test-combined.ps1 and launch-combined-gui.ps1
    all fetch, check out and hard-reset the same shared branch. They used to
    carry their own copies of these helpers, which is how their stderr handling
    drifted apart (one of them merged git's stderr into the success stream under
    $ErrorActionPreference = 'Stop', so a normal "From github.com:..." progress
    line aborted the run). Keep the plumbing here so a fix lands once.

    Dot-source from a caller in the same directory:

      . (Join-Path $PSScriptRoot '_git-helpers.ps1')
#>

function Expand-GitArgs {
    <#
        Flatten the argument list of a ValueFromRemainingArguments parameter.

        Windows PowerShell 5.1 and PowerShell 7 disagree here: given
        `Invoke-Git @('branch', '--show-current')`, 7 enumerates the array into
        two arguments while 5.1 keeps it as one nested element, which a
        [string[]] parameter then joins into the single argument
        "branch --show-current" — git rejects it. Declaring the parameter
        [object[]] and flattening by hand makes both hosts behave the same.
    #>
    param([object[]] $GitArgs)

    $Flat = [System.Collections.Generic.List[string]]::new()
    foreach ($Arg in $GitArgs) {
        if ($null -eq $Arg) { continue }
        if ($Arg -is [string]) {
            $Flat.Add($Arg)
        }
        elseif ($Arg -is [System.Collections.IEnumerable]) {
            foreach ($Item in $Arg) {
                if ($null -ne $Item) { $Flat.Add([string] $Item) }
            }
        }
        else {
            $Flat.Add([string] $Arg)
        }
    }
    return $Flat.ToArray()
}

function Get-FirstLine {
    <#
        First line of a captured command's output, or '' when there was none.

        `[string] ($empty | Select-Object -First 1)` does NOT give '': the
        pipeline yields AutomationNull and the cast produces $null, so the
        .Trim() that usually follows throws "You cannot call a method on a
        null-valued expression". Detached HEAD hits this — `git branch
        --show-current` exits 0 and prints nothing.
    #>
    param([object[]] $Lines)

    if ($null -eq $Lines -or $Lines.Count -eq 0) { return '' }
    $First = $Lines[0]
    if ($null -eq $First) { return '' }
    return ([string] $First).Trim()
}

function Invoke-GitCore {
    <#
    .SYNOPSIS
        Run git, returning @{ ExitCode; Output } with stderr split out.

    .DESCRIPTION
        `2>&1` merges git's stderr into the pipeline as ErrorRecord objects,
        and under $ErrorActionPreference = 'Stop' those are terminating — so
        the ordinary "From github.com:..." progress git prints to stderr on a
        SUCCESSFUL fetch would abort the caller. Capturing into a variable is
        not enough (Windows PowerShell still raises NativeCommandError during
        the pipeline), so the preference is lowered for the call itself. The
        assignment is function-scoped: it shadows the caller's value and is
        gone on return. Output holds stdout only; stderr is echoed as warnings
        unless -Quiet. Callers decide what to do about $ExitCode.
    #>
    param(
        [Parameter(Mandatory, ValueFromRemainingArguments)]
        [object[]] $GitArgs,
        [switch] $Quiet
    )
    $GitArgs = Expand-GitArgs $GitArgs
    if ($GitArgs.Count -eq 0) {
        throw "Invoke-GitCore: no arguments"
    }

    $ErrorActionPreference = 'Continue'
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
    <# Run git and throw on a non-zero exit. Returns stdout lines. #>
    param(
        [Parameter(Mandatory, ValueFromRemainingArguments)]
        [object[]] $GitArgs
    )
    $GitArgs = Expand-GitArgs $GitArgs
    $Result = Invoke-GitCore @GitArgs
    if ($Result.ExitCode -ne 0) {
        throw "git $($GitArgs -join ' ') failed (exit $($Result.ExitCode))"
    }
    return $Result.Output
}

function Invoke-GitSoft {
    <# Run git and return its exit code instead of throwing. #>
    param(
        [Parameter(Mandatory, ValueFromRemainingArguments)]
        [object[]] $GitArgs,
        [switch] $Quiet
    )
    $GitArgs = Expand-GitArgs $GitArgs
    return (Invoke-GitCore -Quiet:$Quiet @GitArgs).ExitCode
}

function Get-CurrentBranch {
    <#
        Current branch name, or '' on a detached HEAD. `git branch
        --show-current` exits 0 with empty output when detached, so callers
        that need a branch must test the result themselves.
    #>
    $Result = Invoke-GitCore -Quiet @('branch', '--show-current')
    if ($Result.ExitCode -ne 0) {
        throw "git branch --show-current failed (exit $($Result.ExitCode))"
    }
    return (Get-FirstLine $Result.Output)
}

function Test-GitRef {
    param([string] $Ref)
    return (Invoke-GitSoft -Quiet @('show-ref', '--verify', '--quiet', $Ref)) -eq 0
}

function Get-RefSha {
    param([string] $Ref)
    $Result = Invoke-GitCore -Quiet @('rev-parse', '--verify', "$Ref^{commit}")
    if ($Result.ExitCode -ne 0) {
        return $null
    }
    $Sha = Get-FirstLine $Result.Output
    if (-not $Sha) { return $null }
    return $Sha
}

# Paths whose local edits are per-machine noise on the shared branch: they are
# waved through the dirty-worktree gate instead of blocking a run. They are
# also the paths Get-DirtyIgnoredPath preserves across the hard reset that
# follows — see the warning there.
$script:CombinedIgnoredPaths = @('.gitignore', 'FYPA.code-workspace')

function Get-StatusPath {
    <# Worktree path from one `git status --porcelain` line (rename-aware). #>
    param([string] $Line)
    if ($Line.Length -le 3) { return '' }
    $Path = $Line.Substring(3).Trim()
    if ($Path -match ' -> ') { $Path = ($Path -split ' -> ', 2)[-1].Trim() }
    elseif ($Path -match "`t") { $Path = ($Path -split "`t", 2)[-1].Trim() }
    return $Path.Trim('"')
}

function Assert-CleanWorktree {
    <#
    .SYNOPSIS
        Throw unless the worktree is clean apart from $IgnoredPaths.

    .OUTPUTS
        The ignored paths that ARE dirty, so the caller can preserve them
        across the hard reset it is about to do.
    #>
    param(
        [string[]] $IgnoredPaths = $script:CombinedIgnoredPaths,
        [string] $Context = "the test script"
    )

    $StatusResult = Invoke-GitCore -Quiet @('status', '--porcelain')
    if ($StatusResult.ExitCode -ne 0) {
        throw "git status --porcelain failed (exit $($StatusResult.ExitCode))"
    }
    $Status = @($StatusResult.Output | Where-Object { $_ })
    $Blocking = @($Status | Where-Object { (Get-StatusPath $_) -notin $IgnoredPaths })
    if ($Blocking.Count -gt 0) {
        throw @"
Uncommitted changes detected on '$(Get-CurrentBranch)'.
Commit or stash them before running $Context.
"@
    }
    return @(
        $Status |
            ForEach-Object { Get-StatusPath $_ } |
            Where-Object { $_ -in $IgnoredPaths } |
            Select-Object -Unique
    )
}

function Backup-WorktreePath {
    <#
    .SYNOPSIS
        Copy the working-tree content of $Paths to a temp dir.

    .DESCRIPTION
        The dirty-worktree gate lets local .gitignore / FYPA.code-workspace
        edits through, and the caller then runs `reset --hard`, which would
        destroy them silently. Snapshot them first and hand the result to
        Restore-WorktreePath.

    .OUTPUTS
        @{ Dir; Files = @{ relative path = temp file } }, or $null for nothing
        to back up.
    #>
    param([string[]] $Paths)

    $Present = @($Paths | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
    if ($Present.Count -eq 0) { return $null }

    $Dir = Join-Path ([System.IO.Path]::GetTempPath()) ("fypa-combined-" + [guid]::NewGuid().ToString('n'))
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    $Files = @{}
    foreach ($Path in $Present) {
        # Every character illegal in a file name has to go, not just the
        # separators: a drive-qualified path would otherwise keep its "C:".
        $Dest = Join-Path $Dir ($Path -replace '[\\/:*?"<>|]', '_')
        Copy-Item -LiteralPath $Path -Destination $Dest -Force
        $Files[$Path] = $Dest
    }
    Write-Host "==> Preserving local edits to $($Present -join ', ')"
    return @{ Dir = $Dir; Files = $Files }
}

function Restore-WorktreePath {
    <# Put a Backup-WorktreePath snapshot back and delete the temp copy. #>
    param($Backup)

    if (-not $Backup) { return }
    foreach ($Path in $Backup.Files.Keys) {
        try {
            Copy-Item -LiteralPath $Backup.Files[$Path] -Destination $Path -Force
        }
        catch {
            Write-Warning "Could not restore $Path — a copy is at $($Backup.Files[$Path])"
            return
        }
    }
    Write-Host "==> Restored local edits to $($Backup.Files.Keys -join ', ')"
    Remove-Item -LiteralPath $Backup.Dir -Recurse -Force -ErrorAction SilentlyContinue
}

function Sync-UvEnvironment {
    <#
    .SYNOPSIS
        `uv sync` after a branch switch, falling back to the existing .venv.

    .DESCRIPTION
        The combined branch can carry a feature branch's pyproject/uv.lock
        change, so running pytest or the GUI against the previous branch's
        .venv produces an ImportError that looks like a bug in the merged code.
    #>
    param([string] $RepoRoot)

    Write-Host "==> uv sync"
    & uv sync
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        $VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $VenvPython) {
            Write-Warning "uv sync failed; reusing existing .venv"
        }
        else {
            throw "uv sync failed (exit $LASTEXITCODE)"
        }
    }
}

function Reset-ToRemoteTip {
    <# Point $Branch at $RemoteRef, check it out, and discard local content. #>
    param(
        [string] $Branch,
        [string] $RemoteRef
    )
    Write-Host "==> Checkout $Branch @ $RemoteRef"
    Invoke-Git @('checkout', '-B', $Branch, $RemoteRef) | Out-Null
    Invoke-Git @('reset', '--hard', $RemoteRef) | Out-Null
}
