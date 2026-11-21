---
name: e2e
description: Decide, run, and maintain a repo's end-to-end tests proportionate to the actual diff — deterministic tier routing (skip/static/full, fail-safe full), self-healing adoption from the bundled router, inline test upkeep. Called by /issue-finish and /issue-yolo before shipping; also standalone — e.g. "/e2e", "/e2e plan", "/e2e full", "run the e2e".
---

# e2e (lite)

**Goal:** stop paying for a full browser suite on diffs that never touch the
browser, **without ever under-testing** — uncertainty always escalates to the
full suite, never narrows. Everything this skill needs ships in this folder
(`e2e_route.py` + the bundled `classify_e2e.py` router) plus the sibling
`skills/_lib/` (`runtime_files.py`); it has no dependency on any other repo or
checkout. Copy or junction `skills/_lib/` together with this folder.

All commands below run the scripts from **this skill's base directory**
(shown when the skill loads) with a Python ≥3.11 interpreter.

## Arguments

- Nothing → classify the branch diff, state tier + reason, run the slice.
- `plan` → classify and report only; run nothing.
- `full` → force the full suite (forcing *up* is always allowed; there is no
  way to force *down*).

## Steps

1. **Probe:** `python <base-dir>/e2e_route.py probe <repo-root>` — prints
   `CLASSIFIER` / `E2E_TABLE` / `SUITE` / `WEB_SURFACE` facts. Key every
   branch below on these printed facts, never on eyeballing.
2. **No suite (`SUITE=absent`):** nothing to route. `WEB_SURFACE=no` → report
   `e2e: n/a` and stop — never recommend an e2e suite for a non-web repo.
   `WEB_SURFACE=yes` → evaluate whether a starter suite is worth it (silent
   breakage would hurt + no unit test can catch it + behavior stabilized);
   propose it in the report — build only on the user's OK.
3. **Suite present, `CLASSIFIER=absent`:** self-heal on the current feature
   branch: `python <base-dir>/e2e_route.py bootstrap <repo-root>` copies the
   bundled router in byte-verbatim (`BOOTSTRAP=refused` = the repo has its
   own custom classifier — honor it, don't force). If `E2E_TABLE=absent`,
   author a conservative starter `[e2e]` table in the repo's `.fleet.toml`
   (schema below): explicit `none` rules only for plainly inert paths,
   `static` for asset trees, `full` for the app dir — when in doubt leave a
   path unclassified; unmatched already fails safe to `full`.
4. **Route:** `python <base-dir>/e2e_route.py route <repo-root>` — honor
   `SOURCE=classifier` output **verbatim** (`E2E_TIER`, `E2E_BROWSERS`,
   `E2E_PYTEST_TARGET`, `E2E_REASON`); `classifier-error` already escalated
   to `full`; `judgment` → classify the diff yourself with the same tiers:
   only a diff you can positively argue has no browser impact routes below
   `full`.
5. **Execute synchronously:** `skip` → say so and run nothing browser-shaped;
   `static`/`full` → run the printed pytest target through the repo's own
   interpreter/venv. If the repo's declared verification gate already ran
   this same slice in this session, carry that result — never run it twice.
   Report failures honestly; a red slice blocks shipping like a red gate.
6. **Maintain inline (same branch):** diff removed a feature → delete the
   e2e tests that covered it, same branch; new stable user-visible behavior
   that only e2e can protect → add the regression test (keep the suite
   small — if tempted to grow past ~15 tests, delete or merge first); a
   plainly inert path that keeps routing `full` as unmatched → add its
   narrowing rule to the table in the same change.
7. **Report** (delegating skills echo this into their summary):
   `e2e: <tier> (<reason>) — PASS | FAIL | skip | n/a [maintenance: ...]`

## The `[e2e]` table (schema, embedded)

Lives in the target repo's own `.fleet.toml`. Rules are evaluated top-to-
bottom, **first match wins**; any changed path matching no rule — or a
missing/malformed table, or an empty diff — routes `full`.

```toml
[e2e]
static_pytest_target = "tests/e2e/test_smoke.py"  # what `static` runs
static_browsers       = ["chromium"]
full_pytest_target    = "tests/e2e"               # what `full` runs

[[e2e.rule]]              # inert assets -> smoke slice is enough
tier       = "static"
prefix     = "app/webapp/static/"
extensions = ["svg", "png", "ico", "woff2"]

[[e2e.rule]]              # real browser surface -> full
tier   = "full"
prefix = "app/webapp/"

[[e2e.rule]]              # backend python -> no browser impact
tier       = "none"
prefix     = "src/"
extensions = ["py"]
```

A rule matches on any combination of `prefix` / `path` / `extensions`; a rule
with none of them matches nothing (guarded). Keep CSS/JS on `full` — layout
regressions live exactly there.

## Hard rules

- Never narrow below the classifier; every uncertainty escalates to `full`.
- Synchronous execution only — never fire-and-forget a suite and move on.
- Bootstrap only from the bundled router, byte-verbatim; never overwrite a
  repo's custom classifier without the user's explicit OK.
- Suite-worth evaluation is for web repos only.
- All edits ride the current feature branch, never the default branch.
