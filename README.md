# App Launcher Lite 🚀

An experiment: the full [app-launcher](https://github.com/ferraroroberto/app-launcher) with **GitHub Copilot CLI** as the only coding agent and **GitLab via `glab`** as the only forge. It is built and tested on one PC and installable on any Windows machine. From a phone (Android Chrome first, iOS still supported) you can open a live terminal into a Copilot session in any project folder, launch registered apps, fire one-shot or scheduled scripts, invoke team-os skills, and watch a four-column kanban of GitLab issues plus live agent sessions.

The companion repo [fleet-config-lite](https://github.com/ferraroroberto/fleet-config-lite) supplies the Copilot hooks (session state for the Board), the `glab`-based issue skills (`/issue-start`, `/issue-yolo`) and their `install.ps1`.

Stack: FastAPI + vanilla-JS PWA, Windows-only (ConPTY via pywinpty), Python 3.12+. Two processes behind a tray icon: the webapp on `:8465` and a detached PTY session-host on `:8466` that survives webapp restarts.

## Status

What runs and what does not is stated plainly, so nothing reads as working that was never observed.

**Runs and is tested here** (unit suite, browser suite, screenshot baseline on the build PC): the five work tabs plus Settings, the live PTY terminal and detached sessions, the Apps registry, Jobs with Task Scheduler under `\AppLauncherLite\`, the Team OS skill launcher, the Board's session columns, auth (bearer token, password, passkey), remote access, and the Copilot launch flags.

**Verified on Copilot CLI 1.0.105:** interactive and `-p` sessions both fire the four hooks (`sessionStart`, `userPromptSubmitted`, `agentStop`, `sessionEnd`); `events.jsonl` carries `session.usage_record` with `inputTokens` and `maxPromptTokens`; `--max-ai-credits` exists; `-p` plus `--session-id` resumes with context kept; the ask tool is `ask_user`.

**Unknown (not yet verified live):**

- GitLab, in part: group issue reads, issue create and note, and the two-remote mirror are verified live on gitlab.com; the merge-request rows, pipelines, labels, merge and a self-hosted host are `unknown`. See [docs/gitlab.md](docs/gitlab.md).
- On Copilot CLI 1.0.105: composer keys for the chief's doorbell, a queued `/compact` running at turn end, `/remote on` typed from the launcher, `sessionStart` `additionalContext` being honoured, `agentStop` blocking to force another turn, a background shell finishing waking the agent, and `ask_user` blocking under autopilot.
- Android device behaviour (install, safe areas, gesture bar, keyboard) — see [docs/android.md](docs/android.md).

**Not active until fleet-config-lite ships the chief (planned):** the chief features ported from app-launcher — plan, journal, mailbox, doorbell, compaction and dispatch — are present in the launcher code but need fleet-config-lite's chief helpers, which do not exist yet. Until then the Board shows no chief plan and nothing dispatches work.

**Planned port steps** (each independently shippable; none is done):

1. A probe script for the unknowns above.
2. Copilot transcript flavor and context percentage.
3. Plan and journal.
4. Mailbox.
5. `chief_ops` lite and chief settings.
6. A lite chief skill in fleet-config-lite.
7. Doorbell and compaction.
8. Guard hooks.
9. The merge path via `glab mr merge --when-pipeline-succeeds`.
10. Worktrees, claims and sweep.
11. Drain and owner checks.
12. Jobs feeding the chief.
13. `ui_proof`, the remote-control toggle and the Chat view.
14. A fresh-machine install checklist.

**Dropped from the full app** (not documented, not planned): Telegram, Life OS, inspiration, voice/OCR/TTS, usage and quota tiles, the context filter, other coding agents (Claude, Codex, Antigravity, Pi, Grok), GitHub Actions CI cards on the Board, screen time, the remote power guard, process auto-restart, the PC health row, the perf ledger and timings, fleet scope and `/delegate`.

## Quick start

```bat
git clone https://github.com/ferraroroberto/app-launcher-lite
cd app-launcher-lite
setup.bat        REM creates .venv + installs requirements (icons are committed)
tray.bat         REM tray icon -> webapp :8465 + session-host :8466
```

First-run configuration, in order (CLI install steps: [docs/installing-agent-clis.md](docs/installing-agent-clis.md)):

1. **Copilot CLI** — install GitHub's `copilot` CLI and log in (`/login` inside a session). The Coding tab disables its launch button until `copilot` resolves on `PATH`.
2. **glab** — install [glab](https://gitlab.com/gitlab-org/cli) and run `glab auth login` against your GitLab host. The Board's issue columns shell out to `glab api` on demand.
3. **Settings tab** (or `config/webapp_config.json`) — set **Projects dir** (the folder whose child directories the Coding tab lists), **GitLab group** (Board queries; subgroups included) and **GitLab host** if self-hosted. Copy `config/webapp_config.sample.json` to `config/webapp_config.json` to start from the template.
4. **fleet-config-lite** — clone it and run its `install.ps1` to install the Copilot hooks (they write `~/.copilot/hooks/state/sessions-state.json` for the Board) and the issue skills the Board's ▶/⚡ buttons invoke.
5. Optional: `config/config.json` (from `config.sample.json`) for `tailnet_host` and the webapp embed section; `config/apps.json` (from `apps.sample.json`) for the Apps registry.

**Phone access and auth.** Loopback always passes; non-loopback callers need a bearer token (`scripts/gen_token.py`, stored as `auth_token`). An optional login password (`python -m src.password set`; it stores only a salted hash in the git-ignored `config/app_password.json`) lets a fresh device swap the password for the token; the route answers `429` with `Retry-After` after six wrong passwords from one client (the PC itself is exempt). Terminal-grade routes (live PTY, transcripts, team-os private files, screen view) are additionally **private-network-only and passkey-gated**: they are refused over a public tunnel, and on a trusted network (Tailscale's CGNAT range or an allowlisted VPN subnet, see [Remote access](#remote-access--tailscale-or-any-vpn)) they require a WebAuthn platform passkey enrolled via the tray menu's **Enroll device (5 min)**. HTTPS comes from any `cert.pem`/`key.pem` pair in `webapp/certificates/`; `scripts/gen_tailscale_cert.py` is the Tailscale provisioner, checked on boot. An optional Cloudflare named tunnel (`webapp_tunnel_named.bat`) exposes only the non-terminal surfaces.

## The tabs

Five work surfaces plus Settings, in one tabbed PWA at `/`. A one-screen index per tab is in [docs/features.md](docs/features.md).

### Coding

Lists every child directory of `projects_dir` (no marker files needed; `projects_ignore` globs hide folders). One tap launches a **GitHub Copilot CLI** session in that folder, either as a launcher-owned ConPTY streamed live to the phone (typing, compose bar, paste, image attach, Ctrl-keys, scrollback) or as a **Detached** console window on the PC (listed and killable, but no phone terminal). **Resume** re-opens Copilot's own `--resume` session picker. Per-project ★ favorites sort to the top; a repo-issues button links to the project's GitLab issues page; running sessions can be renamed and stopped from the list. Launch flags come from the Copilot settings (see [Copilot models and flags](#copilot-models-and-flags)). Session reference: [docs/session-view.md](docs/session-view.md), [docs/interactive-terminal.md](docs/interactive-terminal.md).

A small **CPU reading** in the Coding header answers "is the PC loaded?": muted at rest, amber after about 30 s at or above 70%, red at or above 90%, and `–` when the server could not sample. Tapping it lists the busiest process groups by name and cores used, never paths or command lines. One shared sampler (`src/system_load.py`) feeds it, only while a page is asking.

### Apps

The registry in `config/apps.json` (gitignored; sample committed): one row per launcher `.bat`, each with a `kind` of `streamlit`, `webapp`, `tunnel` or `tray`. One tap spawns the bat detached; the Running panel lists live instances with their listening ports and per-instance stop. `kind: tray` rows are also auto-started at tray boot. **Scan for new apps** (Settings) walks `apps_scan_root` for candidate bats; leave it empty to scan nothing. Next to another launcher install on the same PC, keep it empty or point it at a folder holding only this install's apps, or the scan lists the other install's trays. A pre-change `apps.json` is kept as `config/apps.json.bak-<date>` (gitignored via `config/*.bak-*`).

### Jobs

Remote-fireable scripts backed by `config/jobs.json` (empty by default; sample committed; never a copy of another install's `jobs.json`, whose scheduled rows would run the same scripts twice). A job is defined once (script path, typed params, optional schedule) and every trigger (phone tap, scoped-token URL, Task Scheduler slot, webhook, chain) funnels through one executor that writes a uniform run record. Schedules materialise under `\AppLauncherLite\` in Windows Task Scheduler. Full reference: [docs/jobs-tab.md](docs/jobs-tab.md).

### Team OS

Surfaces the skills of a sibling `team-os` checkout (`team_os_dir`; skills live under `<team_os_dir>/.claude/skills`). One tap spawns a Copilot session whose working directory is the team-os repo and that auto-invokes `/<skill>`, with the same PTY/detached/resume options as the Coding tab and a per-launch model combo. A **weekly recap** tile shows the recap's freshness and launches the `/weekly-recap` review. The private-content browser (context, memory, examples, conversations) is private-network and passkey gated and path-jailed to `team_os_dir`.

### Board

A kanban over four computed columns: **Backlog** (open GitLab issues across `gitlab_group`), **Bot's turn** (live sessions working or idle), **Your turn** (sessions awaiting input, the number that matters) and **Done** (issues closed today). Session presence comes from the `:8466` session-host list; semantic status is joined from `sessions-state.json`, written by fleet-config-lite's Copilot hooks. GitLab data is fetched server-side through `glab` **on demand only** (the ↻ button or a stale tab activation), never on the 5 s poll, and is **not yet verified live**. Tapping a session card opens a drill-down drawer (last exchange plus a reply straight into the live PTY); a Backlog card whose repo exists locally carries **▶ Start / ⚡ YOLO** buttons that spawn `/issue-start N` / `/issue-yolo N`. Full reference: [docs/board.md](docs/board.md); the forge side: [docs/gitlab.md](docs/gitlab.md).

### Settings

Directories (projects dir and ignore globs, apps scan root, team-os dir), GitLab group and host, Copilot launch defaults, terminal scrollback, **Start App Launcher Lite at log on** (Startup-folder wrapper, no admin needed), a **Text size** control (Small / Default / Large, persisted in the browser), Save and **Scan for new apps**, edit mode, a **Screen → View live** row (the PC's desktop, live; see [docs/screen-view.md](docs/screen-view.md)), and a machine status readout. A second card mints **job-scoped API tokens** (safe to bake into a Stream Deck button URL; each fires only its chosen job; the raw token is shown once). The light/dark toggle sits in the page header; passkey enrollment is deliberately started from the PC tray menu, not from the phone.

## Configuration

There is no `.env`. Committed samples live next to each gitignored real file in `config/`: `config.sample.json`, `webapp_config.sample.json`, `apps.sample.json`, `jobs.sample.json`. The full key reference is [docs/config.md](docs/config.md); the keys you set on a fresh machine:

| File | Key | Default | Effect |
| --- | --- | --- | --- |
| `config.json` | `webapp.port` | `8465` | The one place the webapp port is set; the tray, `launcher.py webapp`, the `.bat` files (through `python -m scripts.webapp_port`) and `GET /api/config` all read it. |
| `config.json` | `tailnet_host` | `""` | Hostname or IP the phone reaches this PC on; builds the Apps tab's remote Open URLs. Empty disables. |
| `config.json` | `task_namespace` | `AppLauncherLite` | Task Scheduler folder the Jobs tab registers under, read at startup. |
| `webapp_config.json` | `projects_dir` | repo's parent | Folder whose child dirs the Coding tab lists. |
| `webapp_config.json` | `team_os_dir` | sibling `../team-os` | Team OS checkout root. |
| `webapp_config.json` | `gitlab_group` | `""` | GitLab group the Board reads (subgroups included). Empty keeps the panel dormant with a Settings hint. |
| `webapp_config.json` | `gitlab_host` | `""` | Self-hosted GitLab (rides glab's `GITLAB_HOST`). Empty means glab's default context. |
| `webapp_config.json` | `sessions_state_file` | `~/.copilot/hooks/state/sessions-state.json` | Session-status file written by fleet-config-lite's hook. |
| `webapp_config.json` | `copilot_models` | `["gpt-6-luna", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "mai-code-2-flash"]` | Model ids the pickers offer. Edited here, read-only in the UI. |
| `webapp_config.json` | `auth_token` | `""` | Bearer token required from non-loopback callers; empty means the gate is off. |
| `webapp_config.json` | `tailnet_allowlist` | `[]` | Extra IPs/CIDRs allowed on terminal routes beyond loopback and the Tailscale range. |
| `webapp_config.json` | `webauthn_rp_id` / `webauthn_origin` | `""` | Passkey relying party; both empty turns the passkey gate off. |

## Copilot models and flags

`src/launching/flags.py::build_copilot_flags()` turns the persisted knobs into the `copilot` argv:

- **Explicit model ids are tenant-gated.** `copilot_models` is a config list, not a hardcoded set; what an account offers varies, so edit it per install. An unavailable id falls back to auto and prints one line in the PTY at launch.
- `--model` is emitted only for a non-empty explicit id; `""` and `"default"` both mean "let Copilot pick (auto)" and omit the flag.
- `--effort` (`copilot_effort`) is emitted **only alongside an explicit model**; the auto model rejects it ("does not support reasoning effort configuration").
- `--context` (`copilot_context`) is emitted for `default` and `long_context`, omitted for `""`.
- `copilot_skip_permissions: true` adds `--allow-all`; `copilot_autopilot: true` adds `--autopilot`.
- Resume launches splice Copilot's native `--resume` ahead of the same flags, so Copilot renders its own session picker.

## Ports and processes

| Port | Process | Owner |
| --- | --- | --- |
| `:8465` | FastAPI webapp (HTTPS when a cert pair exists in `webapp/certificates/`) | Tray; killed and reclaimed by `tray.bat --restart`. |
| `:8466` | PTY session-host (loopback only, never network-reachable) | Spawned **detached** from the tray subtree; **excluded** from the restart reclaim sweep; re-adopted by the fresh tray. |

- **`tray.bat --restart`** is the canonical restart: an orphan-proof reclaim-then-start through the vendored `scripts/tray_lifecycle.ps1`. It **preserves live Coding/PTY sessions**; the session-host is deliberately not touched. Run it from PowerShell, not Git Bash.
- A change under `src/session_host.py` or `app/webapp/session_host/` is **not live** until `:8466` itself restarts. `GET /api/version` reports a `session_host` block whose `stale_relevant` field scopes staleness to session-host-relevant paths; `true` means such a change is merged but not yet running. The one supported restart is `C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -File scripts/restart-session-host.ps1 -Confirm`, operator-only because it kills every live PTY. Details: [docs/restart-and-liveness.md](docs/restart-and-liveness.md).
- The tray menu offers open/copy-URL (local, remote, Cloudflare), webapp restart, passkey enrollment, status and quit. **Copy remote URL** uses the Tailscale hostname when the CLI resolves one, else `tailnet_host`.
- Boot autostart (Settings toggle) drops a self-logging `AppLauncherLite.bat` wrapper into the user's Startup folder; each login attempt leaves breadcrumbs in `webapp/startup.log`.

## Verifying changes

- **Pre-ship gate:** `C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -File scripts/verify-before-ship.ps1`. It boots its own disposable webapp and session-host (no tray needed, no live sessions touched) and runs byte-compile, the non-browser suite, the ratchets, a diff-routed browser slice and the screenshot baseline. Stages and rules: [docs/verification.md](docs/verification.md).
- **Dev-loop smoke against the live tray:** `.\scripts\run-e2e.ps1` (sets `LAUNCHER_E2E_LIVE=1`; a bare `pytest tests/e2e` without the opt-in env vars exits with a guard message so ad-hoc runs cannot hit the webapp the phone is using).
- **Brand assets:** this repo commits app-launcher's rendered PWA/tray icon set verbatim. `.\.venv\Scripts\python.exe scripts\gen_icons.py` verifies the committed copies still match an app-launcher checkout (sibling `..\app-launcher`, or `--upstream`) and exits non-zero on drift; `--sync` copies the changed ones over for review. It is not part of the gate.
- **CI:** `.github/workflows/e2e.yml` runs the same gate for every PR and push to `main`. Advisory, not required; the local gate is the contract. See [docs/ci-github-actions.md](docs/ci-github-actions.md).

## Remote access — Tailscale or any VPN

Nothing in the launcher requires Tailscale; it is the zero-config default. The terminal gate is plain IP-based (`app/webapp/middleware.py::client_in_tailnet`): terminal-grade routes accept loopback, the Tailscale CGNAT range `100.64.0.0/10` and every entry in `tailnet_allowlist`. The `tailnet_*` names are historical; the values are generic.

**With Tailscale** everything works out of the box, and `scripts/gen_tailscale_cert.py` provisions a browser-trusted HTTPS cert for the MagicDNS name. Android Chrome needs that HTTPS to install the PWA ([docs/android.md](docs/android.md)).

**Without it (any VPN or LAN):**

1. **`tailnet_allowlist`** — add the VPN's client subnet (for example `"10.8.0.0/24"`) or individual client IPs. This alone unlocks the terminal-grade routes.
2. **`tailnet_host`** (`config.json`) — the hostname or IP the phone reaches this PC on. Feeds the Apps tab's Open URLs and the tray's **Copy remote URL** fallback.
3. **HTTPS** — drop any `cert.pem` + `key.pem` pair into `webapp/certificates/` (a private CA, `mkcert`, or a self-signed cert the phone trusts). `gen_tailscale_cert.py` is only the Tailscale provisioner; its boot-time renewal check no-ops on other certs.
4. **Passkeys** — set `webauthn_rp_id` (the bare hostname from step 2) and `webauthn_origin` (the full https origin). WebAuthn requires HTTPS on that exact hostname.

Either way, the public Cloudflare tunnel stays terminal-refused; the live terminal never rides a public edge.

## Deploying to another computer

The pair (this repo plus fleet-config-lite) installs on any Windows machine. The GitLab steps are verified on gitlab.com only.

1. **Clone both repos.** Run `setup.bat` here (venv and deps), then fleet-config-lite's `install.ps1` (renders the Copilot hook config into `~/.copilot/hooks/` and junctions its skills into `~/.copilot/skills/`). Restart any open Copilot session afterwards; hook configs load at CLI startup only.
2. **glab against your GitLab host.** `glab auth login`, set `gitlab_host` and `gitlab_group` in Settings, then validate one real response shape: `glab api "groups/<group>/issues?state=opened&per_page=1"`, comparing fields with the fixtures in `tests/unit/board/test_gitlab_client.py`. If the instance returns a different shape, fix the fixtures and `src/clients/gitlab.py` together. See [docs/gitlab.md](docs/gitlab.md).
3. **Copilot login and model gate.** Log the `copilot` CLI in, then check which ids of `copilot_models` the account offers and trim the list to the real set. If none is available, set `copilot_model` to `""` (auto).
4. **Team OS repo.** Point `team_os_dir` at the checkout whose `.claude/skills` the Team OS tab should list. Skills can live in any repo: Copilot discovers whatever sits in `~/.copilot/skills/`, so a separate skills repo works like fleet-config-lite's own once its skill folders are junctioned in. The tab lists only `team_os_dir`'s skills; the junctions are what every Copilot session (including Board ▶/⚡ launches) can invoke.
5. **Verify the hooks.** Confirm `~/.copilot/hooks/state/sessions-state.json` appears after a `copilot -p "hi" --allow-all-tools` run, and again after an interactive session. On Copilot CLI 1.0.105 both fire the four hooks; re-test after each `copilot update`.
6. **Auth and boot.** `scripts/gen_token.py` (bearer token, required before terminals connect), optionally `python -m src.password set` and a passkey; flip the boot-autostart toggle in Settings and check `webapp/startup.log` on the next login. No Tailscale? Follow [Remote access](#remote-access--tailscale-or-any-vpn).
7. **Probe the real-agent reconnect.** No e2e test runs the reconnect-replay check against a real `copilot` boot (the suite uses a lightweight stub child). Where a real `copilot` echoes a typed marker end to end, add a scenario to `tests/e2e/terminal/test_terminal_reconnect.py`'s flow that types a marker, drops the WebSocket and asserts the marker count in the xterm buffer does not grow after the reconnect.

## Bringing features over from app-launcher

app-launcher keeps growing. This repo tracks it through the `[upstream]` watermark in `.fleet.toml` and the `/upstream-sync` skill, which surveys merged upstream work and ports each item through the normal issue workflow. Anything that depends on a dropped agent or forge is skipped or adapted to Copilot and GitLab.

## Documentation

| Doc | What it covers |
| --- | --- |
| [docs/features.md](docs/features.md) | One screen per tab: what each does. |
| [docs/config.md](docs/config.md) | Every config key. |
| [docs/board.md](docs/board.md) | Board tab: columns, `glab` reads, state-file join, drill-down, security boundary. |
| [docs/gitlab.md](docs/gitlab.md) | The GitLab side: `glab` setup, what is read and written, dual remote, an Android Git client. |
| [docs/jobs-tab.md](docs/jobs-tab.md) | Jobs tab: schema, kinds, schedules, webhooks, alerting, coverage, watchdog. |
| [docs/android.md](docs/android.md) | Android Chrome as the primary phone: install, HTTPS, what to check. |
| [docs/installing-agent-clis.md](docs/installing-agent-clis.md) | Installing and signing in to the Copilot CLI and `glab`. |
| [docs/session-view.md](docs/session-view.md) | Opening, reading, answering and stopping a session from the phone. |
| [docs/interactive-terminal.md](docs/interactive-terminal.md) | The interactive terminal pane. |
| [docs/launcher-owned-pty.md](docs/launcher-owned-pty.md) | The ConPTY terminal architecture, security model and gotchas. |
| [docs/detached-stop-decision.md](docs/detached-stop-decision.md) | Why detached sessions get a single kill-style Stop. |
| [docs/screen-view.md](docs/screen-view.md) | The live read-only desktop view. |
| [docs/documents.md](docs/documents.md) | The Documents reader. |
| [docs/lid-keep-awake.md](docs/lid-keep-awake.md) | Keeping the PC awake with the lid closed. |
| [docs/iphone-debugging.md](docs/iphone-debugging.md) | Debugging the PWA on an iPhone. |
| [docs/cache-hygiene-asset-hashing.md](docs/cache-hygiene-asset-hashing.md) | Content-hash asset stamping and `/api/version` build identity. |
| [docs/restart-and-liveness.md](docs/restart-and-liveness.md) | Restart recipes and how to tell a change is live. |
| [docs/run-troubleshooting.md](docs/run-troubleshooting.md) | Run-time troubleshooting. |
| [docs/job-run-state.md](docs/job-run-state.md) | The job run-state model. |
| [docs/ci-github-actions.md](docs/ci-github-actions.md) | What the CI workflow runs and why it stays advisory. |
| [docs/e2e-conventions.md](docs/e2e-conventions.md) | Browser-test conventions. |
| [docs/ux-surface-map.md](docs/ux-surface-map.md) | Every screen, menu, sheet, dialog, Settings row, gesture and key the app has, with how each is reached; generated from `docs/ux-surfaces.yaml`. |
| [docs/layout.md](docs/layout.md) | Where code goes: folders, subpackages and the declared root exceptions. |
| [docs/verification.md](docs/verification.md) | The pre-ship gate and the dev-loop commands. |
| [docs/architecture.mmd](docs/architecture.mmd) | Hand-authored Mermaid diagram of the internal structure. |

## License

MIT — see [LICENSE](LICENSE).
