# Layout

```
app-launcher-lite/
├── tray.bat                   # the day-to-day entrypoint: starts or restarts the tray
├── setup.bat                  # one-shot fresh-clone installer
│
├── app/
│   ├── cli/                   # argparse dispatcher (commands/): tray | webapp | scan | session-host | run-job
│   ├── tray/                  # pystray icon: owns the webapp + cloudflared + session-host; watchdog, self-heal, Tailscale helper
│   └── webapp/
│       ├── server.py          # FastAPI app: route registration, auth gating, WS proxy
│       ├── restart_targets.py # the build readout's restart sheet: its two targets (app, session-host + live session count) and the passkey guard
│       ├── manager.py         # adopt-or-spawn webapp lifecycle
│       ├── serve.py           # the one launch entry: hypercorn (HTTP/2 over TLS) on the selector loop, uvicorn as the fallback
│       ├── shell_html.py      # splices static/views/*.html into index.html where its include markers sit
│       ├── static_trim.py     # strips comments from the JS modules the static mount serves
│       ├── module_preload.py  # expands index.html's modulepreload markers into one link per module of an entry's import graph
│       ├── demand_cache.py    # a slow read kept in memory and rebuilt in the background while it is polled
│       ├── hot_poll.py        # bounded builds for the polls that read live transcripts
│       ├── perf_apply.py      # resizes the thread pools to the Settings > Performance limits after a save and at boot
│       ├── session_host/      # loopback PTY host on :8466: owns every agent ConPTY; a second HTTP
│       │                      #   surface with its own server.py, so the webapp mounts nothing from it
│       ├── routers/           # split API routers: config, sessions, session_transcript, apps, jobs*, board*,
│       │                      #   team_os, documents, tokens, auth, webauthn, desktop_screen, ...
│       └── static/            # SPA shell + PWA manifest + icons + vendored components
│           ├── views/         # each view's markup (panes, dialogs, overlays), spliced into index.html when served
│           ├── apps/ board/ jobs/ team-os/ settings/    # one folder of modules per tab, beside the tab's entry .js
│           ├── chat/ terminal/ changes/ links/ screen/ shared/   # the session overlay's panes and the modules several views import
│           ├── styles/        # the app's own CSS: base.css (tokens, tabs, shell) + one file per view, linked in cascade order
│           └── _vendored/     # components vendored byte-for-byte from project-scaffolding
│
├── src/                        # logic layer (no UI imports); flat modules first, then one subpackage per domain
│   ├── app_config.py           # log level, webapp embed section, the webapp port
│   ├── webapp_config.py        # host / scan paths / agent settings / secrets / terminal knobs
│   ├── webapp_settings/        # the defaults and value sets every webapp_config field validates against; performance.py is the speed-limit table and presets
│   ├── self_restart.py         # vendored: /api/restart routes, the detached restart runner and its run record
│   ├── agents.py               # coding-agent registry (Copilot CLI only) + PATH detection
│   ├── registry.py             # apps registry (load / save / scan)
│   ├── scanner.py              # bat classifier + project-dir + team-os skill discovery
│   ├── session_host.py         # PtySession + SessionManager (ConPTY via pywinpty)
│   ├── session_host_*.py       # PTY-protocol scanning, composer state, input delivery, declared-path parsing
│   ├── session_hosting/        # session kinds + stop modes, child env, detached console sessions, deferred submit
│   ├── vt_snapshot.py          # headless VT mirror per fullscreen session (reconnect snapshot)
│   ├── audit.py                # terminal audit + per-session logs; session_retention.py is its daily sweep
│   ├── diagnostics.py          # log ring buffer + port-owner introspection
│   ├── request_timing.py       # per-request queue/worker time, the budget table, in-flight peaks
│   ├── health_snapshot.py      # the read-only `scripts/launcher.py health` report
│   ├── performance_health.py   # the read-only health line under Settings > Performance
│   ├── monitoring/             # background samplers behind the read-only readings: the Apps tab port listeners and the hub's machine health (listeners_sampler.py)
│   ├── static_versioning.py    # content-hash query strings for asset cache-busting
│   ├── stylesheet_bundle.py    # the linked stylesheets concatenated into the one /static/bundle.css
│   ├── static_bundles/         # js_bundle.py: main.js's module graph bundled at start by a pinned esbuild into /static/js-bundle/
│   ├── subprocess_flags.py     # the NO_WINDOW spawn-flag convention (single source)
│   ├── git_utils.py            # shared `git` subprocess runner; every git spawn goes through it
│   ├── git_upstream/           # ahead/behind against origin and the guarded fast-forward
│   ├── documents/              # Settings > Documents: allowlist validation, path jail, read
│   ├── auth/                   # job-scoped API tokens, the passkey (WebAuthn) gate, the self-signed TLS certificate
│   ├── middleware/             # the webapp's auth gate: trust (loopback / tailnet), credentials, route guard table, bearer
│   ├── clients/                # loopback HTTP clients (shared base, session-host) and the GitLab client (gitlab.py, via glab)
│   ├── launching/              # agent launch: Copilot flags (flags.py), spawn helpers (launcher.py), the tray-restart check, the PowerShell script launcher (ps_script.py), the installer behind setup.bat (installer.py)
│   ├── board/                  # Board backend: column assembly, session-state join, drawer exchange, proof reports, owner checks, the viewed Board's single-flight forge refresh (view_refresh.py)
│   ├── chief/                  # the chief's plan, journal, answers and dispatch briefs
│   ├── session_view/           # a live session's views: transcript pages, changed files, the Chat outbox, plan and resume pickers
│   ├── transcript_flavors/     # the Copilot transcript-line grammar (copilot.py, apply_patch.py) and its turn-end reader (copilot_turn.py)
│   ├── desktop/                # the Screen view's PC side: capture, input, cursor, clipboard, the Prevent lock nudge (prevent_lock.py)
│   ├── notify/                 # vendored provider package behind notifications.py (failure alerts)
│   ├── jobs/, jobs_kinds/      # Jobs backend: config, scheduling, trigger, queue, reap, history, stats, coverage, webhooks,
│   │                           #   secrets; per-kind executors (batch / http_check / inline_shell / powershell / python / shell_wsl)
│   └── ...                     # small single-purpose modules (password, login lockout, build info, boot autostart, runtime files, ...)
│
├── scripts/                    # launcher.py (thin entry point: sys.path shim -> app/cli/main), webapp.bat (webapp without the tray), webapp_tunnel_named.bat (uvicorn + cloudflared), operator tools and the vendored ratchets
│   ├── verify-before-ship.ps1  # the pre-ship gate: disposable webapp + session-host, no tray needed
│   ├── run-e2e.ps1             # dev-loop e2e runner against the live tray
│   ├── install.py              # setup.bat's work: venv, requirements, configs from samples (never overwriting), checklist
│   ├── restart-session-host.ps1 # confirmation-gated :8466 restart (see CLAUDE.md, session-host)
│   ├── tray_lifecycle.ps1      # the tray's start / restart / reclaim logic, used by tray.bat
│   ├── classify_e2e.py         # diff -> browser-slice routing
│   ├── shot_baseline.py        # the screenshot baseline stage
│   ├── *_ratchet.py            # one ratchet per concern, each wrapped by the gate (structure, docs, doc refs, dead code, ...)
│   ├── gen_icons.py            # icon generation (the icons are committed)
│   ├── gen_tailscale_cert.py   # Tailscale certificate provisioning + --check auto-renew
│   ├── gen_token.py            # bearer token rotate / clear
│   ├── webapp_port.py          # resolves the webapp port for the .bat files
│   ├── run_named_tunnel.py     # the Cloudflare named-tunnel runner
│   ├── probe_endpoint_timing.py # GET-only endpoint timing at a phone's poll spacing
│   ├── bench_requests.py       # p50/p95 benchmark of the slow-feeling requests + max in-flight
│
├── copilot/                    # the Copilot side: hooks, chief helpers, lane skills, their unittest suite (see below)
│   ├── hooks/                  # session-state, chief-inbox and chief-guard hooks and their templates
│   ├── skills/                 # issue-*, e2e, quick and the shared skills/_lib/ (chief_ops.py and friends)
│   ├── .github/skills/chief/   # the chief skill
│   └── tests/                  # its stdlib unittest suite, run by tests/unit/copilot/
│
├── tests/                      # pytest suite; shared helpers and the root conftest sit here, every test module in a folder below
│   ├── unit/                   # unit tests by domain (board, chief, clients, core, desktop, jobs, launching, pty, session_host, ...)
│   ├── api/                    # API tests by router (board, chief, config, documents, jobs, screen, webapp, nonblocking)
│   ├── e2e/                    # Playwright browser suite: one folder per tab, fixtures/ for the conftest's parts, shots/ for the baseline
│   ├── js/                     # plain-Node tests for browser-side logic a desktop engine cannot judge
│   └── fixtures/               # recorded transcripts and sample files the tests read
│
├── docs/                       # durable reference, one topic per file; architecture.mmd is the structural diagram
│
├── config/                     # *.sample.json committed, real files gitignored (config, webapp_config, apps, jobs)
├── assets/                     # tray icon and Stream Deck button image, committed
│
└── webapp/                     # runtime state, gitignored except samples
    ├── certificates/           # cert.pem / key.pem from gen_tailscale_cert
    ├── cloudflared.yml         # your filled-in copy of the sample
    └── auth.log                # failed-login audit
```

