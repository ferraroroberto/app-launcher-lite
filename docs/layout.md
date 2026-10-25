# Layout

Where code goes in this repo. Its shape is `webapp-tray` (declared as `shape` in `.fleet.toml`): a FastAPI + static PWA app owned by a Windows tray. The canonical tree and placement rules for the shape live in project-scaffolding's `docs/repo-layout.md`; this page is the repo's own map. The structure ratchet (`scripts/structure_ratchet.py`, run by `scripts/structure_ratchet_gate.py` inside `scripts/verify-before-ship.ps1`) holds the root folders to the shape and stops files, functions and directories from growing.

## Top-level folders

| Folder | Holds |
| --- | --- |
| `app/` | The UI layer: CLI, tray, session-host process, the FastAPI webapp and its static PWA. |
| `src/` | The logic layer. Never imports `app/`; a helper both need lives here. |
| `tests/` | The pytest suite; `tests/e2e/` is the Playwright browser suite. |
| `scripts/` | Ops CLIs, the pre-ship gate and the vendored ratchets. Never imported by `app/`. |
| `config/` | Committed `*.sample.json` templates; the real config files are gitignored. |
| `assets/` | Generated icons the app ships: the tray `.ico` and the Stream Deck button. |
| `docs/` | Durable reference, this page included. |
| `webapp/` | Declared extra root, see below. |

Beside them sit the entry points (`launcher.py`, `tray.bat`, `webapp.bat`, `webapp_tunnel_named.bat`, `setup.bat`) and the hidden `.claude/` (the repo's `verify` skill) and `.github/` (the advisory e2e workflow).

## `app/`

| Path | Holds |
| --- | --- |
| `app/cli/` | Argparse dispatcher only; one module per command under `commands/`. |
| `app/tray/` | The pystray icon that owns the webapp, plus single-instance, watchdog and Tailscale helpers. |
| `app/session_host/` | The loopback PTY host process on `:8466`. |
| `app/webapp/` | `server.py` (app factory), `manager.py` (adopt-or-spawn of uvicorn), `middleware.py`, `observability.py`, `event_loop.py`. |
| `app/webapp/routers/` | Thin routers: parse the request, call `src/`, return. One module per concern. |
| `app/webapp/static/` | The single-page shell (`index.html`, `styles/` for the CSS, one script per view and per terminal concern), the PWA manifest and icons. |
| `app/webapp/static/_vendored/` | Vendor-verbatim components, never edited here. |
| `app/webapp/static/vendor/` | The vendored xterm.js bundle. |

## `src/`

`src/` is a flat directory of domain-prefixed modules. By domain: `app_config.py`, `webapp_config.py` and `registry.py` (config and the apps registry); `agents.py` and `launch_flags.py` (the Copilot agent and its flags); `session_host*.py`, `session_client.py`, `vt_snapshot.py` and `launcher.py` (PTY sessions); `board*.py` and `gitlab_client.py` (the Board and its GitLab reads); `jobs*.py` (the Jobs backend); `webauthn_gate.py`, `api_tokens.py` and `audit.py` (auth and audit); the rest are small shared helpers (`subprocess_flags.py`, `git_run.py`, `env_path.py`, `build_info.py`).

| Subpackage | Holds |
| --- | --- |
| `src/jobs_kinds/` | One executor per job kind (batch, http_check, inline_shell, powershell, python, shell_wsl). |
| `src/notify/` | Notification providers behind `notifications.py`. |

`src/` is past the 40-module limit, so it takes no new module: new logic goes in a `src/<domain>/` subpackage.

## `tests/`

The pytest suite sits in `tests/unit/<domain>/`, one `test_<module>.py` per module under test, grouped by domain: `board`, `jobs`, `run_job` (the executor), `session_host`, `webapp` (routers, config, server), `platform` (scanner, agents, tray, diagnostics, notifications) and `tooling` (the ratchet, e2e-routing and guard tests). `tests/conftest.py` holds the shared fixtures; a folder's own `conftest.py` and `_helpers` modules hold what its tests share. `tests/e2e/` holds the Playwright suite: the cross-cutting tests sit beside `conftest.py`, the Jobs, Coding and terminal tests in `tests/e2e/jobs/`, `coding/` and `terminal/`, and the screenshot capture in `tests/e2e/screens/`. A new test goes in the matching domain folder, never beside `conftest.py`.

## Where new code goes

- New endpoint: `app/webapp/routers/<concern>.py`, request parsing and response only; the logic goes in `src/<domain>/`.
- New view or terminal concern: its own script under `app/webapp/static/`, linked from `index.html`; never another block in a baselined script.
- New logic module: a `src/<domain>/` subpackage.
- New ops script: `scripts/`. A helper the app imports belongs in `src/`.

## Declared exceptions

- **`webapp/`** is the tray-owned webapp's runtime-state folder (certificates, logs, briefs, the pid and tunnel files, all gitignored). It also tracks two sample files, `cloudflared.sample.yml` and `terminal-themes.sample.json`, which sit next to the real copies they template. It is listed in `extra_roots` because the tray and the webapp read and write it at that path.
- **Baselined offenders.** `.structure-ratchet-baseline.json` records each file, function and directory already over a limit at its size when the ratchet was adopted. They may not grow; they are split in their own changes, not hidden. The largest are `src/session_host.py` and `app/webapp/static/index.html`.

## Stylesheets

`app/webapp/static/styles/` holds the app's own rules, cut into files of at most 1,000 lines. `index.html` links them one by one, and that link order is the cascade: a rule in a later file wins a tie against an earlier one exactly as it did when they were a single sheet.

- **Tokens, tabs and cards** live in `base.css`; it stays first.
- **A view's rules go in that view's file**: `code.css` (Coding tiles, git flags, favorites), `jobs.css`, `settings.css`, `terminal.css` (running sessions and the terminal overlay), `team-os.css` and `board.css`. Rules several views share go in `shared.css` (empty state, toast), `dialogs.css` or `lists.css` (collapsible cards, option rows, card lists).
- **A rule that overrides another stays after it.** A new file goes where its rules must win, not alphabetically.
- **A file that would pass 1,000 lines splits at a section comment**, never inside a block, and its link goes right after the file it was cut from.
- **Cache-busting is automatic**: `src/static_versioning.py` stamps every `href="/static/styles/*.css"` with the fleet hash, so a new file needs only its `<link>`. `index.html` is itself at its baselined size, so a new link is paid for by trimming elsewhere in the page.
