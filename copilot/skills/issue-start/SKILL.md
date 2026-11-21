---
name: issue-start
description: Start work on a GitLab issue — pick it, sync the default branch, cut a feature branch, load context, then plan or build. Use when beginning work, e.g. "/issue-start 35", "/issue-start" (pick the best next issue), or "/issue-start 35 now" to skip the plan gate.
---

# issue-start (lite, GitLab)

**Goal:** Go from "issue exists" to "correct branch, context loaded, building"
with zero manual git bookkeeping.

## Arguments

- A number → that issue (`/issue-start 35`).
- Bare → list open issues (`glab issue list --assignee @me`), pick the most
  actionable small one, confirm the pick in one line, proceed.
- `now` anywhere → skip the plan gate even for enhancements.
- `plan` anywhere → force the plan gate.

## Steps

### 0. Claim the repo (first action)

Before reading the issue or any code, from the repo root:

```
python <base-dir>/../_lib/worktree_claim.py acquire . --issue <N>
```

Append `--force-worktree` when `APP_LAUNCHER_SESSION_ID` is set (a launcher
or chief lane never works in the primary checkout, which may be serving a live
app). `MODE=primary`: work in place, as below. `MODE=worktree`: another session
owns the primary; skip step 2's checkout (just `git fetch origin`) and in step 3
create an isolated sibling worktree instead of a branch:
`python <base-dir>/../_lib/worktree_claim.py setup-worktree <repo> <N> <branch>`, then `cd`
into the printed `WORKTREE=` path and do all remaining work there. Never
hand-roll `git worktree add`. Abort after a primary claim → run
`python <base-dir>/../_lib/worktree_claim.py release <repo>` before stopping.

Once the branch exists, publish it for the Board:
`python <base-dir>/../_lib/active_issue.py add <checkout> <N> <branch>` (the primary path, or the
`WORKTREE=` path).

### 1. Load the issue

`glab issue view <N>` — read title, body, labels. If it doesn't exist, stop.

Then the owner gate: `python <base-dir>/../_lib/owner_check/cli.py gate <repo>#<N>`.
`OWNER_GATE=clear` → proceed. `blocked` → stop and print its `reason=` and
`TRY=` lines (the issue waits on a step of the owner's). `unknown` → stop and
say the gate could not be confirmed. Release a primary claim before stopping.

### 2. Sync the default branch

Resolve the default branch rather than guessing it — `git symbolic-ref --short
refs/remotes/origin/HEAD` prints `origin/<branch>`; check out the part after
`origin/`:

```
git symbolic-ref --short refs/remotes/origin/HEAD
git checkout <default-branch>
git pull --ff-only
```

If `origin/HEAD` isn't set locally, `git remote set-head origin --auto` fixes
it; the portable fallback is `git checkout main || git checkout master`.
**Never write `2>nul`** — it's cmd.exe-only syntax, and a POSIX shell (Git
Bash, the shell an agent often gets on Windows) treats `nul` as a filename and
creates an untracked `nul` in the repo root, which makes the *next* run's
dirty-tree check refuse to start.

A dirty tree stops here: report the dirty files and let the user decide.

### 3. Cut the branch

`<type>/<N>-<short-slug>` where `<type>` mirrors the issue's type label
(`bug` → `fix`, `enhancement` → `feat`, `documentation` → `docs`, else the
label itself), e.g. `fix/28-terminal-reconnect`:

```
git checkout -b <type>/<N>-<slug>
```

### 4. Mode

- `bug` / `chore` / `documentation` label (or `now`): build straight away.
- `enhancement` (or `plan`): present a short plan (files to touch, approach,
  risks) and wait for a go-ahead before editing.

### 5. Build

Work the issue to a verified state: run the project's own checks (tests,
lint, byte-compile — whatever the README declares). Report failures honestly.
Don't run e2e per change — `/e2e` is available mid-session when a change
plausibly touches the browser surface, and `/issue-finish` always runs its
evaluation before the MR. When done, suggest `/issue-finish`.
