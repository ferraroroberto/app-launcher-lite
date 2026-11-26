# Config

There is no `.env`. Every real config file is gitignored and has a committed `*.sample.json` template next to it in `config/`; copy a sample to its real name to start from it. Missing keys fall back to the defaults in code.

## `config/config.json`

Cross-surface settings read by the tray, the CLI and the server (defaults in `src/app_config.py`):

```json
{
  "log_level": "INFO",
  "tailnet_host": "pc.example-tailnet.ts.net",
  "webapp": { "enabled": true, "host": "0.0.0.0", "port": 8465, "session_host_port": 8466 }
}
```

| Key | Default | Effect |
| --- | --- | --- |
| `log_level` | `INFO` | Root logging level. |
| `tailnet_host` | `""` | Any hostname or IP the phone reaches this PC on (a Tailscale MagicDNS name, a VPN DNS name, a static VPN or LAN IP). It builds the Apps tab's remote **Open** URLs (`<scheme>://<tailnet_host>:<port>/`, scheme auto-detected per app by a TLS probe) and the tray's copy-remote-URL fallback. Empty disables both; the Open button is then shown disabled with a hint. |
| `task_namespace` | `AppLauncherLite` | Task Scheduler folder the Jobs tab registers entries under (`\<name>\<job id>`): one name of letters, digits, `_`, `.` or `-`, read at startup. |
| `webapp` | `{}` | Embed section for the tray-spawned webapp: `enabled` (default true), `host`, `port` (default `8465`). **`webapp.port` is the one place the webapp port is set** — the tray, `scripts/launcher.py webapp`, `scripts/webapp.bat`, `tray.bat`'s restart reclaim, the PC mirror window URL and `GET /api/config` all read it (the `.bat` files through `python -m scripts.webapp_port`). A machine that serves on another port carries `"webapp": { "port": <n> }`; without the key every reader falls back to `8465`. `webapp.session_host_port` (default `8466`, must differ from `port`) is the same for the loopback session-host: the webapp, the tray, `restart-session-host.ps1` and the session-host spawn read it, and the tray's single-instance mutex is `Global\app-launcher-lite-<webapp port>-tray`. |

The `webapp` section also takes three tuning knobs whose defaults are almost always right: `startup_timeout_seconds` (`15.0`, how long the tray waits for the webapp to answer before declaring the boot failed), `request_timeout_seconds` (`1.0`, per health-probe timeout) and `poll_interval_seconds` (`0.4`, gap between probes). `server` (`"hypercorn"`) picks the server `app.webapp.serve` runs: `"hypercorn"` speaks HTTP/2 to browsers over TLS and HTTP/1.1 to clients that offer only that; `"uvicorn"` is the HTTP/1.1-only fallback. Any other value stops the tray at boot with an error naming the key; a change takes effect on `tray.bat --restart`.

## `config/webapp_config.json`

UI preferences and secrets, written by the web UI's **Save**. The field list lives in `src/webapp_config.py` (dataclass and serializer); the sample file and this page track it.

The file may carry a byte-order mark, which some Windows editors write. A file that cannot be read as a JSON object, such as one with a syntax error, is never written over: the app logs a warning, a save is refused with the reason and the file stays as it was. Until it is fixed or moved away, the app does not use what is in it.

### Network

