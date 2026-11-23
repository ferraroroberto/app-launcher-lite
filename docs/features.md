# What it does, in one screen

The tab-by-tab feature index. Setup, running and the gate stay in the [README](../README.md); each tab with a deeper reference links to it.

The web UI is one phone-first PWA with five work tabs — **Coding · Apps · Jobs · Team OS · Board** — plus **Settings** behind the gear in every page header. Every tab opens with the same page header: the tab's name, one context line, the theme toggle and the gear. On a wide desktop window (1100 px and up, mouse pointer) the tabs move into a left rail, the Coding tab becomes list-and-detail (a session's view opens in a pane beside the list), Jobs docks a job's sheet beside the list, and Apps becomes two columns.

The coding agent is **GitHub Copilot CLI** (`copilot`) and the forge is **GitLab via `glab`**; nothing else is offered. A GitLab item below is implemented against GitLab's documented REST API and is **not yet verified live** (see [GitLab](gitlab.md)).

## Coding

- **Projects** — every directory directly under `projects_dir` is a tile (no marker file; the list is the live directory listing, minus `projects_ignore` globs). A filter field narrows by name, per-project ★ favorites sort to the top, and a ★ toggle filters to them.
- **Launch** — one tap starts a Copilot session in the folder. **Full control** (the default) runs it in a launcher-owned ConPTY streamed live to the phone ([Interactive terminal](interactive-terminal.md)); the **Detached** switch opens it in its own console window on the PC instead (listed and killable, no phone terminal). **Resume** hands off to Copilot's own `--resume` session picker. The launch button is disabled until `copilot` resolves on `PATH` ([Installing the CLIs](installing-agent-clis.md)).
- **Launch flags** — model, effort, context, autopilot and skip-permissions come from the persisted Copilot settings; explicit model ids are tenant-gated and `copilot_models` is edited per install ([Config](config.md#copilot-launch-settings)).
- **Git state** — always on: the tab reads `git-status` when it opens and every ~45 s while it is the visible tab; no other tab triggers a scan. The server scans at most two repos at once, repos with a live lane first, and labels each answer `fresh` or `stale` (served past its cache window while a refresh runs; the footer adds "refreshing"). A row shows `on <branch>` when off its default branch, an amber **uncommitted** chip for a dirty tree, and **behind N** / **diverged** / **ahead N** / **remote unknown** against `origin`. The counts come from local refs; `git fetch` runs only on the refresh button, inside Pull and Sync all, and on a 15-minute timer while a page polls. Launching on a behind repo is refused until you **Pull and launch** or choose **Launch anyway**. **Pull** and **Sync all** fast-forward only a clean repo on its default branch that is only behind; nothing is ever stashed, reset, rebased or merged.
- **⋮ project menu** — **Show changes** (a read-only phone view of the working tree against `HEAD`: files grouped Changes / Untracked, status badges, inline unified diffs capped at 200 KB), **Open in VS Code** (creates a sibling `<name>.code-workspace` if absent; greyed when `code` is not on `PATH`), **Open folder** (Explorer on the PC), **Copilot launch** and the project's **GitLab issues** link. VS Code and Explorer act on the PC the launcher runs on, not on the phone.
- **Sessions card** — running sessions on a two-line row: the agent icon with an alive badge, the title and `folder · model · status · age · up uptime`. Only an exception gets a chip (`needs you`, `stalled`). The row's ⋮ menu holds Terminal, Chat, Rename / link and Stop and kill; tapping the row opens the session view ([Session view](session-view.md)).
- **Session view** — one full-screen overlay with a shared bar and two modes, **Terminal** and **Chat** (Chat reads Copilot's `events.jsonl`). Switching never reconnects anything.
- **History** — the clock button in the Projects card opens a sheet of what the projects shipped: lines added and removed (local `git log`), merge requests merged and issues opened and closed (GitLab), by day, week or month, with a per-project table. It answers from a cache at once and re-reads only on its Refresh button; a project with no GitLab remote reads `skipped`, never zero ([History](history.md)).
- **PC load** — the page header carries a CPU reading that opens a sheet with total CPU, memory and the busiest process groups, plus the PC's private IP with a Copy button; a shared sampler reads it every 5 s and only while a page asks.

## Apps

- **Registry** — `config/apps.json`, one row per launcher `.bat`, each with a `kind`: `streamlit`, `webapp`, `tunnel` or `tray` ([Config](config.md#configappsjson)).
- **Launch and Running** — one tap spawns the bat detached; the **Running** panel lists live instances with their listening ports and a per-instance stop. With `tailnet_host` set, each running app gets a remote **Open** URL (`<scheme>://<tailnet_host>:<port>/`, scheme auto-detected by a TLS probe).
- **Scan** — **Scan for new apps** (Settings) walks `apps_scan_root` for candidate bats and shows a checklist diff before saving; a bat that names no app signature is read one level further, through the `.py` files it runs inside its own folder (read, never run), and a bat still unrecognised is logged at info level by its path under the scan root; a **Mini apps** group lists small helper bats from `mini_apps_root`.
- **Registered trays** — `tray` rows with autostart on are started one at a time, waiting for each to report ready, whenever this tray boots; a sister tray is started, never managed. One whose `.fleet.toml` port did not answer is retried after 30 s, 60 s and 120 s while this tray runs, each retry checking first that the port is still silent and nothing is running it, so a slow tray is never started twice; `webapp/registered_trays.log` records each spawn, spawn failure, timeout, retry and final failure.
- **Edit mode** reveals rename and remove.

## Jobs

Remote-fireable scripts backed by `config/jobs.json`: define a job once (script, typed params, optional schedule) and every trigger — phone tap, scoped-token URL, Task Scheduler slot, webhook, chain — runs through one executor that writes a uniform run record. Schedules materialise under `\AppLauncherLite\` in Windows Task Scheduler. Failure alerts go through Pushover, missed-fire coverage and a stuck-run watchdog guard unattended runs, and run history is searchable. Full reference: [Jobs tab](jobs-tab.md).

## Team OS

Surfaces the skills of a sibling `team-os` checkout (`team_os_dir`, skills under `<team_os_dir>/.claude/skills`). One tap launches a Copilot session whose working directory is the team-os repo and which opens with `/<skill>`, with the same full-control / Detached / Resume options as Coding and a per-launch model. A **weekly recap** tile shows the recap's freshness and launches `/weekly-recap`. The private-content browser (context, memory, examples, conversations) is private-network and passkey gated and path-jailed to `team_os_dir`.

## Board

A kanban over four computed columns: **Backlog** (open GitLab issues across `gitlab_group`), **Bot's turn** (live sessions working or idle), **Your turn** (sessions awaiting input — the number that matters) and **Done** (issues closed today). Session presence comes from the `:8466` session-host and semantic status from `sessions-state.json`, written by the Copilot hooks in `copilot/`. GitLab is read through `glab` on demand only (the refresh button or a stale tab activation), never on the 5 s poll. A session card opens a drill-down drawer (last exchange plus a reply straight into the live PTY); a Backlog card whose project exists locally carries **Start** and **YOLO** buttons that launch `/issue-start N` and `/issue-yolo N`. Full reference: [Board](board.md).

## Settings

- **Folders** — projects dir and ignore globs, apps scan root, mini-apps root, team-os dir, Copilot dir (`fleet_config_dir`), documents the reading sheet may show.
- **Board / GitLab** — group and host.
- **Copilot** — the model list, the saved model, effort, context, autopilot and skip-permissions.
- **Sessions** — open sessions in Chat, open in a new PC window, mirror window, terminal scrollback lines.
- **Access** — bearer token, sign-in password, passkey device list (enrolment starts from the PC tray menu, not the phone), job-scoped API tokens (safe to bake into a Stream Deck URL; the raw token shows once).
- **Appearance and startup** — a Small / Default / Large text size, a **Full screen on Edge** switch (Edge only) for a phone browser that will not install Lite (see [android.md](android.md#a-managed-phone-full-screen-without-installing)), an **Install as app** row that says whether the browser can and why not, the light/dark toggle in the header, and **Start App Launcher Lite at log on** (a Startup-folder wrapper, no admin).
- **Documents** — the reading sheet renders listed Markdown files with Mermaid diagrams ([Documents](documents.md)).

## The chief

The chief works: one long-lived Copilot session that answers in the Board chat, keeps a plan and a journal, raises questions for the owner, dispatches lanes, opens plain sessions and stops them. Its helpers and skill live in this repo's `copilot/` folder, where it runs. The Board shows its card, plan and answer sheet; [The chief](board.md#the-chief) describes each part. The doorbell rings an idle Copilot chief (composer keys verified on CLI 1.0.114); a bare `/compact` runs idle and queued at turn end (verified on 1.0.114; one with a focus argument is `unknown`), and a lane needs `git` to reach GitLab without a prompt. See the README's Status section.
