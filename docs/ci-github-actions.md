# CI on GitHub Actions

This repo's own continuous integration: one workflow, `.github/workflows/e2e.yml`, job `verify-before-ship`, on every pull request into `main` and every push to `main`. It is unrelated to the Board, which reads GitLab and shows no CI state.

## What it is for

The local pre-ship gate (`scripts/verify-before-ship.ps1`, see [verification.md](verification.md)) is the contract. CI runs the same script on a runner that has never seen the repo, which is the one thing a local run structurally cannot do:

- a fresh `.venv` built from `requirements.txt`, so a dependency that only exists on the dev machine, or an unpinned one that has drifted, shows up red;
- committed files only, at the commit rather than a working tree, so a file nobody committed shows up red;
- no gitignored config, no tray, no agent CLI on `PATH`, so a test that passes only because of local state shows up red.

CI is advisory (no branch protection): the local gate decides whether a change is done, CI is the safety net behind it.

## The workflow

| Step | Why |
|---|---|
| `runs-on: windows-2025` | `pywinpty` is Windows-only and the suite includes real-ConPTY tests. |
| Checkout, Python 3.12 | A fresh machine has nothing. |
| Create `.venv`, install `requirements.txt` | `verify-before-ship.ps1` requires the venv interpreter under `.venv/Scripts/`; the install is itself part of what the job checks. |
| Seed `config/` from the samples | `config.json`, `webapp_config.json` and `apps.json` are gitignored and only the `*.sample.json` templates are committed. Loopback access bypasses the bearer-token middleware, so the sample values are enough for the suite to run instead of skipping. |
| Run `scripts/verify-before-ship.ps1` | The gate itself. |
| Upload `webapp/verify-progress.log` | Runs even on failure: per-test START/DONE lines and the slowest-test summary make a red diagnosable from the run page. |

The job times itself out in the workflow file, so a wedge fails fast instead of running to GitHub's ceiling. `concurrency` cancels a superseded run on the same ref.

Which browser tier runs on CI is decided by `scripts/e2e-gate-route.ps1`, not by the workflow, and the gate's own output names the tier it chose. Counts and timings of a healthy run are deliberately not quoted here: they move with every test added, and the workflow's header comment and `CLAUDE.md`'s CI block are where they live.

## Reading a red

- A single red in a timing-sensitive e2e class (input delivery, the kill-from-terminal overlay) is a flake: rerun once. The classes and their tuning variables are in [e2e-conventions.md](e2e-conventions.md).
- A red that names a missing file, a missing package or a config key is the clean machine doing its job: fix the repo, not the runner.
- A red that passes locally on the same commit points at unpinned dependencies first: compare the versions CI installed with the local `.venv`.

## A skip is not a pass

A fixture that skips when a gitignored file is missing would cascade the skip through everything downstream and still exit 0. A green run that skipped its tests is worse than a red one, so the workflow seeds the config files from the samples to make the suite run for real. The one fixture that may still skip on a bare machine is the one that needs the real agent CLI; the deterministic stub PTY covers every UI assertion that does not depend on the agent's own rendering.
