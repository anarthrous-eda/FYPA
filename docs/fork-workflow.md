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

Feature branches listed in `team/test-combined.json` on `team/local` are merged **once** into the shared `test/combined` branch and pushed to GitHub. Machines only fetch and check out that tip — they do not merge at Altium/FYPA start.

Config on `team/local`:

```json
{
  "baseBranch": "main",
  "testBranch": "test/combined",
  "deleteTestBranchFirst": true,
  "extraFeatureBranches": ["feature/example-a", "fix/example-b"]
}
```

### Maintain (rebuild + publish)

Uses **origin tips only** (unpushed local commits are ignored). Missing remotes abort.

```powershell
pwsh scripts/maintain-test-combined.ps1 -Rebuild -Push
```

Omit `-Push` to rebuild locally while resolving merge conflicts, then push when clean.

On conflict, only `.gitignore` and `FYPA.code-workspace` auto-resolve with `--ours`; other conflicts stop the script.

Config resolution for maintain: `scripts/test-combined.json` (gitignored override) → `team/test-combined.json` → `team/local:team/test-combined.json` → example file.

### GitHub Action (fork only)

`.github/workflows/maintain-test-combined.yml` lives **only on `team/local`** — never commit it to `main` or to branches destined for an upstream PR.

Triggers: `workflow_dispatch` (use `--ref team/local`) and pushes to `team/local` that touch `team/test-combined.json`. The job runs maintain with `-Rebuild -Push`.

### Launch (no merge)

```powershell
pwsh scripts/test-combined.ps1
pwsh scripts/test-combined.ps1 -SkipTests
pwsh scripts/test-combined.ps1 -SkipTests -PrjPcb path\to\Board.PrjPcb
```

Checks out `origin/test/combined`, optionally runs topology pytest, then FYPA. `-Rebuild` is not supported here — use maintain.

Altium bootstrap (`Run_FYPA.ps1`) calls `scripts/launch-combined-gui.ps1` after clone/`uv sync`: fetch + hard-reset to `origin/test/combined`, then `Launch_GUI.py`.

### Typical flow

1. Push the feature branch to `origin`.
2. Add it to `team/test-combined.json` on `team/local` and push `team/local`.
3. Run maintain (or let the Action rebuild) so `origin/test/combined` updates.
4. On any machine: Altium → Run FYPA → fetch + checkout — done.

Prefer clean feature branches in the JSON (not pre-merged `*-combined` stacks).

## Upstream pull requests

Create the PR branch from upstream, not from `team/local`:

```powershell
git fetch upstream
git checkout -b feature/my-fix upstream/main
git cherry-pick <commit>   # feature commits only
```
