---
name: issue-finish
description: Finish the current issue branch — confirm acceptance, update docs, run the verification gate, push, open a merge request that closes the issue, merge when the pipeline passes, and clean up. Use "/issue-finish" on a feature branch; pairs with /issue-start.
---

# issue-finish (lite, GitLab)

**Goal:** Take the current feature branch from "code done" to "merged, branch
deleted, issue closed, back on an updated default branch".

## Preconditions

- On a `<type>/<N>-<slug>` branch (not the default branch) — else stop.
- Parse `<N>` from the branch name; `glab issue view <N>` to reload the
  acceptance checklist.

## Steps

### 1. Acceptance + docs

- Walk the issue's acceptance checkboxes against the actual diff; anything
  unmet → report and stop.
- Update `README.md` if usage, config, or output changed.

### 2. Verification gate

Run the project's declared gate (README/AGENTS.md — e.g. tests + lint +
byte-compile) through the evidence helper:
`python <base-dir>/../_lib/gate_evidence.py run --label gate --repo . -- <gate command>`.
It binds the result to the tree and marks the gate in flight, so a
chief-dispatched lane is never compacted mid-gate. Red gate → stop and
report; never ship on red.

### 2b. E2e leg (delegated to `/e2e`)

Run the `/e2e` skill — the evaluation is mandatory before any MR, the
execution proportionate: it routes the branch diff to a tier
(`skip`/`static`/`full`, fail-safe `full`), runs the routed slice, and keeps
the suite right-sized. If the gate above already ran that slice, `/e2e`
carries the result. A red slice stops the finish like a red gate; the tier +
reason go in the final report even when the answer is `skip` or `n/a`.

### 3. Commit + push

Conventional message (`<type>: <subject>`, ≤72-char first line, body bullets
explaining *why*). Then:

```
git push -u origin HEAD
```

### 4. Merge request

```
python <base-dir>/../_lib/chief_ops.py mr-create <N>
python <base-dir>/../_lib/chief_ops.py merge <repo>#<N> --branch <branch> [--ask]
```

`mr-create` runs `glab mr create --fill --yes` with stdin closed and a kill
timeout, so it cannot wait on a prompt nobody reads. It prints `MR=created
url=...` (the branch must already be pushed, step 3) or stops on `MR=failed`,
`MR=timeout` (glab gave no answer and was killed; check `glab mr view` before
retrying) or `MR=unknown reason=...`. Never run bare `glab mr create` in a
lane. `Closes #<N>` in the MR description auto-closes the issue on merge to the
default branch. The `merge` verb asks GitLab to merge as soon as the pipeline
passes (a project with no pipeline merges at once) and reads the state back:
`MERGE=merged` → land; `MERGE=queued` → wait for the pipeline, then re-run the
verb until it says `merged` or `refused`; `MERGE=refused reason=...` → stop and
report the reason; `MERGE=unknown` → stop, glab could not answer. In a
chief-dispatched session pass `--ask`, so a refusal also becomes a question on
the chief's Board (`MERGE_ASKED=`).

### 5. Land

First ask which checkout this is, from the checkout you are finishing:
`python <base-dir>/../_lib/worktree_claim.py mode <repo>` prints `primary`, `worktree` or
`UNKNOWN reason=...` (stop; never guess).

**Linked worktree** (`<repo>-wt-<N>`): never check out the default branch here.
`cd` to the primary repo path, then remove the worktree (the helper strips the
`.venv` junction before `git worktree remove`) and land the primary:

```
python <base-dir>/../_lib/worktree_claim.py remove-worktree <repo>-wt-<N>
python <base-dir>/../_lib/worktree_claim.py land-primary <repo> <N>
```

Report its one `PRIMARY=` line: `PRIMARY=live` means the merge is live
locally; `PRIMARY=stale reason=...` means it is not, so say so plainly.

**Primary checkout:** guard before switching:
`python <base-dir>/../_lib/worktree_claim.py assert-owner <repo> <N>`. `ASSERT_OWNER=refuse`
→ stop and report. On a pass, land with the steps below, but fast-forward through
`python <base-dir>/../_lib/untrack_guard.py fast-forward .` instead of `git pull --ff-only`,
then `python <base-dir>/../_lib/worktree_claim.py release <repo>` and confirm
`python <base-dir>/../_lib/worktree_claim.py status <repo>` prints `CLAIM=free`.

Either way, clear the Board marker:
`python <base-dir>/../_lib/active_issue.py remove <checkout> <N>`.


Resolve the default branch rather than guessing it — `git symbolic-ref --short
refs/remotes/origin/HEAD` prints `origin/<branch>`; check out the part after
`origin/` (fallback `git checkout main || git checkout master`). **Never write
`2>nul`**: it's cmd.exe-only, and a POSIX shell (Git Bash) treats `nul` as a
filename and leaves an untracked `nul` behind that blocks the next run's
dirty-tree check.

```
git symbolic-ref --short refs/remotes/origin/HEAD
git checkout <default-branch>
git pull --ff-only
git branch -d <branch>
git fetch --prune
```

Confirm the MR merged and the issue closed (`glab issue view <N>` shows
closed). Report the MR URL and the one-line outcome.