## Stylesheets

`app/webapp/static/styles/` holds the app's own rules, cut into files of at most 1,000 lines. `index.html` links them one by one, and that link order is the cascade: a rule in a later file wins a tie against an earlier one exactly as it did when they were a single sheet.

- **Tokens, tabs, cards and the page header** live in `base.css`; the bottom-tab nav's token and overlay-hide rules are there, so they stay first.
- **A view's rules go in that view's file**: `settings.css`, `jobs.css`, `board.css`, `code.css`, `apps.css`, `team-os.css`, `terminal.css`, and `chat.css` / `chat-cards.css` for the transcript pane. Rules several views share go in `shared.css` (toasts, dialogs, row menus) or `lists.css` (collapsible cards, option rows, card lists).
- **A rule that overrides another stays after it.** The `wide.css` desktop media blocks restyle the views above, so they sit below those views' files; a new file goes where its rules must win, not alphabetically.
- **A file that would pass 1,000 lines splits at a section comment**, never inside a block, and its link goes right after the file it was cut from.
- **The browser fetches one stylesheet.** The source files stay separate, but at server start `src/stylesheet_bundle.py` concatenates every sheet `index.html` links, vendored ones included, in link order into `/static/bundle.css`, and the served page links only that: a phone queues behind a handful of connections per origin, so dozens of requests cost far more than one. A new file needs only its `<link>` in `index.html`; the bundled sheets must use no `@import`, `@charset` or relative `url()`, which `tests/unit/static_ui/test_stylesheet_bundle.py` pins. `xterm.css` loads lazily and goes first in the head, ahead of the bundle, so the app's terminal rules still override it.
- **Cache-busting is automatic**: `src/static_versioning.py` stamps the bundle URL with a content hash, which also covers `index.html`'s link order, so a reorder busts the immutable cache.
- **Tests read the sheets through `tests/_css.py`**, which concatenates them in `index.html`'s link order.

