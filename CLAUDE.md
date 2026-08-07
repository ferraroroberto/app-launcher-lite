# Project Instructions

Canonical instructions for AI coding agents working in this repository. Claude Code reads this file directly as project memory; other agents reach it via the one-line `AGENTS.md` pointer.

## This repository

**App Launcher Lite** — phone-first launcher hub, a lite fork of [ferraroroberto/app-launcher](https://github.com/ferraroroberto/app-launcher) for environments where Copilot is the only agent. One coding agent only: **GitHub Copilot CLI** (`src/agents.py`; config-driven `copilot_models` list, default `gpt-5.6-luna`). One forge only: **GitLab via `glab`** (`src/gitlab_client.py`; `gitlab_group` / `gitlab_host` config). Five work tabs plus Settings: **Coding** (live PTY / detached Copilot sessions via the `:8466` session-host, project list from `projects_dir`), **Apps** (registry `config/apps.json`: `streamlit` | `webapp` | `tunnel` | `tray`), **Jobs** (`config/jobs.json`, Task Scheduler-backed), **Team OS** (skills from `<team_os_dir>/.claude/skills`, launched as Copilot sessions), **Board** (4 columns: Backlog · Bot's turn · Your turn · Done; GitLab issues via `glab` on demand, session status from fleet-config-lite's `sessions-state.json`). Companion repo: [fleet-config-lite](https://github.com/ferraroroberto/fleet-config-lite) — Copilot hooks (`~/.copilot/hooks/state/`) + the `/issue-start` / `/issue-yolo` skills the Board's ▶/⚡ buttons launch + `install.ps1`. See `README.md` for setup and usage; `docs/board.md` and `docs/jobs-tab.md` for the deep references.

**Project specifics:**

- **Stack:** FastAPI + vanilla JS — **not** Streamlit; do not introduce Streamlit. Windows-only (ConPTY via pywinpty).
- **Config & secrets:** there is no `.env`. App-level config in `config/config.json`, runtime UI prefs + secrets in `config/webapp_config.json`, app registry in `config/apps.json`, jobs in `config/jobs.json` — all gitignored with committed `*.sample.json` templates. The `webapp_config.json` field list lives in `src/webapp_config.py` (dataclass + serializer); keep the sample and README table in sync with it.
- **Ports:** webapp `:8465` (tray-owned), session-host `:8466` (detached, loopback-only). `tray.bat` is self-contained — the lifecycle helper `scripts/tray_lifecycle.ps1` is vendored in-repo, no fleet-config dependency.
- **Copilot flag semantics** (`src/launch_flags.py::build_copilot_flags`, verified on CLI 1.0.70): explicit model ids are **tenant-gated** — `copilot_models` is a config list, edited per install; `""`/`"default"` means omit `--model` (Copilot auto); `--effort` is only emitted alongside an explicit model (the auto model rejects it); `--context` is emitted for `default`/`long_context`; `copilot_skip_permissions` → `--allow-all`; `copilot_autopilot` → `--autopilot`.
- **GitLab reads** go through `glab` as a subprocess, **never on a poll** — `src/gitlab_client.py::refresh()` runs only on the Board's ↻ or stale tab activation; the 5 s poll reads the in-memory snapshot. Real API shapes are pinned in `tests/test_gitlab_client.py` fixtures — when the work instance's responses differ, fix fixtures and client together. All `glab`/session-host calls are mocked at client seams in the unit suite, so `glab` need not be installed to run tests.
- **Verification:** before declaring any webapp/launcher/session-host change done, run the pre-ship gate `pwsh -File scripts/verify-before-ship.ps1` (boots its own disposable webapp + session-host — no tray needed). Byte-compile + the non-e2e pytest suite always run; the **browser slice is diff-proportionate**, routed by `scripts/classify_e2e.py` against `main` — static-asset-only diff → Chromium-only smoke, backend/docs-only → no browser suite, any `.js`/`.css`/real-page/session-host/e2e-test change (or any ambiguous diff) → the full suite, Chromium driving both a desktop and a phone (Android device-emulated) projection (Android-only fork, no WebKit/iPhone engine). Routing is fail-safe: uncertainty escalates to full, never narrows; the chosen tier prints to console and `webapp/verify-progress.log`. Dev-loop smoke against the live tray: `.\scripts\run-e2e.ps1` (sets `LAUNCHER_E2E_LIVE=1`; a bare `pytest tests/e2e` without opt-in env vars exits with a guard). Non-browser suite alone: `pytest tests -m "not smoke" -v`.
- **Restart and verify before hand-off:** the webapp has no hot-reload — code edits do nothing until the `:8465` process restarts. The canonical restart is **`tray.bat --restart`** (orphan-proof reclaim-then-start; run it via the PowerShell tool, not Git Bash). It kills the tray subtree and reclaims `:8465` by `.venv`-scoped PID, but **preserves the `:8466` session-host and every live PTY session** — the session-host is spawned detached and excluded from the reclaim sweep. After restarting, confirm the new build with a bounded poll of `GET /api/version` (`git_sha` matches `HEAD`, `asset_hash` changed) and report that line.
- **A session-host change is not live until `:8466` itself restarts — `tray.bat --restart` never does that.** Check `GET /api/version`'s `session_host.stale_relevant`: `true` → report the change as **merged but not yet live**, never as shipped; `false` with `stale: true` → the repo moved on but nothing the session-host loads changed. The one supported restart is `pwsh -File scripts/restart-session-host.ps1 -Confirm` — operator-initiated only, at a clean boundary: it kills every live PTY on the machine. Never wire it into a normal ship flow.

## session-host

- what/why: the `:8466` session-host (`src/session_host.py`, `app/session_host/`) hosts the user's live Coding/PTY sessions. Spawned detached from the tray subtree (`cmd /c start`) so a `taskkill /T` subtree kill can't reach it; deliberately excluded from `tray.bat --restart`'s port-reclaim sweep.
- update command: `scripts/restart-session-host.ps1 -Confirm` (confirmation-gated, operator-only — kills every live PTY)
- liveness signal: `GET /api/version`'s `session_host.stale_relevant` (raw sha-diff fact in `session_host.stale`; `stale_relevant` scopes it to whether a declared path here was touched)
- NOT restarted/deployed by: `tray.bat --restart`

## Internal architecture

[`docs/architecture.mmd`](docs/architecture.mmd) is a hand-authored Mermaid diagram of this repo's structure (CLI entrypoint, tray-owned webapp + detached session-host, FastAPI routers, shared `src/` library, external deps — Task Scheduler, `glab`, team-os, fleet-config-lite's state files). Update it in the same PR as any material structural change — same anti-staleness contract as `.fleet.toml`'s `description`.

