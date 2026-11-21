---
name: issue-yolo
description: One-shot the GitLab issue workflow end-to-end — load (or file) the issue, cut the branch, build, validate hard, then ship (MR, merge, cleanup). Pass a number ("/issue-yolo 34") to work an existing issue; pass text to file one first. YOLO means "no plan gate", not "no safety" — the verification gate is non-negotiable.
---

# issue-yolo (lite, GitLab)

**Goal:** `/issue-start <N> now` + build + `/issue-finish`, as one
uninterrupted run.

## Arguments

- A number → work that existing issue.
- Text → run the `/issue-add` drafting steps first (research, draft, label,
  create), then continue with the new number.
- Nothing → stop and ask for an issue number or an idea.

## Steps

1. **Start:** follow `/issue-start <N> now` — load issue, sync default
   branch (stop on a dirty tree), cut `<type>/<N>-<slug>`.
2. **Build:** implement the issue completely. No plan gate — but surface any
   discovery that contradicts the issue's premise instead of building on it.
3. **Validate hard:** run the project's full verification gate through
   `python <base-dir>/../_lib/gate_evidence.py run --label gate --repo . -- <gate command>`
   (binds the result to the tree; marks the gate in flight). Red → fix and
   rerun; only a green gate proceeds. Never skip, never ship on red. Then run
   the `/e2e` skill — proportionate e2e for the diff (`skip`/`static`/`full`,
   fail-safe `full`); it carries the gate's result if the slice already ran.
   A red slice blocks shipping; the tier + reason go in the report.
4. **Ship:** follow `/issue-finish` steps 1-5 — acceptance walk, docs, push,
   MR with `Closes #<N>`, merge, land back on the updated default branch.
5. **Report:** MR URL, what shipped, what was verified, anything left open.