| Key | Default | Effect |
| --- | --- | --- |
| `host` | `0.0.0.0` | Webapp bind address. The webapp *port* is `config.json`'s `webapp.port`; a leftover `port` key in an older file is ignored, except in a linked git worktree, where the `port` that worktree setup writes here wins so a worktree webapp never binds the primary's port. A leftover `session_host_port` is ignored too: `webapp.session_host_port` sets it. |
| `serve_over` | `auto` | How the webapp is served, applied at the next restart (Settings > Connections > **Serve over**): `auto` is HTTPS when `webapp/certificates/` holds `cert.pem` and `key.pem`, else HTTP; `https` serves HTTPS (HTTP until a certificate exists); `http` never uses TLS; `both` adds plain HTTP on `serve_http_port` beside HTTPS. See [Remote access](../README.md#remote-access--tailscale-or-any-vpn). |
| `serve_http_port` | `8475` | The plain-HTTP port `both` adds; with `both` it must differ from the webapp and session-host ports. If another program holds it, the start logs a warning and serves HTTPS only. |

### Coding, Apps and Team OS

| Key | Default | Effect |
| --- | --- | --- |
| `projects_dir` | parent of this repo | Folder whose direct child directories the Coding tab lists. |
| `projects_ignore` | `[]` | Case-insensitive folder-name globs (`*`, `?`) hidden from the Coding tab; VCS and build directories are always skipped. |
| `coding_favorites` | `[]` | Starred project ids, managed by the per-tile ★. |
| `coding_hidden_agents` | `[]` | Launch buttons hidden from project rows: agent ids (only `copilot` here) plus the pseudo-ids `vscode` (the whole project ⋮ menu) and `github`. A *hidden* list, so anything new appears by default. |
| `coding_favorite_agent` | `copilot` | The agent whose launch button stays on a project row. Only `copilot` exists here, and an unknown id is saved as the default. |
| `apps_scan_root` | parent of this repo | Root the Apps-tab scan walks for launcher bats. Empty scans nothing; set it empty when another launcher install shares the PC. |
| `mini_apps_root` | `""` | Optional folder of small helper bats; a scan lists each `.bat` directly inside it in the Apps tab's **Mini apps** group, autostart off. Empty hides the group. Read on the next webapp start. |
| `team_os_dir` | sibling `../team-os` | Team OS checkout root (skills at `.claude/skills`). When that directory is missing the tab shows disabled. |
| `fleet_config_dir` | this repo's `copilot/` | Folder with the Copilot hooks, chief helpers and chief skill. The chief runs in it; the Board, the active-issue claims and the owner checks load their helpers from it. Empty means the default, and a path wins, so a machine that still holds the old fleet-config-lite path must repoint it: `copilot/install.ps1 -Migrate` offers to ([migration](migrating-from-fleet-config-lite.md)). |

### Board and GitLab

| Key | Default | Effect |
| --- | --- | --- |
| `sessions_state_file` | `~/.copilot/app-launcher-lite-state/sessions-state.json` | Session-status file written by the Copilot hooks in `copilot/hooks/`. Absent, corrupt or stale degrades to an `unknown` status, never an error. |
| `gitlab_group` | `""` | GitLab group or user namespace (subgroups included) the Board's `glab` queries span. Empty keeps the panel dormant with a Settings hint. |
| `gitlab_host` | `""` | Self-hosted GitLab instance, passed to every `glab` call (the Board's and the chief's) as `GITLAB_HOST`. Empty takes the host of the checkout's `origin` when that is not GitHub, else gitlab.com. |

What is verified live against GitLab is listed in [GitLab](gitlab.md#verified-live-and-not).

### Machine health

| Key | Default | Effect |
| --- | --- | --- |
| `llm_hub_url` | `""` | Base URL of the local hub on this computer. The PC load sheet's Health row and its ports, commit and disk rows come from `GET <base>/admin/api/machines/health`, read by a background sampler once per 30 s with a 3 s limit, only while a page is open and visible. Empty reads `Not confirmed` with the reason. Set in Settings > Local hub (a blank address turns the hub off, **Use the local hub** fills the hub-lite address), or in the file. A saved address takes effect at once, with no restart. Settings accepts only `http` or `https` on this PC or a private-network address with no path, and saves it only when `<base>/healthz` answers; a refusal names the reason and keeps the saved value. Job failure summaries use the same hub when `notify_failure_summary` is on. |

### Copilot launch settings

`src/launching/flags.py::build_copilot_flags` turns these into the `copilot` argv. Semantics verified on Copilot CLI 1.0.70:

| Key | Default | Effect |
| --- | --- | --- |
| `copilot_models` | `["gpt-6-luna", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "mai-code-2-flash"]` | Model ids the pickers offer. Edited here or in Settings > Agents > GitHub Copilot. A saved list replaces this default, so Settings offers an Add button for any default id the saved list lacks. **Explicit ids are tenant-gated**: which ids an account may use depends on its plan, so edit the list per install; Copilot falls back to its own pick and prints one line when an id is unavailable. |
| `copilot_model` | `gpt-6-luna` | Saved `--model`. `""` or `"default"` means let Copilot pick (auto) and omits the flag. |
| `copilot_effort` | `xhigh` | `--effort` (`""`, `none` … `max`; `""` omits it). Emitted **only alongside an explicit model**: the auto model rejects it, so an empty or default model drops both flags. |
| `copilot_context` | `long_context` | `--context` for `default` or `long_context`; `""` omits it. |
| `copilot_autopilot` | `true` | Adds `--autopilot`. |
| `copilot_skip_permissions` | `false` | Opt-in `--allow-all` (every tool permission). |
| `copilot_remote_session` | `true` | Remote access for every Copilot session Lite launches (Coding, Board and chief, Team OS), shown in Settings as **Remote session**. Copilot has no launch flag for it: it is the user settings `ahpEnvironment.acceptRemoteConnections` and `ahpEnvironment.publishNewSessionsByDefault`, which Lite sets together before each Copilot launch (`copilot config`, skipped when they already match). `false` sets both off, so a new session starts unshared. Those two settings live in `~/.copilot/settings.json` (or `$COPILOT_HOME`) and are shared by every Copilot session on the PC, including ones started outside Lite; a session reads them as it starts, so a running session keeps its state. Probed on CLI 1.0.114. |
| `copilot_web_url_prefix` | `""` | Override only. A full-control Copilot session's **Copy link** gives `https://<host>/copilot/tasks/<task id>`, where `<host>` is the GitHub Enterprise host the Copilot CLI is signed in to (`lastLoggedInUser.host` in `~/.copilot/config.json`, or under `$COPILOT_HOME`). A github.com sign-in derives no link. Set a full URL prefix, e.g. `https://ghe.example.com/copilot/tasks/`, only when the derived one is wrong or to give a github.com account a link; empty, or not an `http(s)` URL, keeps the derived one. See [session view](session-view.md#copy-link). |
| `launch_presets` | `[]` | Named Copilot launches: `{id, name, agent: "copilot", model, effort, context?}` rows (at most 12, unique names), written through `POST /api/config` (an `id` is assigned when absent) or from Settings › Coding agent › Launch presets, which adds, edits, renames and removes them; a refused write is a 400 whose body carries `field` (`name`, `model`, `effort`, `context`, or empty for the list) and the row `index`. `GET /api/config` gives the editor `copilot.preset_efforts` (each listed model's efforts) and `copilot.preset_contexts`. The Projects launch toolbar offers a dropdown with Default and one row per preset whose model is in `copilot_models`; a launch from a preset sends `--model`, `--effort` and, when the preset has a `context` (`default` or `long_context`), `--context`; without one the saved `copilot_context` applies. `effort` must be one the model offers: `mai-code-2-flash` takes `low`, `medium`, `high`, `xhigh` and `max`, `gpt-6-luna` takes `none` through `max` but not `minimal` (both probed), any other model takes any level the CLI accepts and the CLI decides. The launch route refuses a model outside `copilot_models`. |

The `.sample` file carries a `copilot_models` default list; a per-launch model (Team OS, Board) follows the same rules, and an empty or `default` override means no `--model` and no `--effort`.

### Usage card

| Key | Default | Effect |
| --- | --- | --- |
| `copilot_monthly_credits` | `300` | Settings > Coding agent > **Monthly allowance** (AI credits, 1–100000). A fallback only: the Code tab's Usage card takes the allowance from the Copilot plan as the CLI last cached it, and names this value when that cannot be read. |

### Chief

Settings > Chief, plus the chief's state files. The behaviour behind each switch is described in [The chief](board.md#the-chief).

| Key | Default | Effect |
| --- | --- | --- |
| `chief_model` | `gpt-6-luna` | The chief's Copilot model; the chief needs the helpers in `copilot/` ([README](../README.md#status)). A value outside `copilot_models` shows as not offered in Settings > Chief, and a start uses Copilot's own pick and returns a `warning`. |
| `chief_context` | `""` | The chief's `--context` (`""`, `default`, `long_context`). `""` follows `copilot_context`, so an install that never sets it launches the chief as before. |
| `chief_effort` | `""` | The chief's `--effort` (`""`, `none` … `max`), with the same only-beside-an-explicit-model rule as `copilot_effort`. `""` follows `copilot_effort`. |
| `chief_worker_cap` | `3` | The most worker sessions the chief runs at once, 1–10. The chief itself never counts. |
| `chief_worker_cap_scope` | `chief` | What the cap counts: `chief` only the sessions the chief started, `all` every live session. |
| `skills_dir` | `""` | Folder holding the `issue-add`, `issue-start`, `issue-yolo` and `issue-finish` skills a lane runs (Settings > Chief > **Skills folder**). Empty uses the ones bundled in the Copilot folder; a lane start is refused while one is missing. See [installing the Copilot side](../copilot/docs/install.md). |
| `chief_doorbell` | `false` | Type one wake-up line into an idle chief whose mailbox holds events. |
| `chief_fallback_minutes` | `30` | Ring the idle chief once when mail has waited this long, or worker slots sat free beside queued plan rows; 0–1440, `0` off. Independent of the doorbell. |
| `chief_rering_seconds` | `60` | Wait before ringing again after a ring that did not land, 30–600. |
| `chief_unread_alert_minutes` | `15` | Unread mail this old moves the chief to Your turn, 5–240. |
| `chief_reoffer_minutes` | `15` | Claimed but unacknowledged mail is offered again after this long, 10–120. |
| `chief_hold_minutes` | `10` | The turn-end hook holds the chief at most once per this window, 5–60. |
| `chief_auto_drain` | `true` | Let the chief queue unattended audit fixes and start them when a worker slot frees. |
| `chief_auto_compact_threshold` | `30` | Context-use percent at which the chief compacts itself, 10–90; `0` off. |
| `chief_job_failures` | `true` | Leave the chief a mailbox event when a Job run fails or its script is missing, while a chief is live. |
| `chief_job_self_heal`, `chief_weekly_proposals` | `false` | Opt-in switches served to the chief on `GET /api/board/chief/settings`. The app starts nothing for them, and nothing in this repo's `copilot/` reads them. |
| `chief_plan_file`, `chief_managed_file`, `chief_inbox_dir` | `chief-plan.json`, `chief-managed.json`, `chief-inbox` under `~/.copilot/app-launcher-lite-state/` | The chief's plan, the register of sessions it started and its mailbox folder. |

### Speech to text

Settings > Connections > **Speech to text**; the request and recording behaviour is in the [README](../README.md#speech-to-text).

| Key | Default | Effect |
| --- | --- | --- |
| `speech_enabled` | `false` | Shows the record button in every message box. |
| `speech_base_url` | `""` | Base `http(s)` URL of an OpenAI-shaped speech server; audio goes to `<base>/audio/transcriptions`. |
| `speech_model` | `""` | Model name sent with each upload; omitted when empty. |
| `speech_api_key` | `""` | Bearer key sent when set. A secret, never returned by the API. |
| `speech_keep_recordings_hours` | `24` | Hours each dictation take stays on disk, 0–720; `0` keeps none. |

### Auth and terminal

| Key | Default | Effect |
| --- | --- | --- |
| `auth_token` | `""` | Bearer token required from non-loopback callers. Empty turns the gate off. Generate with `scripts/gen_token.py`. |
| `auth_password` | `""` | Legacy plaintext sign-in password, checked only until `python -m src.password set` stores a salted hash in `config/app_password.json`; the hash then wins and this can be deleted. |
| `tailnet_allowlist` | `[]` | Extra IPs and CIDRs allowed on terminal routes beyond loopback and the Tailscale CGNAT range `100.64.0.0/10` — how a non-Tailscale VPN enables the terminal. |
| `terminal_allow_private_lan` | `false` | Trust the private network (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, link-local, IPv6 unique-local) on terminal routes, for a PC with no Tailscale whose phone joins over a trusted LAN. The bearer token and passkey still apply. Prefer `tailnet_allowlist` with one subnet when the LAN is shared. Settings › Terminal has the switch (**Allow private network without Tailscale**, with a warning before it turns on); it applies at once, with no restart, and can be changed only on the PC itself or over Tailscale. |
| `screen_allow_private_lan` | `false` | Also trust the private network for the screen view, which shows the whole desktop. Needs `terminal_allow_private_lan` on: with it off the screen view stays Tailscale-only, and turning the first switch off turns this one off. Settings › Terminal: **Also allow screen view without Tailscale**. |
| `webauthn_rp_id` / `webauthn_origin` | `""` | Passkey relying party: the bare hostname the phone browses to, and the full https origin. Both empty turns the passkey gate off. |
| `webauthn_rp_name` | `Launcher` | Display name in the passkey prompt. |
| `claude_show_local_window` | `true` | Allow the dedicated PC window at all, for a launch and for a row's **Open in new window**. (The key name predates the Copilot-only fork.) |
| `open_sessions_in_window` | `false` | Open every new launch's terminal in the dedicated PC window. Off, it renders in the page. Needs `claude_show_local_window`. |
| `prevent_lock` | `false` | Settings › This PC › Prevent lock. On, the canonical webapp moves the pointer 1 px every 15 s (up, then down) and holds a per-process keep-awake request, so the PC does not lock for being idle; it skips a beat after real input. Off, nothing runs and nothing is held. Saved through `POST /api/config`; see [`docs/prevent-lock.md`](prevent-lock.md). |
| `mirror_browser` | `chrome` | The browser a session's PC window opens in: `chrome` or `edge` open a dedicated app window in that browser, found through App Paths and then Program Files, and fall back to the other one and then the Windows default when it is not installed; `system` opens the page in the Windows default browser. The window runs on its own browser profile folder under the app's runtime data, so a browser already open does not swallow the window's flags. Edited from Settings › Sessions › Window browser. |
| `open_sessions_in_chat` | `true` | A session never opened before starts in Chat. Off, it starts in Terminal. |
| `desktop_browser` | `chrome` | Where a link tapped in Chat opens when the page runs on the PC itself: `chrome` (falls back to the Windows default) or `system`. |
| `terminal_history_lines` | `10000` | Scrollback the session-host replays on (re)connect, 200–50000. |
| `terminal_timing_log` | `false` | Diagnostics: logs each terminal keystroke's stage timings and any event-loop stall over 100 ms to `webapp/slow-requests.log`. Read live. |
| `request_budgets` | `{}` | Per-request-class time budgets, `{class: [soft_s, hard_s]}`, laid over the defaults in [diagnostics](diagnostics.md#request-timings-and-budgets). Read live. |
| `transcript_page_cache_size` | `32` | Parsed transcript pages kept for files that have not changed since, so reopening a chat does not parse it again; `0` turns the cache off. |
| `transcript_tail_cache_mb` | `32` | MB of session transcript tails kept for files that have not changed, so the session list and the Board open each transcript once per change instead of once per reader per poll; `0` turns the cache off. |
| `transcript_resolve_miss_ttl_s` | `10.0` | Seconds a Copilot session whose event log was not found answers "not found" before the session-state folder is scanned again. A new session folder ends the wait early. |
| `poll_board_s`, `poll_sessions_s`, `poll_transcript_s`, `poll_pause_hidden` | `5`, `5`, `3`, `true` | How often the Board, the session list and an open chat refresh, in seconds, and whether a hidden tab stops every page poll. Settings > Performance; applied live. |
| `pool_hot_poll`, `pool_session_reads`, `pool_input`, `pool_sends`, `pool_git_status` | `8`, `12`, `4`, `0`, `2` | Thread limits for the Board and session builds, transcript reads, message sends, every other route without its own pool (`0` picks from the CPU count), and the git scan (one thread runs one git process at a time, so this is also the most git processes alive during a scan). Settings > Performance; applied live. |
| `git_status_ttl_s` | `10` | Seconds a git scan is served as it is before the next poll refreshes it behind the response. |
| `request_budget_scale` | `1.0` | Multiplier on every request budget, so a slow PC logs fewer breaches. |
| `large_upload_max_mb` | `2048` | Limit for the composer's **Attach large file (not read)**, 1–10240 MB; the ordinary attach keeps its fixed 12 MB. |
| `outbox_scratch_roots` | `["%TEMP%/claude"]` | Folders, besides a session's own, an agent may send a file to Chat from. `%VAR%` and `~` expand; Settings › Folders validates each on save. |
| `documents` | `[]` | Files the reading sheet may show, each `{id, title, repo, path}` ([Documents](documents.md)). |
| `session_retention_days` | `365` | A daily sweep deletes per-session logs and transcripts older than this; live sessions are never touched. `0` keeps everything. Hand-edit only. |

The poll, pool and cache rows above, from `transcript_page_cache_size` through `request_budget_scale`, are Settings > Performance limits, each with a hard range and a Light machine preset value: [performance](performance.md).

### Jobs notifications and coverage

| Key | Default | Effect |
| --- | --- | --- |
| `pushover_api_token` / `pushover_user_key` | `""` | Pushover credentials for the failure channel; both must be set. |
| `notify_on_failure` | `false` | Master switch: one push per failed run. |
| `notify_failure_streak` | `0` | Also push when N consecutive failures stack up (0 = off). |
| `jobs_coverage_interval_minutes` | `60` | Missed-fire coverage scan interval (0 = poll-only). |

### Secrets and tokens

| Key | Default | Effect |
| --- | --- | --- |
| `secrets` | `{}` | Named secret values referenced from `jobs.json` as `$secret:<key>` (webhook secrets, job env). |
| `api_tokens` | `[]` | Job-scoped bearer-token records minted from Settings — salted hashes only, never hand-edit. A job-scoped token can only call `POST /api/jobs/<id>/run` for its allowed jobs. |
| `telegram_bot_token` / `telegram_chat_id` | `""` | Used only by the login lock-out alert; either empty sends none. |

The dataclass in `src/webapp_config.py` may still carry fields from the full app that the lite UI never reads; the tables above are the contract.

## `config/apps.json`

The Apps-tab registry, bat-based launchers only. Each row:

```json
{ "id": "...", "name": "...", "kind": "streamlit | webapp | tunnel | tray",
  "bat_path": "...", "added_at": "...", "autostart": false }
```

Coding projects are not stored here: the tab discovers them live from `projects_dir`. Scan flow: **Scan for new apps** in Settings calls `/api/apps/scan`, which returns a diff for a checklist dialog; the selections are persisted through `/api/apps/save`.

## `config/jobs.json`

`{"jobs": [...]}`, empty by default. The full job schema (kinds, schedules, params, webhooks, env and secrets, alerting) is in [Jobs tab](jobs-tab.md).

## Registered trays (Apps tab)

A `tray` row is a sister project's `tray.bat` that references the shared `tray_lifecycle.ps1` helper; the scan flow recognises it. When this tray starts, right after it tries to bring its own webapp up, it walks every autostart-enabled tray one at a time, in the registry's alphabetical order, waiting for each to report ready (its `/healthz` and `/api/version` answering on its `.fleet.toml`'s declared `port`, over HTTPS or HTTP, serving the `git_sha` its repo is at; or a fixed delay when no port is declared) before starting the next, so several Python processes never boot at once. The walk does not depend on the webapp coming up (a missed ready-wait writes its reason to `webapp/registered_trays.log` and the sister trays launch anyway), and one tray failing does not block the rest. A port that accepts connections but does not answer is logged as `bound_not_answering` and retried, never counted ready or launched over; one answering on another `git_sha` than its repo's `HEAD` is logged as `stale` and left for that repo's `tray.bat --restart`. A tray launched this way is started, not managed: `tray.bat --restart` never touches another repo's tray.