## UX surface

*Design-conformance gate block — the product is the FastAPI + static PWA under `app/webapp/`.*

- design spec applies: yes
- paths:
  - app/webapp/static/**/*.css
  - app/webapp/static/**/*.{js,html}
- key views:                      # single tabbed SPA served at `/`
  - /          (Coding · Apps · Jobs · Team OS · Board · Settings tabs)
- accepted exceptions:            # permanent, re-confirmed by each /design-sync run
  - `app-icon-family` FAILs by design and stays FAIL (issue #10). The contract requires `project-scaffolding`'s `brand_gen.render_set`, which by definition is not available alongside this fork — `scripts/gen_icons.py` is a documented stub. The PWA/tray/favicon assets (`app/webapp/static/icon-180|192|512|512-maskable.png`, `favicon.ico`, `assets/tray/app-launcher.ico`) are committed byte-for-byte from upstream `app-launcher` and are shape-correct; index + manifest wiring passes. Re-sync by copying those files from an upstream checkout when its brand changes — never by adding a `project-scaffolding` dependency, which the fork exists to avoid. Do not re-file this as drift.

## CI expectations

- Workflow `.github/workflows/e2e.yml`, job `verify-before-ship`, on every PR and push to `main`. **Advisory, not required** (no branch protection) — the local gate is the contract. On CI the full Chromium suite always runs (no diff routing).
- The job self-caps at 20 min; `pytest-timeout` (120 s, thread method) aborts any single hung test with a stack dump. The e2e suite caps Playwright's default action/navigation timeout at 15 s (`tests/e2e/conftest.py`, `E2E_DEFAULT_TIMEOUT_MS`) so hangs fail fast with a named locator.
- Timing-headroom env vars CI widens: `E2E_LOG_POLL_DEADLINE_MS` (input delivery), `E2E_STOP_OVERLAY_HIDE_MS` (kill-from-terminal overlay), `E2E_REAL_AGENT_ECHO_MS` (real-agent echo — the one real-`copilot` reconnect test is currently skipped pending a probe on a deployment machine, see README's deployment checklist). A single red in these timing classes is a flake, not the diff — rerun once.
- Convention — mock non-deterministic boot fetches before `goto()`, don't widen timeouts; and never assert on a raw `evaluate()`/`bounding_box()` read of a polled-re-render surface (board columns rebuild every 5 s) — prefer auto-retrying `expect()` assertions, or wrap unavoidable raw reads in `tests/e2e/conftest.py::stable_read`.
- CI's only signal beyond the local gate is the e2e suite. Its surface = `app/webapp/`, the session-host/PTY layer (`src/session_host*.py`, `src/launcher.py`), `tests/e2e/`, and static assets — a diff touching none of these gains nothing from CI.
