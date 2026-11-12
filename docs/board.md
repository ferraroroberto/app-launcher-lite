# Board tab — reference

The launcher's fifth surface (four single-purpose columns over **GitLab/`glab`** data; there is no free-text dispatch bar — the Board launches work only via the Backlog cards' ▶/⚡ one-tap buttons). It is a **read-only fleet kanban** that answers one question — *"what needs me now, across everything"* — over four **computed** columns, each holding one kind of card. A card moves because reality changed; there is deliberately no drag-and-drop. It renders three independently-degrading live sources (the session-host's session list plus fleet-config-lite's sessions-state and active-issues files) plus a cached GitLab view (`glab`-fetched issues).

On the phone the four columns are a swipeable one-column-per-screen carousel with a count strip on top; desktop shows all four side by side, each column header carrying its own `(N)` item count. The **Your turn** count is the number that matters — its strip button highlights when nonzero.

## The four columns and their data sources

Column assembly is pure logic in `src/board.py::build_board()`. Each column holds one kind of card:

| Column | What populates it |
| --- | --- |
| **Backlog** | Open GitLab issues across every project of the configured group (`gitlab_group`; subgroup projects included). |
| **Bot's turn** | Live session cards whose status is **not** in the needs-you family — i.e. `working`, `unknown`, or `idle`. (A session with nothing pending is still the bot holding a workspace, so it is shown here — dimmed client-side, not hidden.) |
| **Your turn** | Session cards whose status is `awaiting-input` (the hook's raw `needs-you`, see "Card status" below) — a terminal-only column. |
| **Done** | Today's closed issues, since local midnight. |

(The old fifth **Other** column — open PRs + failed/stuck job runs — was dropped in Phase 5; failed jobs stay visible on the Jobs tab, and merge requests have no Board surface for now.)

**Backlog** cards render thin (repo · #N · title · age) and link out to GitLab. A Backlog card whose repo is present in the projects folder additionally carries **▶ Start / ⚡ YOLO** (see "One-tap issue start" below). If fleet-config-lite's issue workflows have an active marker for the same `<repo>#<number>`, the row gains the accent-soft tint and an explicit “in progress” label, and both launch buttons are truly disabled.

**Done is issues only:** a merged MR that closed an issue is already reflected by that issue showing closed, so there is no MR/issue pairing step — `src/gitlab_client.py::search_done_today()` is the closed-issues query. Group issues have no `closed_after` param, so it queries `state=closed&updated_after=<local midnight>` and filters client-side on `closed_at >= local midnight` (rows updated today but closed earlier, and rows with a null `closed_at`, are dropped).

The GitLab queries (`src/gitlab_client.py`) go through `glab api`: `groups/<group>/issues?state=opened&order_by=updated_at&sort=desc&per_page=100` for Backlog and the `state=closed&updated_after=<midnight>` variant for Done, each with a 20 s per-call timeout. Neither query paginates: a response of a full 100 rows sets the snapshot's `truncated` flag and the status line shows "GitLab list capped", since the group may hold more than the Board displays. The group is URL-encoded as one path segment (`grp/sub` → `grp%2Fsub`); a non-empty `gitlab_host` rides glab's `GITLAB_HOST` env var for self-hosted instances. Repo short names come from `references.full` — the last path segment before the `#`/`!` marker, so subgroup projects resolve correctly.

## Data endpoints

All Board routes live in `app/webapp/routers/board.py` (the shared launch helpers sit in `app/webapp/routers/board_spawn.py`).

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /api/board` | bearer-token | The four columns — the **5 s poll target**. Cheap only: runs the live session list and two small state-file reads concurrently, plus a pure in-memory read of the GitLab cache. **No `glab` subprocess ever runs on this path.** |
| `POST /api/board/gitlab/refresh` | bearer-token | Runs the two `glab` queries (open issues, closed-today issues) and replaces the cache. The **only** place `glab` is invoked. With an empty `gitlab_group` it never touches a subprocess — the snapshot just carries a "set gitlab_group in Settings" hint as its `error`. |
| `GET /api/board/sessions/{sid}/exchange` | private network + passkey | The last user↔assistant exchange from the agent-aware conversation-source hierarchy (drill-down drawer). |
| `POST /api/board/issues/start` | private network + passkey | One-tap `/issue-start` / `/issue-yolo <N>` on a Backlog card. |

The `GET /api/board` response is `{ generated_at, columns, gitlab: {fetched_at, error, truncated}, sessions_state: {available, stale, updated_at}, active_issues: {available, updated_at, count} }`. Each session card carries its raw session fields plus `project`, `status`, and `age_seconds`; each Backlog issue carries a boolean `in_progress`.

## The session-state file join

Launcher-owned session **presence and agent identity** come from the session-host list. That is the authoritative process-liveness source: when a PTY/detached process leaves the host list, its live Board card leaves on the next 5 s poll, regardless of whether an agent shutdown hook fired. Semantic status comes from a **sessions-state file** written by the fleet's agent hooks/extensions — path `sessions_state_file`, default `~/.copilot/hooks/state/sessions-state.json`. The Board only ever reads it.

**The join is agent-aware and exact-id-first.** A state writer may carry `agent` plus the launcher's own id as `launcher_session_id`; that exact id + agent pair wins. Rows with neither field default to the launcher's agent (`copilot`) and retain the normalized-cwd fallback (forward-slash separators, lowercase, trailing slash stripped), but only for a live session of that same agent — a row can never classify a session running a different agent.

Join mechanics (`_claim_walk` / `_match_state_row`):

- Live sessions are walked **newest-first** by `started_at`.
- Each session first claims an unmatched state row with the same `launcher_session_id` and `agent`.
- Without exact identity, the session can claim only an agent-compatible row whose normalized `cwd` is **equal-or-under** the session's project dir. A row carrying a different launcher id is never allowed to fall back by cwd.
- The cwd fallback also requires the candidate row's `updated_at` to be **at-or-after** the session's own `started_at` — a row genuinely written by this session can never predate the session's existence, so an older row can only be some other, unrelated conversation's leftover state in the same directory. Without this guard, a brand-new live session with no exact-id row yet (its own hook write hasn't landed) could claim an hours-old sibling's row by "most recently updated" and show that sibling's `shared_name` on its card.
- Two legacy candidate rows in one directory → the **most recently updated** (among those not older than the session itself) wins.
- Two live sessions in the same dir → the fresher one claims the row; the older renders `unknown`.
- A fresh unmatched row with **no** live session becomes a state-only external card only after clearing three checks, in order (`_external_row_liveness`) — two deterministic, one an inherently imperfect fallback:
  1. **Reaped-session check.** If the row carries a `launcher_session_id` that is *not* in the session-host's current live list, it's provably dead — the session-host is the sole authority on which PTYs it owns — regardless of how fresh the transcript looks. Suppressed unconditionally.
  2. **Shared-transcript check.** If the row's `transcript_path` is already backing a *live, matched* card, this row is a superseded leftover of that same session — re-keyed when a worker moved from one issue to the next, still pointing at the transcript the live session keeps writing — not a second process. Suppressed unconditionally. (This was the upstream "fleet-config ghost": a merged-and-deleted branch's session kept rendering a `working` card alongside its own live successor.)
  3. **Transcript-freshness fallback.** Only once neither of the above applies does "declared transcript file exists and was written within the last 5 minutes" remain the (imperfect) fallback evidence for a genuinely external row the launcher has no other way to verify — a process that writes once and exits inside the window is indistinguishable from one still running; this narrows that gap, it does not close it.

  Hook status is semantic evidence, not process-liveness evidence: a missing cloud/bridge transcript or a quiet `working`/`needs-you`/`idle` row is suppressed rather than trusted for the state file's 24 h retention window. The first suppression per state id leaves an info-level breadcrumb with the distinct reason (reaped session, claimed transcript, missing path, unavailable file, or quiet transcript) without repeating on every poll.

**Raw hook states** (`_KNOWN_STATUSES`): `working`, `needs-you`, `idle`. Anything else — including a missing row — renders `unknown`. See "Card status" below for how they reach a card.

Agent capability matrix:

| Agent | Presence | Semantic state in this release |
|---|---|---|
| GitHub Copilot CLI | Session host | Hook-written `working` / `needs-you` / `idle` rows when the hook writer is installed; `unknown` otherwise |

`unknown` is an explicit capability limit, not silence interpreted as certainty.

**Degradation is total and silent.** `read_sessions_state()` returns `{available, stale, updated_at, rows}` and never raises: an absent, unreadable, or corrupt file yields `available: False` with empty rows, and every session card falls back to `unknown` while the GitLab columns render regardless. `stale: True` when the newest row is older than `STATE_STALE_AFTER` (24 h) — i.e. the hooks have stopped writing.

## The active-issues lifecycle join

Fleet-config-lite's issue workflows publish `~/.copilot/hooks/state/active-issues.json` once an issue branch is ready and remove its row only after the MR merges. Rows are keyed by `<repo>#<number>` and carry `repo`, `number`, `branch`, and `started_at`. `GET /api/board` reads the file beside `sessions-state.json`, canonicalizes the key case-insensitively, and annotates every Backlog issue with `in_progress` before returning the columns.

`read_active_issues()` has the same never-break-Board contract as the session-state reader: a missing, unreadable, corrupt, or non-dict file yields `available: False` with no rows. Invalid records are ignored individually. A record older than `STATE_STALE_AFTER` (24 h) expires on read, matching the writer's prune horizon, so a crashed workflow that never reaches `/issue-finish` cannot permanently block Start/YOLO. This is deliberately a lightweight lifecycle marker, not a GitHub/branch reconciliation source.

## Shared session title, cross-tab

The state row also carries `name` / `name_source` — the agent's own live per-conversation title, copied in by the `session_state` hook writer where the agent exposes one. `merge_sessions()` copies those onto every card as `shared_name` / `shared_name_source` via the **same** exact-id/agent-aware `_claim_walk` described above, and `attach_shared_names()` runs the identical walk for `GET /api/coding/sessions` (the Coding tab's Running-sessions list) — so a live session resolves to the same state row, and therefore the same title, on both tabs. `name_source: "derived"` marks the generic `<project>-N` fallback (no real title assigned yet); anything else is a genuine title.

The frontend precedence lives in one place — `sessions.js`'s `sessionTitle()` — which `board.js` imports rather than re-deriving a title: a genuine `shared_name` wins outright, the OSC-parsed `live_title` is kept as a same-poll-cycle-faster supplement (it updates sub-second inside an open terminal, ahead of the next state-file poll), then `prompt_title`, then a *derived* `shared_name`, then the launch name. See [Naming sessions from the conversation](#interactive-terminal-from-the-phone) in the README for the full precedence rules.

## Card status

A card's `status` is the hook row's own status, mapped by `src/board_sessions.py::_card_status()`: `working` and `idle` pass through, the writer's `needs-you` becomes `awaiting-input` (the only status that routes to **Your turn**), and anything else — a missing row, an unrecognised value — is `unknown`. The raw `needs-you` string never reaches a card.

No transcript content is read. A transcript path is only used for the external-row liveness check above, which stats the file's mtime and is format-agnostic.

## The drill-down drawer and its PTY-write path

Tapping a live session card opens an **inline drill-down drawer** on the card (`board.js::buildDrawer`). It shows the last user↔assistant exchange plus a reply box, and while any drawer is open the **5 s poll pauses** (`fetchBoard()` self-gates on `state.boardExpanded`) so a re-render can never wipe a half-typed reply.

**Reading the exchange.** `GET /api/board/sessions/{sid}/exchange` first resolves the exact live session-host row; a missing session returns `reason: session_not_found`, so the endpoint never guesses by cwd. The source is the exact-id launcher capture: the last 512 KB of `webapp/sessions/<launcher-session-id>.transcript` is replayed through `pyte`; assistant blocks are selected by the terminal's leading-bullet colour contract, while the matching `<id>.log` supplies the last submitted input. Raw ANSI and full-screen repaint bytes never reach the API. No agent transcript is parsed.

All reads happen only when the drawer opens — `GET /api/board`'s 5-second poll still performs no transcript parsing or LLM call. Display caps remain 6000 assistant characters and 1500 user characters. The response carries `source` (`launcher`) and a machine-readable unavailable `reason` (`session_not_found`, `no_exchange`, `capture_unparseable`); the drawer distinguishes a true `no_exchange` empty state from a sanitized source error. An unavailable result leaves an info-level log breadcrumb.

**The exchange pane's state.** The pane is one declared machine, `board-exchange` in `.fleet.toml`, owned by `app/webapp/static/board-exchange-state.js`. It starts `loading` and settles exactly once; `board.css` reads the state from `data-state`.

| State | Meaning | Reached from |
|---|---|---|
| `loading` | the drawer just opened and the fetch is in flight | (initial) |
| `ready` | the API returned an exchange | `loading` |
| `empty` | `available: false` with `reason: no_exchange` | `loading` |
| `error` | any other unavailable reason, or the fetch/render threw | `loading` |

`deriveExchangeState(body)` is the only code that maps the API answer to a state, and `setExchangeState(el, state)` is the only writer of the attribute: it refuses any move the table does not list, so a late response can never reopen or flip a settled pane. `tests/unit/board/test_board_exchange_state.py` drives the real module under Node (skipped when `node` is absent).

**Replying to the live PTY.** The reply box is offered only for `alive && kind === 'pty'` cards (a detached console or state-only card has no reachable stdin). The reply proxies through `POST /api/coding/sessions/{sid}/input` — body `{data, submit}`, one call to the session-host, which owns framing + settle-then-submit internally via `PtySession.submit_input()` (it ports the compose bar's `framePaste`/`sendSubmit`/`bulkSettle`). `data` may be blank when `submit` is true — a bare submit against whatever is already sitting in the composer, with no text write — the recovery path for a message stranded by the settle race, otherwise reachable only by tapping the phone's own compose Send by hand. On success the drawer closes (`boardExpanded = null`, poll resumes) and `board.js::moveCardToBotTurn()` optimistically relocates the card into *Bot's turn* client-side, immediately — before the server-side prompt-submit hook has had time to flip `sessions-state.json`. No extra re-poll fires right away (it would almost always still see the pre-hook state and revert the optimistic move); the regular 5 s poll reconciles with ground truth as always. A **⚡** in the drawer opens the full-control terminal.

**Delivery honesty.** `{"ok": true}` means the write (and, if requested, the submit) actually reached a live session — a dead/exited session reports 409 instead of a false 200, and the settle-then-submit sequence means `submit: true` in the response reflects that the CR was actually sent after the paste's ingest was given a chance to settle, not just that bytes were written. Bracketed-paste framing is applied only when the PTY's own output has announced DECSET 2004 is on (tracked off the raw output stream, never stripped from it) — a literal `\x1b[200~` sent blind to an agent that never asked for it is garbage, not a paste.

**One-tap issue start.** A Backlog card whose repo is in the projects folder carries **▶ Start / ⚡ YOLO** → `POST /api/board/issues/start`, body `{repo, number, mode, rows, cols}`. Both controls are disabled while the card's active-issue marker is fresh, preventing a redundant conflicting session. Otherwise the route is injection-safe by construction: `prompt = "/issue-<mode> <number>"` with mode allowlisted and number int-validated. Every launch uses the persisted Coding model (Phase 6 removed the per-launch model selector; 400 if the Copilot CLI isn't installed). It resolves the repo to a project dir (404 if not present locally) and spawns a streamed PTY session — the `/issue-*` skills inherit worktree isolation for free, claiming the repo and building in a sibling worktree when the primary checkout is busy. It opens the PC-mirror window by default — a desktop client, the phone, and a headless loopback API caller all get one; only a genuine in-page loopback browser explicitly opts out (`in_page: true`, the SPA's own signal) and stays on the in-page terminal.

**`?board=<sid>` deep-link** (`board.js::openBoardCard`): activates the Board tab, fetches the board, finds the column holding that `session_id`, opens its drawer, and scrolls the carousel to it. If the session is already gone, it toasts and leaves the board browsable rather than pausing the poll forever on a non-existent card.

## Refresh / cache contract

GitLab data is fetched **server-side via `glab`** into a lock-guarded module-level cache in `src/gitlab_client.py`. `snapshot()` is the pure in-memory read the 5 s poll hits for free; `refresh(group, host)` runs the two `glab` subprocesses and replaces the cache. On failure the previous data is kept and only `error` is set — a flaky `glab` degrades to a badge, not an empty board.

The cache is refreshed **only** by:

- the manual **↻** button, or
- **tab activation** when the cache is stale — never fetched, or `fetched_at` older than `GL_STALE_MS` (2 minutes).

It is **never** refreshed on the 5 s poll (which only reads the snapshot), and an **errored** cache is never auto-retried — that would hammer a broken `glab`, so ↻ stays manual. This mirrors the Coding tab's ⎇ git-status on-demand contract exactly.

## Security boundary

The Board splits along the launcher's usual line. The **read-only board** (`GET /api/board`, the GitLab refresh) is bearer-token gated and reachable over the Cloudflare tunnel — it exposes only issue / session metadata. The **terminal-grade surfaces** — the drill-down exchange (transcript text), the drawer reply (`/input`), and one-tap issue start — are **private-network-only + passkey-gated** (the tailnet CGNAT range or an allowlisted VPN subnet — see the README's "Remote access" section): refused over the public tunnel entirely, and on the trusted network they additionally require a valid WebAuthn terminal token, exactly like the live terminal. The client obtains the token via the shared `ensureTerminalToken()` path.

## Verification

The pre-ship gate (`C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -File scripts/verify-before-ship.ps1`) covers the Board via `tests/unit/board/` (column assembly, the cwd-join, card status, the API shape), `tests/unit/board/test_gitlab_client.py` (the `glab` client — real GitLab API shapes in fixtures, cache semantics, error mapping), `tests/unit/board/test_board_drilldown.py` (the exchange read + reply path + one-tap issue start), and `tests/e2e/test_board_tab.py` (the read-only kanban, drill-down + reply + one-tap start in a real browser). All `glab` and session-host calls are mocked at their client seams so the unit suite never shells out — `glab` need not even be installed.
