# Fork workflow (team/local)

This fork keeps upstream-ready work separate from local team tooling.

## Branches

| Branch | Purpose |
|--------|---------|
| `main` | Tracks upstream; use as the base for upstream pull requests |
| `team/local` | Shared fork config (combined-test branch list, etc.) |

Daily development can use `team/local` or feature branches. **Do not merge `team/local` into branches you open upstream.**

## Combined local test

`scripts/test-combined.ps1` builds a throwaway branch, merges configured feature branches, runs topology tests and FYPA, then returns to your previous branch. Run it from **any** branch — config is read from `team/local` via `git show` when the file is not in your working tree.

Config lives in `team/test-combined.json` on the `team/local` branch:

```json
{
  "baseBranch": "main",
  "testBranch": "test/combined",
  "deleteTestBranchFirst": true,
  "extraFeatureBranches": ["feature/example-a", "fix/example-b"]
}
```

- `deleteTestBranchFirst`: when `true`, delete `testBranch` before recreating it (clean slate).
- By default, `baseBranch` and `extraFeatureBranches` are **fetched from `origin`** and merged via `origin/<branch>`. Use `--local-only` to use local branches only.
- Override any field on the command line, e.g. `-DeleteTestBranchFirst:$false`.

```powershell
pwsh scripts/test-combined.ps1
pwsh scripts/test-combined.ps1 --local-only
```

Resolution order: `scripts/test-combined.json` (local override) → `team/test-combined.json` → `team/local:team/test-combined.json` → example file.

For a one-off local config, copy `scripts/test-combined.example.json` to `scripts/test-combined.json` (gitignored).

## Upstream pull requests

Create the PR branch from upstream, not from `team/local`:

```powershell
git fetch upstream
git checkout -b feature/my-fix upstream/main
git cherry-pick <commit>   # feature commits only
```
