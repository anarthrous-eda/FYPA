# Fork workflow (team/local)

This fork keeps upstream-ready work separate from local team tooling.

## Branches

| Branch | Purpose |
|--------|---------|
| `main` | Tracks upstream; use as the base for upstream pull requests |
| `team/local` | Shared fork config (combined-test branch list, GH Action for maintain) |
| `test/combined` | Published integration tip built from the JSON list (force-pushed) |

Daily development can use `team/local` or feature branches. **Do not merge `team/local` into branches you open upstream.**

## Combined test (`test/combined`)

Feature branches listed in `team/test-combined.json` on `team/local` are merged into the shared `test/combined` branch and pushed to GitHub. Machines only fetch and check out that tip — they do not merge at Altium/FYPA start.

Config on `team/local`:

```json
{
  "baseBranch": "main",
  "testBranch": "test/combined",
  "deleteTestBranchFirst": true,
  "extraFeatureBranches": ["feature/example-a", "fix/example-b"]
}
```

### Maintain (update + publish)

Uses **origin tips only**. Prefer **incremental** updates (default): check out `origin/test/combined`, then merge `origin/main` plus any extras not already in that tip. That keeps prior conflict resolutions while still picking up everything the base branch gained.

```powershell
pwsh scripts/maintain-test-combined.ps1 -Push          # incremental
pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push # clean recreate from main
pwsh scripts/maintain-test-combined.ps1 -Abort         # escape stuck merge
```

Omit `-Push` while resolving conflicts locally, then `-Push` when clean.

**Conflicts:** only `.gitignore` / `FYPA.code-workspace` auto-resolve. On a real conflict the script stays on `test/combined` — do not `git switch` away. Either finish (`git add` + `git commit`, then `-Push`) or run `-Abort` to hard-reset to `origin/test/combined` and return to the branch that run started from (recorded in `.git/fypa-test-combined-return`). `-Abort` also works from a detached HEAD, which is where an interrupted rebase leaves you.

**Local edits to `.gitignore` / `FYPA.code-workspace`** do not block a run, and are copied aside before the hard reset and put back when you land on your own branch again. Everything else must be committed or stashed first.

**Stamps:** each published tip is annotated in `refs/notes/test-combined` with the config and the input SHAs it was built from. The notes ref is fetched and pushed alongside the branch, so a machine that already has a matching tip reuses it instead of rebuilding.

Use `-Rebuild` only when `main` moved a lot, extras were removed/reordered, or the tip is broken. Expect to resolve the same conflicts again.

Config resolution: `scripts/test-combined.json` (gitignored) → `team/test-combined.json` → `team/local:team/test-combined.json` → example file.

### GitHub Action (fork only)

`.github/workflows/maintain-test-combined.yml` lives **only on `team/local`** — never commit it to `main` or upstream PR branches.

Triggers: `workflow_dispatch` (`--ref team/local`) and pushes to `team/local` that touch `team/test-combined.json`.

### Launch (no merge)

```powershell
pwsh scripts/test-combined.ps1
pwsh scripts/test-combined.ps1 -SkipTests -PrjPcb path\to\Board.PrjPcb
```

Checks out `origin/test/combined`, runs `uv sync` (the combined branch can carry a feature branch's dependency change — pass `-SkipSync` to skip), then the topology tests and FYPA. `-Rebuild` is not supported — use maintain.

Altium (`Run_FYPA.ps1`) calls `scripts/launch-combined-gui.ps1`.

All three scripts share `scripts/_git-helpers.ps1` (dot-sourced) for the git wrapper, the dirty-worktree gate and the checkout/reset step. They run under both `pwsh` (PowerShell 7) and Windows PowerShell 5.1 — the wrapper normalises the two hosts' argument binding and keeps git's stderr from aborting a successful run.

### Typical flow

1. Push the feature branch to `origin`.
2. Add it to `team/test-combined.json` on `team/local` and push `team/local`.
3. `pwsh scripts/maintain-test-combined.ps1 -Push` (incremental).
4. On any machine: Altium / `test-combined.ps1` → `origin/test/combined`.

Prefer clean feature branches in the JSON (not pre-merged `*-combined` stacks).

## Upstream pull requests

```powershell
git fetch upstream
git checkout -b feature/my-fix upstream/main
git cherry-pick <commit>
```
