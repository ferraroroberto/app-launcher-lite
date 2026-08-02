# App Launcher Lite 🚀

Phone-first launcher hub for a work PC — a lite fork of [ferraroroberto/app-launcher](https://github.com/ferraroroberto/app-launcher) rebuilt around **GitHub Copilot CLI** (the only coding agent) and **GitLab via `glab`** (the only forge). From the phone you can: open a live terminal into a Copilot session in any project folder, launch registered apps, fire one-shot or scheduled scripts, invoke team-os skills, and watch a four-column kanban of GitLab issues + live agent sessions. The companion repo [ferraroroberto/fleet-config-lite](https://github.com/ferraroroberto/fleet-config-lite) supplies the Copilot hooks (session-state for the Board) and the `glab`-based issue skills (`/issue-start`, `/issue-yolo`) plus their `install.ps1`.

Stack: FastAPI + vanilla-JS PWA, Windows-only (ConPTY via pywinpty), Python 3.12+. Two processes behind a tray icon: the webapp on `:8465` and a detached PTY session-host on `:8466` that survives webapp restarts.

## Quick start

```bat
git clone https://github.com/ferraroroberto/app-launcher-lite
cd app-launcher-lite
setup.bat        REM creates .venv + installs requirements (icons are committed)
tray.bat         REM tray icon -> webapp :8465 + session-host :8466
```

First-run configuration, in order:

1. **Copilot CLI** — install GitHub's `copilot` CLI and log in (`/login` inside a session works too). The Coding tab disables its launch button until `copilot` resolves on PATH.
2. **glab** — install [glab](https://gitlab.com/gitlab-org/cli) and run `glab auth login` against your GitLab host. The Board's issue columns shell out to `glab api` on demand.
3. **Settings tab** (or `config/webapp_config.json`) — set **Projects dir** (the folder whose child directories the Coding tab lists), **GitLab group** (Board issue queries; subgroups included), and **GitLab host** if self-hosted. Copy `config/webapp_config.sample.json` → `config/webapp_config.json` to start from the template.
4. **fleet-config-lite** — clone it and run its `install.ps1` to install the Copilot hooks (they write `~/.copilot/hooks/state/sessions-state.json` for the Board) and the lite issue skills the Board's ▶/⚡ buttons invoke.
5. Optional: `config/config.json` (from `config.sample.json`) for `tailnet_host` (Apps-tab remote URLs) and the webapp embed section; `config/apps.json` (from `apps.sample.json`) for the Apps tab registry.

**Phone access & auth.** The auth model is unchanged from upstream: loopback always passes; non-loopback callers need a bearer token (`scripts/gen_token.py`, stored as `auth_token`); an optional login password (`scripts/set_password.py`) lets a fresh device swap the password for the token. Terminal-grade routes (live PTY, transcripts, team-os private files) are additionally **private-network-only + passkey-gated**: they are refused over a public tunnel, and on a trusted network (Tailscale's CGNAT range or an allowlisted VPN subnet — see [Remote access](#remote-access--tailscale-or-any-vpn)) they require a WebAuthn platform passkey enrolled via the tray menu's **Enroll device (5 min)**. HTTPS comes from any `cert.pem`/`key.pem` pair in `webapp/certificates/` — `scripts/gen_tailscale_cert.py` is the Tailscale provisioner, checked on boot. An optional Cloudflare named tunnel (`webapp_tunnel_named.bat`) exposes only the non-terminal surfaces.

## The tabs

Five work surfaces plus Settings, in one tabbed PWA at `/`.

### Coding

Lists every child directory of `projects_dir` (no marker files needed; `projects_ignore` globs hide folders). One tap launches a **GitHub Copilot CLI** session in that folder — either a launcher-owned ConPTY streamed live to the phone (full interactive terminal: typing, compose bar with predictive keyboard, paste, image attach, Ctrl-keys, scrollback), or a **Detached** console window on the PC (listed and killable, but no phone terminal). **Resume** re-opens Copilot's own `--resume` session picker. Per-project ★ favorites sort to the top; a repo-issues button links to the project's GitLab issues page; running sessions can be renamed and stopped from the list. Launch flags come from the Copilot settings in the options card (see [Copilot models & flags](#copilot-models--flags)).

### Apps

The registry in `config/apps.json` (gitignored; sample committed) — one row per launcher `.bat`, each with a `kind`: `streamlit` | `webapp` | `tunnel` | `tray`. One tap spawns the bat detached; the Running panel lists live instances with their listening ports and per-instance stop. `kind: tray` rows are also auto-started sequentially at tray boot (Registered Trays). **Scan for new apps** (Settings) walks `apps_scan_root` for candidate bats; edit mode reveals rename/remove.

### Jobs

Remote-fireable scripts backed by `config/jobs.json` (empty by default; sample committed). A job is defined once — script path, typed params, optional schedule — and every trigger (phone tap, scoped-token URL, Task Scheduler slot, webhook, chain) funnels through one executor producing a uniform run record (status, duration, output log, artifacts). Schedules materialise under `\AppLauncher\` in Windows Task Scheduler; kinds cover `python`, `powershell`, `batch`, `inline-shell`, `shell-wsl`, and `http-check`. Failure alerts (Pushover global / Telegram per-job), missed-fire coverage, watchdog kills, run-history search — the full reference is [docs/jobs-tab.md](docs/jobs-tab.md).

### Team OS

Surfaces the skills of a sibling `team-os` checkout (`team_os_dir`; skills live under `<team_os_dir>/.claude/skills`). One tap spawns a Copilot session cwd'd in the team-os repo that auto-invokes `/<skill>` — with the same PTY/detached/resume options as the Coding tab and a per-launch model combo. A **weekly recap** tile shows the recap's freshness and launches `/weekly-recap` review. The private-content browser (context/memory/examples/conversations) is private-network + passkey gated and path-jailed to `team_os_dir`.

### Board

A read-only fleet kanban over four computed columns — **Backlog** (open GitLab issues across `gitlab_group`), **Bot's turn** (live sessions working/idle), **Your turn** (sessions awaiting a decision, input, or stalled — the number that matters), **Done** (issues closed today). Session presence comes from the `:8466` session-host list; semantic status (working / awaiting-input / …) is joined from `sessions-state.json`, written by fleet-config-lite's Copilot hooks. GitLab data is fetched server-side via `glab` **on demand only** (the ↻ button or stale tab activation) — never on the 5 s poll. Tapping a session card opens a drill-down drawer (last exchange + reply straight into the live PTY); a Backlog card whose repo exists locally carries **▶ Start / ⚡ YOLO** buttons that spawn `/issue-start N` / `/issue-yolo N` (skills shipped by fleet-config-lite). Full reference: [docs/board.md](docs/board.md).

### Settings

Directories (projects dir + ignore globs, apps scan root, team-os dir), GitLab group/host, terminal scrollback, **Start App Launcher Lite at log on** (Startup-folder wrapper, no admin needed), Save + **Scan for new apps**, edit mode, and a machine status readout. A second card mints **job-scoped API tokens** (safe to bake into a Stream Deck button URL — each fires only its chosen job; the raw token is shown once). The light/dark theme toggle sits in the page header; passkey enrollment is deliberately started from the PC tray menu, not from the phone.

## Configuration reference

There is no `.env`. Committed samples live next to each gitignored real file in `config/`.

### `config/config.json` (app-level)

| Key | Default | Effect |
| --- | --- | --- |
| `log_level` | `INFO` | Root logging level. |
| `tailnet_host` | `""` | Any hostname/IP the phone reaches this PC on (Tailscale MagicDNS name, VPN DNS name, or static VPN/LAN IP) — builds the Apps tab's remote Open URLs and the tray's Copy-remote-URL fallback. Empty disables. |
| `webapp` | `{}` | Embed section for the tray-spawned webapp: `enabled` (default true), `host`, `port`. |

### `config/webapp_config.json` (UI prefs + secrets, written by "Save")

All keys optional — missing keys fall back to `src/webapp_config.py` defaults.

**Network**

| Key | Default | Effect |
| --- | --- | --- |
| `host` / `port` | `0.0.0.0` / `8465` | Webapp bind. |
| `session_host_port` | `8466` | Loopback-only PTY session-host port; must differ from `port`. |

**Coding / Apps / Team OS**

| Key | Default | Effect |
| --- | --- | --- |
| `projects_dir` | repo's parent | Folder whose child dirs the Coding tab lists. |
| `projects_ignore` | `[]` | Folder-name globs hidden from the Coding tab (VCS/build dirs always skipped). |
| `coding_favorites` | `[]` | Starred project ids — managed by the per-tile ★. |
| `coding_hidden_agents` | `[]` | Launch buttons hidden from project rows (agent ids plus the pseudo-id `github` for the repo-issues button). |
| `apps_scan_root` | repo's parent | Root the Apps-tab scan walks for launcher bats. |
| `team_os_dir` | sibling `../team-os` | Team OS checkout root (skills at `.claude/skills`). |

**Board / GitLab**

| Key | Default | Effect |
| --- | --- | --- |
| `sessions_state_file` | `~/.copilot/hooks/state/sessions-state.json` | Board session-status file written by fleet-config-lite's hook. |
| `gitlab_group` | `""` | GitLab group the Board's `glab` queries span (subgroups included). Empty = dormant panel with a Settings hint. |
| `gitlab_host` | `""` | Self-hosted GitLab instance (rides glab's `GITLAB_HOST` env var). Empty = glab's default context. |

**Copilot launch settings**

| Key | Default | Effect |
| --- | --- | --- |
| `copilot_models` | `["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]` | Model ids offered in the UI pickers — edit here, read-only from the UI (ids are tenant-gated). |
| `copilot_model` | `gpt-5.6-luna` | Persisted `--model` value. `""` or `"default"` = let Copilot pick (auto). |
| `copilot_skip_permissions` | `false` | Opt-in `--allow-all`. |
| `copilot_autopilot` | `true` | `--autopilot`. |
| `copilot_context` | `long_context` | `--context` (`""` \| `default` \| `long_context`; `""` = omit). |
| `copilot_effort` | `xhigh` | `--effort` (`""`, `none`…`max`; `""` = omit; only emitted alongside an explicit model). |

**Auth & terminal**

| Key | Default | Effect |
| --- | --- | --- |
| `auth_token` | `""` | Bearer token required from non-loopback callers. Empty = gate off. Generate with `scripts/gen_token.py`. |
| `auth_password` | `""` | Optional login-overlay password that hands the token to a fresh device (`scripts/set_password.py`). |
| `tailnet_allowlist` | `[]` | Extra IPs/CIDRs allowed on terminal routes beyond loopback + the Tailscale CGNAT range — how a non-Tailscale VPN enables the terminal (add its client subnet). |
| `show_local_window` | `true` | Phone-launched sessions also open a PC mirror terminal window. |
| `terminal_history_lines` | `10000` | Session-host scrollback replayed on (re)connect (bounds 200–50000). |
| `webauthn_rp_id` / `webauthn_origin` | `""` | Passkey relying party (the bare hostname the phone browses to / full https origin — Tailscale or any VPN DNS name). Both empty = passkey gate off. |
| `webauthn_rp_name` | `App Launcher Lite` | Display name in the passkey prompt. |

**Jobs notifications & coverage**

| Key | Default | Effect |
| --- | --- | --- |
| `pushover_api_token` / `pushover_user_key` | `""` | Pushover credentials for the global failure channel. |
| `notify_on_failure` | `false` | Master switch — one push per failed run. |
| `notify_failure_streak` | `0` | Also push when N consecutive failures stack up (0 = off). |
| `telegram_bot_token` / `telegram_chat_id` | `""` | Credentials for per-job Telegram alerts (`Job.alert_on_failure`). |
| `jobs_coverage_interval_minutes` | `60` | Background missed-fire coverage scan interval (0 = poll-only). |

**Secrets & tokens**

| Key | Default | Effect |
| --- | --- | --- |
| `secrets` | `{}` | Named secret values referenced from jobs.json as `$secret:<key>` (webhook secrets, job env). |
| `api_tokens` | `[]` | Job-scoped bearer-token records minted from Settings — salted hashes only, don't hand-edit. |

### `config/apps.json` and `config/jobs.json`

`apps.json`: `{scan_root, apps: [{id, name, kind, bat_path, added_at}]}` with `kind` ∈ `streamlit` | `webapp` | `tunnel` | `tray`. `jobs.json`: `{jobs: [...]}` — see [docs/jobs-tab.md](docs/jobs-tab.md) for the full job schema (kinds, schedules, params, webhooks, env/secrets, alerting).

## Copilot models & flags

`src/launch_flags.py::build_copilot_flags()` turns the persisted knobs into the `copilot` argv. Semantics verified on Copilot CLI 1.0.70:

- **Explicit model ids are tenant-gated.** The `copilot_models` list is a config list, not a hardcoded set — what your tenant actually offers varies, so edit the list per install. An unavailable id errors visibly in the PTY at launch.
- `--model` is emitted only for a non-empty explicit id; `""` and the `"default"` sentinel both mean "let Copilot pick (auto)" and omit the flag.
- `--effort` is emitted **only when an explicit model is set** — the auto model rejects it outright ("does not support reasoning effort configuration"). An empty/default model therefore drops both `--model` and `--effort`.
- `--context` is emitted for `default`/`long_context`, omitted for `""`.
- `copilot_skip_permissions: true` adds `--allow-all`; `copilot_autopilot: true` adds `--autopilot`.
- Resume launches splice Copilot's native `--resume` ahead of the same flags, so Copilot renders its own session picker.

## Ports & processes

| Port | Process | Owner |
| --- | --- | --- |
| `:8465` | FastAPI webapp (HTTPS when a cert pair exists in `webapp/certificates/`) | Tray — killed and reclaimed by `tray.bat --restart`. |
| `:8466` | PTY session-host (loopback-only, never network-reachable) | Spawned **detached** from the tray subtree; **excluded** from the restart reclaim sweep; re-adopted by the fresh tray. |

- **`tray.bat --restart`** is the canonical restart: orphan-proof reclaim-then-start via the vendored `scripts/tray_lifecycle.ps1` (no external repo dependency). It **preserves live Coding/PTY sessions** — the `:8466` session-host is deliberately not touched.
- Consequence: a change under `src/session_host.py` or `app/session_host/` is **not live** until `:8466` itself restarts. `GET /api/version` reports a `session_host` block whose `stale_relevant` field scopes staleness to session-host-relevant paths — `true` means such a diff is merged but not yet running. The one supported restart is `pwsh -File scripts/restart-session-host.ps1 -Confirm` — deliberate and operator-only, because it kills every live PTY on the machine (bare, without `-Confirm`, it prints the warning and exits 1).
- The tray menu offers open/copy-URL (local, remote, Cloudflare), webapp restart, passkey enrollment, status, and quit. **Copy remote URL** uses the Tailscale hostname when the CLI resolves one, else the configured `tailnet_host`.
- Boot autostart (Settings toggle) drops a self-logging `AppLauncherLite.bat` wrapper into the user's Startup folder; each login attempt leaves breadcrumbs in `webapp/startup.log`.

## Verifying changes

- **Pre-ship gate** — `pwsh -File scripts/verify-before-ship.ps1`. Byte-compiles `app src tests`, runs the non-e2e pytest suite (~1270 tests), then a **diff-proportionate** browser slice routed by `scripts/classify_e2e.py` against `main`: static-asset-only diff → Chromium smoke; backend/docs-only → no browser suite; any real UI/session-host/e2e change (or ambiguity) → the full Chromium + WebKit/iPhone dual-projection suite. It boots its own disposable webapp + session-host — no tray needed, and it never touches live sessions. Progress streams to `webapp/verify-progress.log`.
- **Dev-loop smoke against the live tray** — `.\scripts\run-e2e.ps1` (sets `LAUNCHER_E2E_LIVE=1`; a bare `pytest tests/e2e` without the opt-in env vars exits with a guard message so ad-hoc runs can't hit the webapp the phone is using).
- **Non-browser suite alone** — `.\.venv\Scripts\python.exe -m pytest tests -m "not smoke"`.
- **CI** — `.github/workflows/e2e.yml` runs the same gate on `windows-2025` for every PR and push to `main`. **Advisory, not required** — the local gate is the contract. See [docs/ci-github-actions.md](docs/ci-github-actions.md).

## Remote access — Tailscale or any VPN

Nothing in the launcher *requires* Tailscale — it is just the zero-config default. The terminal gate is plain IP-based (`app/webapp/middleware.py::client_in_tailnet`): terminal-grade routes accept loopback, the Tailscale CGNAT range `100.64.0.0/10`, and every entry in `tailnet_allowlist`. The `tailnet_*` config names are historical; the values are generic.

**With Tailscale** everything works out of the box: the CGNAT range passes the gate with no config, and `scripts/gen_tailscale_cert.py` provisions a browser-trusted HTTPS cert for the MagicDNS name.

**Without it — any VPN or LAN:**

1. **`tailnet_allowlist`** (`webapp_config.json`) — add the VPN's client subnet (e.g. `"10.8.0.0/24"`) or individual client IPs. This alone unlocks the terminal-grade routes.
2. **`tailnet_host`** (`config.json`) — the hostname or IP the phone reaches this PC on (VPN DNS name or a static VPN IP). Feeds the Apps tab's Open URLs and the tray's **Copy remote URL** fallback.
3. **HTTPS** — drop any `cert.pem` + `key.pem` pair into `webapp/certificates/` (corporate CA, `mkcert`, or a self-signed cert the phone trusts). `gen_tailscale_cert.py` is only the Tailscale provisioner; its boot-time renewal check is issuer-keyed and no-ops on other certs.
4. **Passkeys** — set `webauthn_rp_id` (bare hostname from step 2) and `webauthn_origin` (full https origin). WebAuthn requires HTTPS on that exact hostname, hence step 3.

Either way, the public Cloudflare tunnel stays terminal-refused — the live terminal never rides a public edge.

## Deploying to a new machine

This fork was built and verified on its original dev machine, with the GitLab/Copilot tenant specifics mocked where the real services weren't reachable. To stand the pair up anywhere else:

1. **Clone both repos** — this one plus [fleet-config-lite](https://github.com/ferraroroberto/fleet-config-lite). Run `setup.bat` here (venv + deps), then fleet-config-lite's `install.ps1` (renders the Copilot hook config into `~/.copilot/hooks/` and junctions its skills into `~/.copilot/skills/`). Restart any open Copilot session afterwards — hook configs load at CLI startup only.
2. **glab against your GitLab host** — `glab auth login`, set `gitlab_host` + `gitlab_group` in Settings, then validate one real response shape: run `glab api "groups/<group>/issues?state=opened&per_page=1"` and compare fields against the fixtures in `tests/test_gitlab_client.py` (`references.full`, `closed_at`, `web_url`). If the instance returns a different shape, fix the fixtures and `src/gitlab_client.py` together.
3. **Copilot login + model gate** — log the `copilot` CLI in, then check which of `gpt-5.6-luna` / `gpt-5.6-terra` / `gpt-5.6-sol` the tenant actually offers; trim `copilot_models` in `webapp_config.json` to the real set (an unavailable id fails visibly at launch). If none are available, set `copilot_model` to `""` (auto).
4. **Team OS repo** — point `team_os_dir` at the checkout whose `.claude/skills` the Team OS tab should list. **Skills can live in any repo**: Copilot discovers whatever sits in `~/.copilot/skills/` regardless of where it came from, so a separate team skills repo works exactly like fleet-config-lite's own — junction its skill folders in (or `copilot skill add <dir>`), no code change here. The Team OS *tab* lists only `team_os_dir`'s skills; the `~/.copilot/skills/` junctions are what every Copilot session (including Board ▶/⚡ launches) can invoke.
5. **Verify the hooks** — confirm `~/.copilot/hooks/state/sessions-state.json` appears after a `copilot -p "hi" --allow-all-tools` run. **Known CLI limitation (1.0.70):** the hooks fire reliably in `-p` runs but NOT in interactive TUI sessions — upstream bugs [copilot-cli#991](https://github.com/github/copilot-cli/issues/991) / [#2201](https://github.com/github/copilot-cli/issues/2201). Until a CLI update fixes that, Board sessions show status "unknown" (presence still works); re-test with the deployed CLI version and after each `copilot update`.
6. **Auth + boot** — `scripts/gen_token.py` (bearer token, required before terminals connect), optionally `scripts/set_password.py` + a passkey; flip the boot-autostart toggle in Settings and verify `webapp/startup.log` gets a breadcrumb on the next login. **No Tailscale on the machine?** Follow [Remote access — Tailscale or any VPN](#remote-access--tailscale-or-any-vpn): allowlist the VPN subnet, set `tailnet_host`, provide a cert pair, point the WebAuthn fields at that hostname.
7. **Un-skip the real-agent echo probe** — `tests/e2e/test_terminal_reconnect.py`'s reconnect-replay test is `@pytest.mark.skip`-ped pending a machine where a real `copilot` boot echoes end-to-end; re-enable it there and tune `E2E_REAL_AGENT_ECHO_MS` if needed.

## Porting features from upstream

The upstream [app-launcher](https://github.com/ferraroroberto/app-launcher) keeps growing (multi-agent Coding tab, fleet-chief chat, voice dictation, read-aloud, GitHub board, system map…). This fork deliberately tracks none of that. To adopt a specific upstream feature, ask an agent to "bring feature X from app-launcher" — pointing it at the upstream repo/PR — rather than merging upstream wholesale; the forks have diverged at the agent/forge layer and a blind merge will not apply cleanly.

## Documentation

| Doc | What it covers |
| --- | --- |
| [docs/board.md](docs/board.md) | Board tab reference — columns, `glab` queries, state-file join, drill-down, security boundary. |
| [docs/jobs-tab.md](docs/jobs-tab.md) | Jobs tab reference — schema, kinds, schedules, webhooks, alerting, coverage, watchdog. |
| [docs/launcher-owned-pty.md](docs/launcher-owned-pty.md) | The ConPTY terminal architecture, security model, and gotchas. |
| [docs/detached-stop-decision.md](docs/detached-stop-decision.md) | Why detached sessions get a single kill-style Stop. |
| [docs/cache-hygiene-asset-hashing.md](docs/cache-hygiene-asset-hashing.md) | Content-hash asset stamping + `/api/version` build identity. |
| [docs/iphone-debugging.md](docs/iphone-debugging.md) | DevTools against a real iPhone from the Windows PC. |
| [docs/ci-github-actions.md](docs/ci-github-actions.md) | What the CI workflow runs and why it stays advisory. |
| [docs/architecture.mmd](docs/architecture.mmd) | Hand-authored Mermaid diagram of this repo's internal structure. |

## License

MIT — see [LICENSE](LICENSE).
