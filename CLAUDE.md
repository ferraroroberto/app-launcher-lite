# Project Instructions

## This repository

**App Launcher Lite** — phone-first launcher hub, a lite fork of [ferraroroberto/app-launcher](https://github.com/ferraroroberto/app-launcher) for Copilot-only environments. **One coding agent only: GitHub Copilot CLI** (`src/agents.py`; config-driven `copilot_models` list, default `gpt-5.6-luna`). **One forge only: GitLab via `glab`** (`src/gitlab_client.py`; `gitlab_group` / `gitlab_host` config). Setup and usage: `README.md`; deep references: `docs/board.md`, `docs/jobs-tab.md`.

Five work tabs plus Settings:

- **Coding** — live PTY / detached Copilot sessions via the `:8466` session-host; project list from `projects_dir`.
- **Apps** — registry `config/apps.json`, kinds `streamlit` | `webapp` | `tunnel` | `tray`.
- **Jobs** — `config/jobs.json`, Task Scheduler-backed.
- **Team OS** — skills from `<team_os_dir>/.claude/skills`, launched as Copilot sessions.
- **Board** — 4 columns: Backlog · Bot's turn · Your turn · Done; GitLab issues via `glab` on demand; session status from fleet-config-lite's `sessions-state.json`.

Companion repo [fleet-config-lite](https://github.com/ferraroroberto/fleet-config-lite) — Copilot hooks (`~/.copilot/hooks/state/`), the `/issue-start` / `/issue-yolo` skills the Board's ▶/⚡ buttons launch, and `install.ps1`.

**Project specifics:**

- **Stack:** FastAPI + vanilla JS — **not** Streamlit; do not introduce Streamlit. Windows-only (ConPTY via pywinpty).
- **Config & secrets:** no `.env`. `config/config.json` (app-level), `config/webapp_config.json` (runtime UI prefs + secrets), `config/apps.json`, `config/jobs.json` — all gitignored with committed `*.sample.json` templates. The `webapp_config.json` field list lives in `src/webapp_config.py` (dataclass + serializer); keep the sample and README table in sync with it.
- **Ports:** webapp `:8465` by default (tray-owned; the one setting is `config.json` `webapp.port`, read through `src/app_config.py::resolve_webapp_port` and, for the `.bat` files, `scripts/webapp_port.py`), session-host `:8466` (detached, loopback-only). `tray.bat` is self-contained — `scripts/tray_lifecycle.ps1` is vendored in-repo, no fleet-config dependency.
- **Copilot flag semantics** (`src/launch_flags.py::build_copilot_flags`, verified on CLI 1.0.70): explicit model ids are **tenant-gated** — `copilot_models` is a config list, edited per install; `""`/`"default"` means omit `--model` (Copilot auto); `--effort` is only emitted alongside an explicit model (the auto model rejects it); `--context` is emitted for `default`/`long_context`; `copilot_skip_permissions` → `--allow-all`; `copilot_autopilot` → `--autopilot`.
- **GitLab reads** go through `glab` as a subprocess, **never on a poll** — `src/gitlab_client.py::refresh()` runs only on the Board's ↻ or stale tab activation; the 5 s poll reads the in-memory snapshot. Real API shapes are pinned in `tests/unit/board/test_gitlab_client.py` fixtures — when the work instance's responses differ, fix fixtures and client together. All `glab`/session-host calls are mocked at client seams in the unit suite, so `glab` need not be installed to run tests.
- **Verification:** before declaring any webapp/launcher/session-host change done, run the pre-ship gate `C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -File scripts/verify-before-ship.ps1` (boots its own disposable webapp + session-host — no tray needed). Stages, the static and docs ratchets, the diff-routed browser slice and the dev-loop commands: [`docs/verification.md`](docs/verification.md).
- **Ratchets (structure, instruction, doc-refs, dead-code, conventions, comment):** each `scripts/<name>_ratchet_gate.py` wraps the vendored `scripts/<name>_ratchet.py`, runs in the pre-ship gate and fails on a finding beyond `.<name>-ratchet-baseline.json`. Exit 3 — git unusable, or a file could not be read, parsed, scanned or measured — fails as NOT CONFIRMED, never a skip. Never `--allow-additions` without review. Dead-code and conventions read the whole tree and are never diff-routed.
  - **Structure:** a file past 1,000 lines, a Python/JS function past 100, a directory past 40 modules or a `src`→`app` import fails unless baselined at no more than its baselined size; a root folder outside the `webapp-tray` shape plus `extra_roots` in `.fleet.toml` fails. `src/` sits just under the 40-module limit, so new modules go in a subpackage (`src/<domain>/`); tests go in a domain folder (`tests/unit/<domain>/`, `tests/e2e/<domain>/`). Where code goes: `docs/layout.md`.
  - **Instruction:** a rule hit of the weekly prompt audit's deterministic tier (R-01 to R-12, R-15 to R-17, R-30, R-34 to R-36, R-45) in `CLAUDE.md`, `AGENTS.md`, `.claude/rules/*.md` or a `SKILL.md` rising above baseline, or a file past its size cap (R-14: 200 lines for `CLAUDE.md`), fails. Reword the line rather than baseline it.
  - **Doc-refs:** a repo path, `python -m` module, relative link or loopback port named in `README.md`, `CLAUDE.md` or `docs/**/*.md` that does not exist and is not in the baseline fails; the webapp's own `port` in `.fleet.toml` resolves by itself and the session-host port is in its `allow_refs`. Point the reference at what exists, or drop it.
  - **Dead-code:** a new function, class or method that nothing in the tracked Python tree refers to fails; route handlers, `__all__`, tests, fixtures and decorated hooks are never reported, and vendored copies are read but not reported. Delete the definition or call it; a real framework hook the rules miss goes in `[dead_code_ratchet]` `allow` / `entry_decorators` in `.fleet.toml`.
  - **Conventions:** a file whose hit count rises for an enabled rule fails: `no-window` (a subprocess spawn without `creationflags`), `logging-not-print` (`print` in `src/` or `app/` library code), `no-drive-path` (a drive-letter path literal), `secret-compare` (a `compare_digest` outside `src/api_tokens.py`), `git-wrapper` (a `git` spawn outside `src/git_run.py`), `must-use` (a bare `requests` call outside `src/_loopback_http.py`) and `subprocess-timeout` (a blocking `subprocess.run`/`call`/`check_*` with no `timeout=`; size it to the call and give its expiry its own error); test modules are held to `no-window` only. Route the call through the helper, or give it a reasoned `# <rule>-exempt: <why>` marker.
  - **Comment:** a file whose count of `#NNN` issue references inside code comments rises, or a new file carrying any, fails; docstrings, tests and vendored components are not counted. A comment states the present-tense why; the story lives in the issue and `git log`. Reword a reference away in the file you touch, then run `python scripts/comment_ratchet.py --update-baseline`.
- **Screenshot baseline:** the gate's last stage runs `scripts/shot_baseline.py` (vendored from project-scaffolding, with `tests/e2e/_shot.py`) over `tests/e2e/shots` — the five work tabs at phone and desktop width, light and dark, captured by `tests/e2e/screens/test_screen_shots.py` from route-mocked fictional data, local gate only. A move-only change touches no PNG and quotes the final line, `shot baseline: PASS -- N of N committed shot(s) recaptured, 0 drifted`. **A deliberate UI change re-baselines in the same PR:** `LAUNCHER_E2E_AUTOBOOT=1 python -m pytest tests/e2e/screens/test_screen_shots.py`, check `git diff --stat -- tests/e2e/shots`, then `git add tests/e2e/shots`. A drift that is not deliberate means something on screen is unpinned: fix the capture, never widen the bands. Exit 3 is NOT CONFIRMED and fails.
- **Restart and verify before hand-off:** the webapp has no hot-reload — code edits do nothing until the `:8465` process restarts. Canonical restart: **`tray.bat --restart`** (orphan-proof reclaim-then-start; run it via the PowerShell tool, not Git Bash). It kills the tray subtree and reclaims `:8465` by `.venv`-scoped PID, but **preserves the `:8466` session-host and every live PTY session**. After restarting, confirm the new build with a bounded poll of `GET /api/version` (`git_sha` matches `HEAD`, `asset_hash` changed) and report that line.
- **A session-host change is not live until `:8466` itself restarts — `tray.bat --restart` never does that.** Gate on `GET /api/version`'s `session_host.stale_relevant` (fields and restart path: `## session-host` below): `true` → report the change as **merged but not yet live**, never as shipped; `false` with `stale: true` → the repo moved on but nothing the session-host loads changed. The one supported restart — `C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -File scripts/restart-session-host.ps1 -Confirm` — is operator-initiated only, at a clean boundary; never wire it into a normal ship flow.

## session-host

- what/why: the `:8466` session-host (`src/session_host.py`, `app/webapp/session_host/`, `app/webapp/__init__.py`) hosts the user's live Coding/PTY sessions, and loads `src/agents.py`, `src/audit.py`, `src/build_info.py`, `src/env_path.py`, `src/session_host_text.py`, `src/subprocess_flags.py` and `src/vt_snapshot.py` (`tests/unit/session_host/test_session_host_paths.py` fails when an import is missing from this list). Spawned detached from the tray subtree (`cmd /c start`) so a `taskkill /T` subtree kill can't reach it; deliberately excluded from `tray.bat --restart`'s port-reclaim sweep.
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

## CI expectations

- Workflow `.github/workflows/e2e.yml`, job `verify-before-ship`, on every PR and push to `main`. **Advisory, not required** (no branch protection) — the local gate is the contract. On CI the full Chromium suite always runs (no diff routing).
- The job self-caps at 20 min; `pytest-timeout` (120 s, thread method) aborts any single hung test with a stack dump. The e2e suite caps Playwright's default action/navigation timeout at 15 s (`tests/e2e/conftest.py`, `E2E_DEFAULT_TIMEOUT_MS`) so hangs fail fast with a named locator.
- Timing-headroom env vars CI widens: `E2E_LOG_POLL_DEADLINE_MS` (input delivery), `E2E_STOP_OVERLAY_HIDE_MS` (kill-from-terminal overlay), `E2E_REAL_AGENT_ECHO_MS` (real-agent echo — the one real-`copilot` reconnect test is currently skipped pending a probe on a deployment machine, see README's deployment checklist). A single red in these timing classes is a flake, not the diff — rerun once.
- Convention — mock non-deterministic boot fetches before `goto()`, don't widen timeouts; and never assert on a raw `evaluate()`/`bounding_box()` read of a polled-re-render surface (board columns rebuild every 5 s) — prefer auto-retrying `expect()` assertions, or wrap unavoidable raw reads in `tests/e2e/conftest.py::stable_read`.
- CI's only signal beyond the local gate is the e2e suite. Its surface = `app/webapp/`, the session-host/PTY layer (`src/session_host*.py`, `src/launcher.py`), `tests/e2e/`, and static assets — a diff touching none of these gains nothing from CI.
