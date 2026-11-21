---
name: quick
description: Trunk-based lane for changes below the issue threshold — one capped, verified, conventional commit pushed directly to the default branch, no issue and no MR. Explicit invocation is the authorization; hard scope caps with auto-escalation to the issue workflow. E.g. "/quick fix the button color", "/quick typo in the README", "commit this quickly".
---

# quick (lite)

**Goal:** ship a genuinely trivial change — a one-line fix, a color tweak, a
typo — as **one verified conventional commit straight onto the default
branch**, skipping the issue + MR ceremony that buys nothing at this size
(nothing to plan, no second reviewer, one commit is already a clean revert
unit). Invoking `/quick` is the explicit authorization — the sanctioned
exception to "never commit directly to the default branch".

The failure mode of quick-lanes is scope creep, so the **escalation rule is
the most important rule here**.

## Arguments

- `/quick <description>` → make the change and ship it.
- Bare `/quick` with a tiny change already in the working tree → ratify and
  ship it. Tree dirty beyond the intended change → stop and say so.

## Eligibility — check BEFORE touching anything

All must hold, else escalate to `/issue-add` / `/issue-yolo`, saying
"this outgrew `/quick`":

- One logical change, ≤2 files, ~≤20 changed lines, **no new files**.
- No API/schema/config-shape/dependency changes; zero design decisions.

## Steps

1. **Pre-flight:** on the default branch, tree otherwise clean,
   `git pull --ff-only` (not fast-forwardable → stop).
2. **Ephemeral branch:** `git checkout -b quick/<slug>` — edits never happen
   on the default branch, so a red gate or an escalation leaves it untouched.
3. **Make exactly the described change.** No drive-by cleanups.
4. **Verify — proportionate, never skipped:** run the project's declared
   gate (README/AGENTS.md); no gate → language-level minimum on the touched
   files, and say the project has no gate. Then run the `/e2e` skill's
   evaluation — a cosmetic diff routes `static`/`skip` in seconds; a `full`
   routing on a "trivial" change is itself an escalation signal. Red
   anything → stop; nothing lands.
5. **Cap re-check on the real diff:** `git diff <default-branch> --stat` —
   over the caps or any new file → escalate, keep the work on the branch.
6. **Land:** commit (`type: subject` + one *why* line), then
   `git checkout <default-branch> && git merge --ff-only quick/<slug> &&
   git push && git branch -d quick/<slug>` — a plain commit, no merge
   commit, no MR. Push rejected (protected branch, non-ff) → never force;
   fall back to a branch + normal MR, saying so.
7. **Report:** `sha · <type>: <subject> · gate: <result> · e2e: <tier>`.

## Hard rules

- Caps are not negotiable; escalation beats momentum — when in doubt, it's
  an issue.
- Verification always runs; the ceremony was removed, not the safety.
- Never force-push; never split a medium change into two `/quick`s.
- Conventional message with the *why*; no AI attribution trailers.
