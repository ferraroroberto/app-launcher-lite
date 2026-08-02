# Board tab — reference

The launcher's fifth surface (issue #164, shipped in steps: **#300** read-only render, **#301** drill-down + reply + one-tap issue start, **#399** split into single-purpose columns; reduced to **four columns** in the fork's Phase 5, which also swapped the data source from GitHub/`gh` to **GitLab/`glab`**; the free-text dispatch bar was removed entirely in Phase 6 — the Board launches work only via the Backlog cards' ▶/⚡ one-tap buttons). It is a **read-only fleet kanban** that answers one question — *"what needs me now, across everything"* — over four **computed** columns, each holding one kind of card. A card moves because reality changed; there is deliberately no drag-and-drop. It renders three independently-degrading live sources (the session-host's session list plus fleet-config-lite's sessions-state and active-issues files) plus a cached GitLab view (`glab`-fetched issues).

On the phone the four columns are a swipeable one-column-per-screen carousel with a count strip on top; desktop shows all four side by side, each column header carrying its own `(N)` item count (#603). The **Your turn** count is the number that matters — its strip button highlights when nonzero.

## The four columns and their data sources

Column assembly is pure logic in `src/board.py::build_board()`. Each column holds one kind of card:

| Column | What populates it |
| --- | --- |
| **Backlog** | Open GitLab issues across every project of the configured group (`gitlab_group`; subgroup projects included). |
| **Bot's turn** | Live session cards whose status is **not** in the needs-you family — i.e. `working`, `unknown`, `idle`, or `idle-finished`. (A session with nothing pending is still the bot holding a workspace, so it is shown here — dimmed client-side, not hidden.) |
| **Your turn** | Session cards whose status is `stalled`, `awaiting-decision`, or `awaiting-input` (#608's split of the old undifferentiated `needs-you` — see below) — a terminal-only column. |
| **Done** | Today's closed issues, since local midnight. |

(The old fifth **Other** column — open PRs + failed/stuck job runs — was dropped in Phase 5; failed jobs stay visible on the Jobs tab, and merge requests have no Board surface for now. `src/gitlab_client.py::search_open_prs()` remains a tested public function awaiting a future MR view.)

**Backlog** cards render thin (repo · #N · title · age) and link out to GitLab. A Backlog card whose repo is present in the projects folder additionally carries **▶ Start / ⚡ YOLO** (see "One-tap issue start" below). If fleet-config-lite's issue workflows have an active marker for the same `<repo>#<number>`, the row gains the accent-soft tint and an explicit “in progress” label, and both launch buttons are truly disabled (#528).

**Done is issues only** (#399): a merged MR that closed an issue is already reflected by that issue showing closed, so there is no MR/issue pairing step — `src/gitlab_client.py::search_done_today()` is the closed-issues query. Group issues have no `closed_after` param, so it queries `state=closed&updated_after=<local midnight>` and filters client-side on `closed_at >= local midnight` (rows updated today but closed earlier, and rows with a null `closed_at`, are dropped).

The GitLab queries (`src/gitlab_client.py`) go through `glab api`: `groups/<group>/issues?state=opened&order_by=updated_at&sort=desc&per_page=100` for Backlog and the `state=closed&updated_after=<midnight>` variant for Done, each with a 20 s per-call timeout. The group is URL-encoded as one path segment (`grp/sub` → `grp%2Fsub`); a non-empty `gitlab_host` rides glab's `GITLAB_HOST` env var for self-hosted instances. Repo short names come from `references.full` — the last path segment before the `#`/`!` marker, so subgroup projects resolve correctly.

## Data endpoints

All Board routes live in `app/webapp/routers/board.py` (the shared launch helpers sit in `app/webapp/routers/board_spawn.py`, split off in #691).

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /api/board` | bearer-token | The four columns — the **5 s poll target**. Cheap only: runs the live session list and two small state-file reads concurrently, plus a pure in-memory read of the GitLab cache. **No `glab` subprocess ever runs on this path.** |
| `POST /api/board/gitlab/refresh` | bearer-token | Runs the two `glab` queries (open issues, closed-today issues) and replaces the cache. The **only** place `glab` is invoked. With an empty `gitlab_group` it never touches a subprocess — the snapshot just carries a "set gitlab_group in Settings" hint as its `error`. |
| `GET /api/board/sessions/{sid}/exchange` | private network + passkey | The last user↔assistant exchange from the agent-aware conversation-source hierarchy (drill-down drawer). |
| `POST /api/board/issues/start` | private network + passkey | One-tap `/issue-start` / `/issue-yolo <N>` on a Backlog card. |

The `GET /api/board` response is `{ generated_at, columns, gitlab: {fetched_at, error}, sessions_state: {available, stale, updated_at}, active_issues: {available, updated_at, count} }`. Each session card carries its raw session fields plus `project`, `status`, and `age_seconds`; each Backlog issue carries a boolean `in_progress`.

## The session-state file join

Launcher-owned session **presence and agent identity** come from the session-host list. That is the authoritative process-liveness source: when a PTY/detached process leaves the host list, its live Board card leaves on the next 5 s poll, regardless of whether an agent shutdown hook fired. Semantic status comes from a **sessions-state file** written by the fleet's agent hooks/extensions — path `sessions_state_file`, default `~/.copilot/hooks/state/sessions-state.json`. The Board only ever reads it.

**The join is agent-aware and exact-id-first.** A state writer may carry `agent` plus the launcher's own id as `launcher_session_id`; that exact id + agent pair wins. Rows with neither field default to the launcher's agent (`copilot`) and retain the normalized-cwd fallback (forward-slash separators, lowercase, trailing slash stripped), but only for a live session of that same agent — a row can never classify a session running a different agent.

Join mechanics (`_claim_walk` / `_match_state_row`):

- Live sessions are walked **newest-first** by `started_at`.
- Each session first claims an unmatched state row with the same `launcher_session_id` and `agent`.
- Without exact identity, the session can claim only an agent-compatible row whose normalized `cwd` is **equal-or-under** the session's project dir. A row carrying a different launcher id is never allowed to fall back by cwd.
- The cwd fallback also requires the candidate row's `updated_at` to be **at-or-after** the session's own `started_at` (#482) — a row genuinely written by this session can never predate the session's existence, so an older row can only be some other, unrelated conversation's leftover state in the same directory. Without this guard, a brand-new live session with no exact-id row yet (its own hook write hasn't landed) could claim an hours-old sibling's row by "most recently updated" and show that sibling's `shared_name` on its card.
- Two legacy candidate rows in one directory → the **most recently updated** (among those not older than the session itself) wins.
- Two live sessions in the same dir → the fresher one claims the row; the older renders `unknown`.
- A fresh unmatched row with **no** live session becomes a state-only external card only after clearing three checks, in order (`_external_row_liveness`) — two deterministic, one an inherently imperfect fallback:
  1. **Reaped-session check (#613).** If the row carries a `launcher_session_id` that is *not* in the session-host's current live list, it's provably dead — the session-host is the sole authority on which PTYs it owns — regardless of how fresh the transcript looks. Suppressed unconditionally.
  2. **Shared-transcript check (#613).** If the row's `transcript_path` is already backing a *live, matched* card, this row is a superseded leftover of that same session — re-keyed when a worker moved from one issue to the next, still pointing at the transcript the live session keeps writing — not a second process. Suppressed unconditionally. (This was the upstream "fleet-config ghost": a merged-and-deleted branch's session kept rendering a `working` card alongside its own live successor.)
  3. **Transcript-freshness fallback.** Only once neither of the above applies does "declared transcript file exists and was written within the last 5 minutes (#613, narrowed from 15)" remain the (imperfect) fallback evidence for a genuinely external row the launcher has no other way to verify — a process that writes once and exits inside the window is indistinguishable from one still running; this narrows that gap, it does not close it.

  Hook status is semantic evidence, not process-liveness evidence: a missing cloud/bridge transcript or a quiet `working`/`needs-you`/`idle` row is suppressed rather than trusted for the state file's 24 h retention window. The first suppression per state id leaves an info-level breadcrumb with the distinct reason (reaped session, claimed transcript, missing path, unavailable file, or quiet transcript) without repeating on every poll.

**Raw hook states** (`_KNOWN_STATUSES`): `working`, `needs-you`, `idle`. Anything else — including a missing row — renders `unknown`. A card's final `status` field can diverge further downstream: the transcript overlay below, and #608's needs-you split (own section below) — the raw `needs-you` string never reaches a card.

Agent capability matrix:

| Agent | Presence | Semantic state in this release |
|---|---|---|
| GitHub Copilot CLI | Session host | Hook-written `working` / `needs-you` / `idle` rows when the hook writer is installed; `unknown` otherwise |

`unknown` is an explicit capability limit, not silence interpreted as certainty.

**Degradation is total and silent.** `read_sessions_state()` returns `{available, stale, updated_at, rows}` and never raises: an absent, unreadable, or corrupt file yields `available: False` with empty rows, and every session card falls back to `unknown` while the GitLab columns render regardless. `stale: True` when the newest row is older than `STATE_STALE_AFTER` (24 h) — i.e. the hooks have stopped writing.

## The active-issues lifecycle join (#528)

Fleet-config-lite's issue workflows publish `~/.copilot/hooks/state/active-issues.json` (convention from upstream fleet-config#376) once an issue branch is ready and remove its row only after the MR merges. Rows are keyed by `<repo>#<number>` and carry `repo`, `number`, `branch`, and `started_at`. `GET /api/board` reads the file beside `sessions-state.json`, canonicalizes the key case-insensitively, and annotates every Backlog issue with `in_progress` before returning the columns.

`read_active_issues()` has the same never-break-Board contract as the session-state reader: a missing, unreadable, corrupt, or non-dict file yields `available: False` with no rows. Invalid records are ignored individually. A record older than `STATE_STALE_AFTER` (24 h) expires on read, matching the writer's prune horizon, so a crashed workflow that never reaches `/issue-finish` cannot permanently block Start/YOLO. This is deliberately a lightweight lifecycle marker, not a GitHub/branch reconciliation source.

## Shared session title, cross-tab (#396)

The state row also carries `name` / `name_source` — the agent's own live per-conversation title, copied in by the `session_state` hook writer where the agent exposes one. `merge_sessions()` copies those onto every card as `shared_name` / `shared_name_source` via the **same** exact-id/agent-aware `_claim_walk` described above, and `attach_shared_names()` runs the identical walk for `GET /api/coding/sessions` (the Coding tab's Running-sessions list) — so a live session resolves to the same state row, and therefore the same title, on both tabs. `name_source: "derived"` marks the generic `<project>-N` fallback (no real title assigned yet); anything else is a genuine title.

The frontend precedence lives in one place — `sessions.js`'s `sessionTitle()` — which `board.js` imports rather than re-deriving a title: a genuine `shared_name` wins outright, the OSC-parsed `live_title` is kept as a same-poll-cycle-faster supplement (it updates sub-second inside an open terminal, ahead of the next state-file poll), then `prompt_title`, then a *derived* `shared_name`, then the launch name. See [Naming sessions from the conversation](#interactive-terminal-from-the-phone) in the README for the full precedence history (#266, extended #396).

## The transcript-activity overlay (#305, #309)

The fleet-config-lite hooks flip status only on a few events (`UserPromptSubmit` → working, `Stop` → needs-you, `Notification` → needs-you/idle). Two resume paths fire **no hook at all**: answering an AskUserQuestion / permission prompt resumes the turn without a prompt submission, and a prompt typed into an already-running session is queued and delivered mid-turn. In both cases a `needs-you` stamp sticks while the agent is visibly working — the bug #305 fixes. Ground truth is the transcript JSONL, which is appended continuously during a turn and quiet on stop.

**The rule** (`_transcript_overlay()`): only when the hook status is `needs-you` or `idle` (it never overrides `working`/`unknown`), if real transcript activity is newer than the row's stamp by more than `_RESUME_EPSILON` (10 s), override the status to `working` and re-anchor the card's age to that activity timestamp. When the agent genuinely stops, the Stop hook re-stamps the row **after** the final transcript write, so `needs-you` wins immediately — no delayed alert. The 10 s epsilon absorbs the Stop hook and the final message write landing a couple of seconds apart in either order.

**Two-stage probe (the #309 refinement):**

1. **Pre-filter — raw mtime `os.stat`.** If the transcript's mtime is not more than the epsilon past the stamp, nothing was written after it — return unchanged and skip the read. This is the cheap gate.
2. **Confirmation — bounded tail read** (`_last_activity()`, only when the mtime clears the epsilon). Reads the **last 8 KB** of the transcript, splits into lines, walks in reverse, and returns the timestamp of the newest line whose `type` is `"assistant"` or `"user"` **and** which carries a dict `message` payload.

**Why mtime alone is too blunt (the #309 bug):** an agent may append **non-message metadata lines** to the transcript *after* Stop stamps the row — `system`, `ai-title`, `mode`, `permission-mode`, `pr-link`, `file-history-snapshot`, and more. Some land seconds-to-minutes post-Stop (a `pr-link` when a PR is detected, a title refresh), pushing mtime past the epsilon with no real resume — which made a finished, waiting session wrongly overshoot to `working`. `_last_activity` ignores every one of those types (only `assistant`/`user` lines with a dict `message` count), and skips torn/unparseable lines. No conversation event in the tail → the hook status is kept.

**Pending background dispatch (the #464 gap):** the agent's own turn can genuinely end — Stop fires, stamping `needs-you` — while it's still waiting to hear back from a background sub-agent (`Agent`/`Task` tool) or backgrounded shell command it dispatched during that turn. The parent transcript then goes **quiet** until that work's own completion notice lands, so the activity check above never fires during the whole in-flight window: there is nothing written *past* the stamp for the mtime pre-filter to find. `_has_pending_background_dispatch()` closes this gap with an independent, **not mtime-gated**, check over the same 8 KB tail, unioning two schemes:

1. **Legacy, `toolUseResult`-keyed** (`_launched_background_ids`/`_notified_background_ids`, #464): a background dispatch's synchronous launch ack rides `toolUseResult` (a sibling of `message`) — `backgroundTaskId` for a backgrounded `Bash` command, or `isAsync: true` + `agentId` for an async sub-agent — and the id resurfaces in a `<task-id>` once the work completes.
2. **`tool_use`-id-keyed, Bash-only** (`_launched_bash_dispatch_ids`/`_notified_bash_dispatch_ids`, #576): a live-transcript spot-check found real `run_in_background` Bash dispatches no longer carry `backgroundTaskId` on the ack at all — scheme 1 alone sees nothing launched and never overrides the status. Scheme 2 reads the launch straight off the assistant's own `tool_use` block (`name == "Bash"`, `input.run_in_background is True`) — part of the stable Anthropic message-content-block shape, not an internal result field — and resolves it via the notification's own `<tool-use-id>` correlation tag (confirmed present alongside `<task-id>` in a live notification). Deliberately **not** resolved by an ordinary `tool_result` for the same id: that's just the "launched in background" ack, and treating it as completion would resolve the dispatch the instant it fires. Scheme 2 is intentionally scoped to `Bash` only — most `Task`/`Agent` dispatches are ordinary synchronous sub-agent calls, so a tool-call-id-keyed check there would have to tell "still running async" apart from "finished, this is just the normal blocking reply," and that's left to scheme 1's explicit `isAsync` marker instead.

Completion notices are **not** ordinary `assistant`/`user` lines (they land as a `queue-operation` line's `content` or an `attachment` line's `attachment.prompt`), so they're invisible to `_last_activity` too; either scheme finding a launched id with no matching completion anywhere later in the tail means the work is still outstanding, and the status stays (or is forced back to) `working`.

**Enqueue vs. dequeue (#601):** a `<task-notification>`'s tags first appear on a `queue-operation` line whose `operation` is `"enqueue"` — that only means the background result is *ready*, not that the agent has received it. A live-transcript spot-check found a session where the enqueue line was the last thing written to the whole transcript, with no later `dequeue`/`remove` — the notification was still sitting in the queue, unconsumed, while a stale `needs-you` Stop stamp stood untouched for several minutes. So a notification only counts as delivered once a *later* `dequeue`/`remove` operation pops it off the (FIFO) queue, tracked positionally since a `dequeue` line carries no content of its own to correlate by id; other queue traffic (e.g. a prompt typed into an already-running session, which rides the same enqueue/dequeue mechanism) occupies a queue slot too and is tracked untagged rather than skipped, so a later dequeue can't misalign onto the wrong notification. The `attachment`-shaped notification is not queue-gated — it isn't a `queue-operation` line at all, so it already represents a delivered conversation event.

Net cost per 5 s poll: one `os.stat` per session row, one ≤8 KB tail read for rows in a waiting status whose mtime moved (the #309 activity check), plus one more ≤256 KB tail read, unconditional for every row in a waiting status, for the #464/#576 pending-dispatch check and #608's needs-you split below (they share the same tail read window and, for the split, largely the same scan).

## The needs-you four-way split (#608)

The undifferentiated `needs-you` conflated four operationally distinct situations, forcing a caller (a human glancing at the Board, or an automated consumer polling `/api/board` to decide what needs attention) to fetch a session's full exchange just to tell them apart. `src/board_transcript.py::_refine_waiting_status()` runs after `_transcript_overlay()` and, whenever the status is still `needs-you` at that point, resolves it to exactly one of:

| Status | Meaning | Routes to |
| --- | --- | --- |
| `stalled` | A background dispatch (sub-agent or backgrounded shell) has been outstanding past `_STALLED_DISPATCH_AFTER` (30 minutes) — a real anomaly, not healthy waiting. Decided in `_transcript_overlay()` itself (it already computes the dispatch's age), not in `_refine_waiting_status()`. | Your turn |
| `awaiting-decision` | A pending `AskUserQuestion` or `ExitPlanMode` tool_use with no `tool_result` yet — the agent is blocked on a human picking an option, not just "needs a prompt." | Your turn |
| `awaiting-input` | Everything else the split can't more specifically classify: a genuinely typed prompt is expected, some other/unrecognized tool_use is pending, or the tail gave no usable signal at all (missing transcript, unreadable file, unparseable content). This is the old undifferentiated `needs-you` meaning, kept as the safe generic fallback. | Your turn |
| `idle-finished` | The turn ended clean — no pending tool_use of any kind found in the tail, and the session's own repo has no still-open issue-workflow marker (see below). The session doesn't actually need anyone; it's just the bot holding a workspace. | Bot's turn |

**`idle-finished` is downgraded when the session's repo still has an open issue (#627).** A clean stop with nothing pending is only an *absence* of evidence, not positive proof the session's own work is done — a turn cut off mid-task (no tool call issued, nothing left to structurally detect) looks identical. Per-session proof (the branch merged, the issue closed) would need a fresh `git`/`glab` call every poll, which this Board's `GET /api/board` deliberately never makes (`scanner.git_status`'s own on-demand-only docstring). `src/board_sessions.py::merge_sessions()` instead takes the already-fetched `active-issues.json` marker file (the same data `_mark_active_backlog()` uses to dim Backlog cards) via its `active_issue_repos` kwarg: when a card would resolve to `idle-finished` but its own repo (worktree-suffix normalized, `active_issue_repos()`) still has an unexpired active-issue marker, the card reports `awaiting-input` instead. This is repo-level, not branch-level — coarser than the issue's own suggested evidence, and it can occasionally keep a genuinely-finished session in view (a false `awaiting-input`) — but that's the asymmetry #627 asks for: under-claim, don't over-claim. A turn that ends with plain trailing text and no active-issue marker for its repo at all (no issue workflow tracked, or one already finished) still has no cheaper signal available and renders `idle-finished` as before — a named, not silently accepted, remaining gap.

**The `stalled` threshold is deliberately generous.** A session was observed correctly *not* alerting through two ~9-minute e2e-gate runs in one day (this repo's own full `verify-before-ship.ps1` gate is documented at ~10-11 minutes, CI's own investigate threshold is >12 minutes) — that's ordinary waiting, not a stall. The actual property a caller wants — "a turn that ended with nothing that will ever wake it" — can't be observed directly, so duration is only a proxy; a threshold comfortably above every observed healthy wait trades a slower alert for not crying wolf. A dispatch whose launch line carried no parseable timestamp is genuinely outstanding but not age-able — that case stays `working`/`awaiting-input` rather than being guessed as `stalled` from the sentinel epoch value; a low-confidence `stalled` call is worse than a late one.

**`idle-finished` requires positive evidence, never a default for "couldn't check."** `_pending_tool_use_names()` (via `_tail_tool_use_pairs()`) distinguishes "read the tail, genuinely found nothing pending" from "no usable signal at all" (no transcript path, missing/unreadable file, or a tail where nothing parsed) using the same "real conversation event" test as `_last_activity()` — an `assistant`/`user` line carrying a dict `message`. Only the former yields `idle-finished`; the latter always falls back to `awaiting-input`, the same asymmetry the `stalled` threshold applies to an unparseable launch timestamp.

**Scoped to `needs-you` only.** `idle` never goes through this split — it's a distinct, largely-unused raw status (see above), not part of the four meanings #608 targets.

## The drill-down drawer and its PTY-write path (#301)

Tapping a live session card opens an **inline drill-down drawer** on the card (`board.js::buildDrawer`). It shows the last user↔assistant exchange plus a reply box, and while any drawer is open the **5 s poll pauses** (`fetchBoard()` self-gates on `state.boardExpanded`) so a re-render can never wipe a half-typed reply.

**Reading the exchange (#457).** `GET /api/board/sessions/{sid}/exchange` first resolves the exact live session-host row; a missing session returns `reason: session_not_found`, so the endpoint never guesses by cwd. The source hierarchy is then:

1. **Native JSONL** — when the claimed hook row declares an existing structured transcript, `board.last_exchange()` reads its last 256 KB and extracts the newest assistant text plus the nearest preceding plain-string user prompt, skipping thinking/tool-use and harness plumbing.
2. **Exact-id launcher capture** — the common fallback for sessions whose hook path is missing and agents without a native adapter. The last 512 KB of `webapp/sessions/<launcher-session-id>.transcript` is replayed through `pyte`; assistant blocks are selected by the terminal's leading-bullet colour contract, while the matching `<id>.log` supplies the last submitted input. Raw ANSI and full-screen repaint bytes never reach the API.

All reads happen only when the drawer opens — `GET /api/board`'s 5-second poll still performs no transcript parsing or LLM call. Display caps remain 6000 assistant characters and 1500 user characters. The response carries `source` (`native` or `launcher`) and a machine-readable unavailable `reason`; the drawer distinguishes a true `no_exchange` empty state from a sanitized source error. Source fallback/failure leaves an info-level log breadcrumb.

**Replying to the live PTY.** The reply box is offered only for `alive && kind === 'pty'` cards (a detached console or state-only card has no reachable stdin). The reply proxies through `POST /api/coding/sessions/{sid}/input` — body `{data, submit}`, one call to the session-host, which owns framing + settle-then-submit internally via `PtySession.submit_input()` (#611, ported from the compose bar's `framePaste`/`sendSubmit`/`bulkSettle` — #166/#450/#499). `data` may be blank when `submit` is true — a bare submit against whatever is already sitting in the composer, with no text write — the recovery path for a message stranded by the settle race, previously reachable only by tapping the phone's own compose Send by hand. On success the drawer closes (`boardExpanded = null`, poll resumes) and `board.js::moveCardToBotTurn()` optimistically relocates the card into *Bot's turn* client-side, immediately — before the server-side prompt-submit hook plus transcript overlay have had time to flip `sessions-state.json` (#461). No extra re-poll fires right away (it would almost always still see the pre-hook state and revert the optimistic move); the regular 5 s poll reconciles with ground truth as always. A **⚡** in the drawer opens the full-control terminal.

**Delivery honesty (#607/#611).** `{"ok": true}` means the write (and, if requested, the submit) actually reached a live session — a dead/exited session reports 409 instead of a false 200 (#607), and the settle-then-submit sequence (#611) means `submit: true` in the response reflects that the CR was actually sent after the paste's ingest was given a chance to settle, not just that bytes were written. Bracketed-paste framing is applied only when the PTY's own output has announced DECSET 2004 is on (tracked off the raw output stream, never stripped from it) — a literal `\x1b[200~` sent blind to an agent that never asked for it is garbage, not a paste.

**One-tap issue start.** A Backlog card whose repo is in the projects folder carries **▶ Start / ⚡ YOLO** → `POST /api/board/issues/start`, body `{repo, number, mode, rows, cols}`. Both controls are disabled while the card's active-issue marker is fresh (#528), preventing a redundant conflicting session. Otherwise the route is injection-safe by construction: `prompt = "/issue-<mode> <number>"` with mode allowlisted and number int-validated. Every launch uses the persisted Coding model (Phase 6 removed the per-launch model selector; 400 if the Copilot CLI isn't installed). It resolves the repo to a project dir (404 if not present locally) and spawns a streamed PTY session — the `/issue-*` skills inherit worktree isolation for free, claiming the repo and building in a sibling worktree when the primary checkout is busy. It opens the PC-mirror window by default — a desktop client, the phone, and a headless loopback API caller all get one (#609); only a genuine in-page loopback browser explicitly opts out (`in_page: true`, the SPA's own signal) and stays on the in-page terminal.

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

The pre-ship gate (`pwsh -File scripts/verify-before-ship.ps1`) covers the Board via `tests/test_board.py` (column assembly, the cwd-join, the transcript overlay, the API shape), `tests/test_gitlab_client.py` (the `glab` client — real GitLab API shapes in fixtures, cache semantics, error mapping), `tests/test_board_drilldown.py` (the exchange read + reply path + one-tap issue start), and `tests/e2e/test_board_tab.py` (the read-only kanban, drill-down + reply + one-tap start in a real browser). All `glab` and session-host calls are mocked at their client seams so the unit suite never shells out — `glab` need not even be installed.