## Page shell

`app/webapp/static/index.html` is the shell: the head, the icon sprite, the bottom-tab nav, the toast and login mount points and the scripts. Everything a view draws lives in `app/webapp/static/views/<name>.html`, named for its view (`board-pane.html`, `jobs-dialogs.html`, `overlays.html`), and `index.html` pulls each in with a marker line on its own:

```html
<!-- @include views/board-pane.html -->
```

- **The server splices, the browser sees one page.** `app/webapp/shell_html.py` replaces each marker with its fragment's text, so `GET /` returns the same bytes a single-file `index.html` would, with no client-side render and no first-paint gap. A module's import-time `getElementById` therefore still finds its element.
- **Marker order is document order.** A dialog moves only by moving its marker or its text, never by renaming a fragment; a fragment holds contiguous markup, indented as it sits in the page.
- **A new view's markup goes in its own fragment**, with a marker where it belongs in `index.html`. The shell stays under 1,000 lines and holds no pane or dialog; `tests/unit/shell/test_shell_html.py` pins both.
- **Fragments do not nest and must sit under `views/`.** The page cache keys on `index.html` and every fragment, so an edit on disk shows on the next load without a restart.
- **Tests that read the page as source go through `assemble_shell`**, not `read_text`, so they see the fragments.

## Structure ratchet

