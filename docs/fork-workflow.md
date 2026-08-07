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

Uses **origin tips only**. Prefer **incremental** updates (default): check out `origin/test/combined` and merge only extras not already in that tip. That keeps prior conflict resolutions.

```powershell
pwsh scripts/maintain-test-combined.ps1 -Push          # incremental
pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push # clean recreate from main
pwsh scripts/maintain-test-combined.ps1 -Abort         # escape stuck merge
```

Omit `-Push` while resolving conflicts locally, then `-Push` when clean.

**Conflicts:** only `.gitignore` / `FYPA.code-workspace` auto-resolve. On a real conflict the script stays on `test/combined` — do not `git switch` away. Either finish (`git add` + `git commit`, then `-Push`) or run `-Abort` to hard-reset to `origin/test/combined` and return to your previous branch.

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

Checks out `origin/test/combined` only. `-Rebuild` is not supported — use maintain.

Altium (`Run_FYPA.ps1`) calls `scripts/launch-combined-gui.ps1`.

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
