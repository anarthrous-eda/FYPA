---
description: Run the same checks as GitHub CI (ruff, ty, pytest) locally and fix what fails
---

Run the checks `.github/workflows/ci.yml` runs, in the same order, from the
repo root, then report and fix. CI uses a clean checkout of what is
committed, so also check nothing it needs is left untracked.

1. `uv run --no-sync ruff check .`
2. `uv run --no-sync ty check`
3. `uv run --no-sync pytest -q -p no:cacheprovider` (about 100 s; run it in
   the background and carry on with the fixes from steps 1-2 meanwhile)
4. `git status --short`: list any untracked `fypa/`, `tests/` or `scripts/`
   file that tracked code imports. CI won't have it. (The local
   `altium_monkey/` folder is expected to be untracked - CI installs the
   package.)

Run every step even if an earlier one fails, so one pass shows everything.

Then:

- Give a short table: step, pass / fail, number of problems.
- Fix each failure in place. Keep fixes behaviour-neutral (typing, None
  checks, lint rewrites); if a fix would change behaviour, or a test failure
  looks like a real bug rather than a stale test, describe it and ask before
  changing anything.
- Re-run only the steps that failed until they pass.
- Do not commit or push - the user does that. End with a one-line commit
  message for the fixes, if there were any.

$ARGUMENTS