`scripts/structure_ratchet.py` (vendored byte-for-byte from project-scaffolding, recorded in `.fleet.toml` `[vendored]`) runs in the pre-ship gate. It fails a new file over 1,000 lines, a new function over 100 lines, a directory past 40 modules, a `src` file that imports `app`, and a root folder the declared shape does not list. A finding already in `.structure-ratchet-baseline.json` may not grow past its recorded size. Shrinking one means lowering the baseline with `--update-baseline`; raising or adding an entry needs `--allow-additions`, which shows in review.

- **Where new code goes:** a view's browser code in `app/webapp/static/`, its API in `app/webapp/routers/`, logic with no UI import in `src/`, tests under `tests/unit/<domain>/` (logic), `tests/api/<router>/` (routers) or `tests/e2e/<tab>/` (browser), never loose in `tests/` or `tests/e2e/`, which hold only shared helpers and the conftests. A new module in a full directory goes in a subpackage (`src/notify/`, `src/jobs_kinds/` and `src/transcript_flavors/` are the pattern), never loose in the directory.
- **Second HTTP surface:** the session-host is `app/webapp/session_host/`, the shape's home for a separately bound listener: it has its own `server.py` and entry point (`scripts/launcher.py session-host`), and the webapp only reaches it over loopback HTTP through `src/clients/session.py`.
- **Declared shape:** `webapp-tray`, with `webapp/` and `copilot/` as extra roots. `webapp/` is the tray's runtime-state folder, and almost all of it is gitignored. The preset itself lists `assets/` and `config/`.
- **`copilot/` is the agent side, not the app.** It holds what the Copilot CLI runs (hooks, skills, the chief's helpers), so one clone carries both the launcher and its chief. It is Lite's own code, edited here like the rest of the repo. It came from fleet-config-lite and keeps that repo's layout, so every relative path and `parents[N]` lookup in it works unchanged. `copilot/install.py` (with `install.ps1` a thin wrapper that forwards to it) installs and removes the hooks and skills under `~/.copilot` and migrates a machine off the old install. `tests/unit/copilot/test_copilot_tree.py` runs its suite in a child interpreter. The launcher reaches it only by path (`fleet_config_dir`, default `copilot/`): the chief runs there, and the Board loads its helpers from there. Nothing in `src/` or `app/` imports from it. The ratchets scan it like any other code, so its existing findings are baselined until a change fixes them.
- **Flat directories:** `src/` is under the limit because its domains sit in subpackages; a new module goes in its domain's package, not loose in `src/`. `app/webapp/static/` is under the limit because its views' modules sit in the subfolders above; a new module goes in its view's folder, not loose beside `main.js`.
- **The e2e conftest is a registrar.** `tests/e2e/conftest.py` holds the pytest hooks, the `_autoboot_server` session fixture and the fixtures it re-exports; the code behind them is in `tests/e2e/fixtures/`: `env.py` (paths, env-var names, run state), `waits.py` (waiting helpers and timeout budgets), `ui.py` (page-driving helpers), `processes.py` (spawning and leak scans), `autoboot.py` (the disposable servers' boot and isolation checks), `launch_target.py`, `sessions.py` and `pages.py`.
- **Oversized files and functions:** every one over the limits is in the baseline at its current size. Splitting them is separate work, and a split lowers its entry.
