# Board tab — reference

The launcher's fifth surface. It is a **read-only kanban** that answers one question — *"what needs me now, across everything"* — over computed columns, each holding one kind of card. A card moves because reality changed; there is deliberately no drag-and-drop. The Board joins four independently-degrading live sources (the session-host's session list, the Copilot hook state files, and today's job runs) with a cached GitLab view (issues and merge requests fetched through `glab`).

> **Verification status.** Every GitLab read and write on this page goes through `glab api` against GitLab's REST v4 API and is pinned to fixtures in `tests/unit/board/test_gitlab_client.py`. The issue reads and writes are verified live on gitlab.com; the merge-request rows are not (no merge request existed to read). [gitlab.md](gitlab.md) lists each item as verified or `unknown`. The Copilot hook files and the chief helpers come from fleet-config-lite and are called out where they matter. See [gitlab.md](gitlab.md) for the GitLab side and the README's Status section for the unknowns.

Each column is a collapsible section card, the same `details.card.card--collapsible` container the other tabs use, with the column's item count in its summary. The lanes run **Backlog**, the chief's plan (hidden until the chief exists), **Bot's turn**, **Your turn**, **MRs and jobs** (the `other` column) and **Done today**. On the phone the lanes stack: **Bot's turn** and **Your turn** open, the rest folded, each folded lane showing its count or, when something is wrong, its exception in tone (`1 failing`, `1 question for you`). On the desktop grid the lanes sit side by side, one column per lane that holds rows (`board.js::sizeLaneGrid`); an empty lane folds into a one-line strip under them.

## The columns and their data sources

Column assembly is pure logic in `src/board/__init__.py::build_board()`.

| Column | What populates it |
| --- | --- |
| **Backlog** | Open GitLab issues across every project of the configured group (`gitlab_group`, subgroups included). |
| **Bot's turn** | Live session cards whose status is **not** in the needs-you family: `working`, `unknown`, `idle`, `idle-finished`, `tool-pending` or `waiting-on-task`, plus the chief's card when one exists. A session with nothing pending is still the bot holding a workspace, so it is shown here, dimmed client-side. |
| **Your turn** | Session cards whose status is `stalled`, `awaiting-decision` or `awaiting-input` (the needs-you split below), excluding the chief; then the owner checks waiting on you (see Owner checks). |
| **Other** | Open merge requests, then today's failed, unconfirmed or stuck job runs. An `unconfirmed` card is a run whose exit code says the scheduled-run adapter could not establish whether it delivered; it keeps its card with the attention accent and a `?` glyph rather than the failure red. See [jobs-tab.md](jobs-tab.md) for terminal outcomes. |
| **Done** | Today's closed issues since local midnight, latest close first, sorted on `closed_at`. |

**Backlog** cards render thin (project, number, title, age) and link out to GitLab. A card whose project exists in the projects folder also carries **Start** and **YOLO** (see One-tap issue start). If the active-issues file holds a marker for the same `<repo>#<number>`, the row gains the accent-soft tint and an explicit "in progress" label and both buttons are disabled (see the lifecycle join).

**Backlog buckets.** Every open issue sits in exactly one bucket, the buckets add up to the Backlog's count, and every other count on the Board reads this same assignment. `src/board/backlog_buckets.py` joins the GitLab cache, the chief's plan (when present), and the active-issue markers, and stamps each card with a `bucket` and its `bucket_chips`; first match wins. Issues carrying a non-actionable bookkeeping label (the `/codebase-audit` ledger) are cached apart as records and never count as work.

**Done is issues only.** A merged MR that closed an issue is already reflected by that issue showing closed, so Done is just the closed-issues read (`src/clients/gitlab.py::search_done_today()`).

**Done's earlier days load on tap.** A **Show earlier** button under Done reads one more local day per tap (yesterday, then the day before, up to seven) from `GET /api/board/done?days_back=N`, lists each under a day header (`board/done-earlier.js`) and keeps them until the page reloads; the 5 s poll never fetches them. A day is the host's local calendar day: GitLab stamps in UTC, so `gitlab.py::local_day_window()` converts local midnight to the next local midnight into a UTC range (a DST day is 23 or 25 hours, not 24), and the browser formats stamps in the viewer's zone.

**A Done row's tap opens its issue, and it carries two actions: MR and Proof.** The merged-MR read links each closed issue to the MR that closed it (a GitLab closing pattern in the MR description such as `Closes #N`; the later MR wins), so a Done card carries the MR number, its URL and the `VISUAL_PROOF=` state the MR description declares. **MR** opens GitLab. `src/board/proof_reports.py` reads each repo's proof store read-only: `<projects_dir>/<repo>/.proof/index.json` keyed by MR number and one `<MR>/index.html` per merged UI MR. `proof` is `available` when the index holds the closing MR (the row gets a **Proof** chip that opens `GET /api/board/proof/{repo}/{pr}`, a self-contained page served under a scriptless CSP behind the Tailscale and passkey gate), and `missing` when the description says `VISUAL_PROOF=missing|stale|failing|unknown` and nothing is stored (the row gets an attention chip). The route's `{pr}` segment carries the MR number. No proof tooling ships in lite yet, so most rows show neither chip until a repo writes `.proof/`.

**The GitLab reads** (`src/clients/gitlab.py`, issues verified live, merge requests `unknown`) go through `glab api` against the REST v4 API with a per-call timeout, scoped to `gitlab_group` and, for a self-hosted instance, `gitlab_host` (carried on `GITLAB_HOST`). Open issues feed Backlog, open MRs feed Other, closed issues in the local day's window feed Done. Every card's second line ends in a date formatted in the browser (`format.js::fmtCardStamp`: the clock for today, `14 Oct` otherwise, the viewer's local time, never converted on the server).

## Data endpoints

All Board routes live in `app/webapp/routers/board.py`, except the `/api/board/chief/*` routes in `app/webapp/routers/board_chief.py` and the owner-check routes in `app/webapp/routers/board_owner.py`, both mounted on the same router. The spawn-then-type mechanics shared by the launch routes sit in `app/webapp/routers/board_spawn.py`.

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /api/board` | bearer-token | The columns, the **5 s poll target**. Cheap only: the live session list, two small state-file reads and one jobs-runs walk run concurrently, plus a pure in-memory read of the GitLab cache. **No `glab` subprocess ever runs on this path.** |
| `POST /api/board/github/refresh` | bearer-token | Runs the GitLab reads and replaces the cache. The **only** place `glab` is invoked. The path keeps the full app's name so the Board's client code is shared; it refreshes GitLab. |
| `GET /api/board/done` | bearer-token | One earlier local day of closed issues, for Show earlier. |
| `GET /api/board/proof/{repo}/{pr}` | Tailscale + passkey | A merged UI MR's visual-proof report. |
| `POST /api/board/issues/file` | Tailscale + passkey | File a new GitLab issue in a project of the group. |
| `GET /api/board/sessions/{sid}/exchange` | Tailscale + passkey | The last user-to-assistant exchange of a session (drill-down drawer). |
| `POST /api/board/issues/start` | Tailscale + passkey | One-tap `/issue-start` or `/issue-yolo` for a Backlog card. |

The chief routes (`/api/board/chief-plan`, `/api/board/chief/*`) are listed under The chief below.

The `GET /api/board` response carries the columns, source health (`github: {available, fetched_at, error, truncated}` for the forge cache, `live_sessions`, `sessions_state`, `active_issues`, `jobs_snapshot`), `backlog_buckets` and `stale` / `age_seconds`. `stale: true` means the build ran past its 1.5 s deadline and this is the last good board, `age_seconds` old. **`github.available: false` means the Backlog and Done lists, and the MR half of Other, are unknown, not empty.** The key keeps its historical name; it describes the GitLab cache.

**`live_sessions.available: false` means the session-host list could not be read**, with the reason in `error`. Bot's turn and Your turn are then unknown, not empty: an empty one shows `—` instead of `0` and "Session-host unreachable — sessions unknown." instead of its usual text. **"Nothing needs you right now." is reachable only from a session list actually read.** The failure logs one warning when the session-host goes unreadable and an info breadcrumb when it recovers, not one line per poll.

**A job whose run history cannot be read is surfaced, not skipped.** `jobs_attention()` gives it an Other card with `state: "unreadable"` and the OS error in `error`, rendered with the attention accent rather than the failure accent, since whether it needs you is unknown.

**A failed or unconfirmed job card can be dismissed once dealt with.** A tap `POST`s `/api/jobs/<job_id>/runs/<run_id>/dismiss`, which records the run in `webapp/jobs-dismissed.json` (`src/jobs/dismissed.py`: gitignored, machine-local, newest 20 per job, written atomically) and the card leaves at once. The key is the run, so the job's next failed run raises a fresh card. The run record is untouched: the Jobs tab still shows the failure. Only a finished, non-deferred failure is dismissible (anything else answers 409); `stuck` and `unreadable` cards have no verb because they describe a live problem.

## The session-state file join

Launcher-owned session **presence and agent identity** come from the session-host list on `:8466`. That is the authoritative process-liveness source: when a PTY or detached process leaves the host list, its live Board card leaves on the next 5 s poll, whether or not a shutdown hook fired. Semantic status comes from a **sessions-state file** written by fleet-config-lite's Copilot hooks (`sessionStart`, `userPromptSubmitted`, `agentStop`, `sessionEnd`) — path `sessions_state_file`, default `~/.copilot/hooks/state/sessions-state.json`. The Board only ever reads it. On Copilot CLI 1.0.105 interactive and `-p` sessions fire all four hooks.

**The join is exact-id-first.** A state row may carry the launcher's own id as `launcher_session_id`; that exact id wins. Rows without it fall back to the normalized cwd (forward-slash separators, lowercase, trailing slash stripped), equal-or-under the session's project dir. A row carrying a different launcher id is never allowed to fall back by cwd.

Join mechanics (`_claim_walk` / `_match_state_row`):

- Live sessions are walked **newest-first** by `started_at`.
- Each session first claims an unmatched state row with the same `launcher_session_id`.
- Without exact identity, the cwd fallback also requires the candidate row's `updated_at` to be at-or-after the session's own `started_at`: a row written by this session can never predate it, so an older row is some other conversation's leftover in the same directory.
- Two live sessions in the same dir: the fresher one claims the row, the older renders `unknown`.
- A fresh unmatched row with **no** live session becomes a state-only external card only after three checks, in order: a row whose `launcher_session_id` is not in the session-host's live list is provably dead and suppressed; a row whose `transcript_path` already backs a live matched card is a superseded leftover and suppressed; only then does "declared transcript exists and was written within the last 5 minutes" remain as imperfect fallback evidence. An external card reports `alive: null` and its status is never `stalled`.

Hook status is semantic evidence, not process-liveness evidence: a quiet `working`, `needs-you` or `idle` row with no backing transcript is suppressed rather than trusted for the state file's 24 h retention window. The first suppression per state id leaves one info-level breadcrumb with the distinct reason.

**Raw hook states** (`HOOK_STAMPS` in `src/board/card_status.py`): `working`, `needs-you`, `idle`. Anything else, including a missing row, renders `unknown`. A card's final `status` can diverge further downstream (the transcript overlay and the needs-you split below); the raw `needs-you` string never reaches a card.

**The card status set is declared once.** `src/board/card_status.py` lists the card values with the properties every consumer groups them by (`needs_human`, `chief_resting`, `doorbell_idle`, `screen_checked`). `sessions.py::derive_card_status()` is the one ordered derivation that live and external cards both run, and `tests/unit/board/test_card_status_contract.py` fails when the front end's label table drifts from the declaration.

`unknown` is an explicit capability limit, not silence interpreted as certainty. **Degradation is total and silent:** `read_sessions_state()` returns `{available, stale, updated_at, rows}` and never raises; an absent, unreadable or corrupt file yields `available: False`, every session card falls back to `unknown`, and the GitLab and jobs columns render regardless. `stale: True` when the newest row is older than 24 h, meaning the hooks have stopped writing.

## The active-issues lifecycle join

fleet-config-lite's issue skills publish `~/.copilot/hooks/state/active-issues.json` once an issue branch is ready and remove the row after the MR merges. Rows are keyed by `<repo>#<number>`. `GET /api/board` reads the file beside `sessions-state.json`, canonicalizes the key case-insensitively and annotates every Backlog issue with `in_progress`. `read_active_issues()` never breaks the Board: a missing, unreadable, corrupt or non-dict file yields `available: False`, invalid records are ignored individually, and a record older than 24 h expires on read so a crashed workflow cannot block Start and YOLO forever.

**Claim owner liveness.** Rows may carry `owner_session_id` / `owner_pid` / `owner_host`. `src/board/active_issue_claims.py` classifies each claim; each Backlog card gets `claim_state`:

| `claim_state` | Meaning | `in_progress` | Card |
|---|---|---|---|
| `live` | owner session or pid positively alive | `true` | tinted, "in progress", actions disabled |
| `dead` | owner session absent from a readable list, or pid confirmed gone | `false` | "stale claim", actions enabled |
| `unknown` | owner-less legacy row, other host, unreadable list, or classifier error | `true` | tinted, "in progress (unverified)", actions disabled |

`unknown` is never folded into either verdict. Classification is read-only: the Board never rewrites or prunes the file. The classifier lives in fleet-config-lite's active-issue helper, loaded by file path from `fleet_config_dir`; until that helper ships the verdict is `unknown`.

## Shared session title, cross-tab

The state row may carry `name` / `name_source`, the session's title as the hook recorded it. `merge_sessions()` copies those onto every card as `shared_name` / `shared_name_source` via the same exact-id walk, and `attach_shared_names()` runs it for the Coding tab's Running-sessions list, so a live session resolves to the same title on both tabs. `name_source: "derived"` marks the generic `<project>-N` fallback. The front-end precedence lives in `sessions.js`'s `sessionTitle()`: a genuine `shared_name` wins, then the OSC-parsed live title, then the prompt title, then a derived `shared_name`, then the launch name.

## The transcript-activity overlay

The hooks flip status only on a few events. Two resume paths fire no hook: answering a question or permission prompt resumes the turn without a prompt submission, and a prompt typed into a running session is queued and delivered mid-turn. A `needs-you` stamp then sticks while the agent is visibly working. Ground truth is the transcript file the state row names.

**The rule** (`_transcript_overlay()`): only when the hook status is `needs-you` or `idle`, if real transcript activity is newer than the row's stamp by more than 10 s, the status becomes `working` and the card's age re-anchors to that activity. When the agent genuinely stops, the `agentStop` hook re-stamps the row after the final transcript write, so `needs-you` wins immediately. The probe is two-stage: a raw `os.stat` mtime pre-filter, then a bounded 8 KB tail read that finds the newest line carrying a conversation message. The 5 s poll therefore stays cheap.

**Copilot transcript reader: planned.** The overlay and the needs-you split below were built around a structured transcript tail. Copilot's own transcript flavor (and context percent) is a planned port step; until it lands the structured-tail signals are absent for Copilot sessions and the classification falls to the safe generic `awaiting-input` rather than asserting anything. Copilot's `events.jsonl` `session.usage_record` carries `inputTokens` / `maxPromptTokens`, verified on CLI 1.0.105, but nothing in the Board reads it yet.

## The needs-you split

`src/board/transcript.py::_refine_waiting_status()` runs after the overlay and, whenever the status is still `needs-you`, resolves it to exactly one of:

| Status | Meaning | Routes to |
| --- | --- | --- |
| `stalled` | A background dispatch has been outstanding past 30 minutes, a real anomaly. A live launcher PTY's screen is read first, so a picker on screen makes the card `awaiting-decision` instead. | Your turn |
| `waiting-on-task` | The turn ended while its own background task is still running. A notification wakes the session when the task finishes, so no human is awaited. Not counted in "N needs you". | Bot's turn |
| `awaiting-decision` | A pending question or plan-approval tool call with no result yet: the agent is blocked on a human picking an option. | Your turn |
| `tool-pending` | An ordinary tool call is unresolved at the tail's end. Genuinely ambiguous (still running, or permission-gated), so reported as its own state rather than folded into either side. | Bot's turn |
| `awaiting-input` | Everything else the split cannot classify, including no usable signal at all. The safe generic fallback. | Your turn |
| `idle-finished` | The turn ended clean with nothing pending and the session's repo has no still-open issue-workflow marker. | Bot's turn |

`idle-finished` is downgraded to `awaiting-input` while the session's repo still has an open active-issue marker (a clean stop is an absence of evidence, not proof the work is done), and it requires positive evidence: "could not check" always falls back to `awaiting-input`. The `idle` raw status never goes through the split. Whether Copilot's `ask_user` tool surfaces as `awaiting-decision` through the transcript path is **unknown** until the Copilot transcript reader exists; `ask_user` blocking under autopilot is also an open probe.

## Owner checks

Work that waits on the owner rather than on a session: shipped work to try as a user (`validate-after`) and a step owed before work can start (`before-start`). **Not yet live, and opt-in:** in lite these are meant to ride GitLab scoped labels (for example `owner::needs-validation` and `owner::needs-owner`) read through `glab`; the list is off until the labels exist in the group and the reads have been verified against a real GitLab. `src/board/owner_checks.py` holds the cache, the marks and the answer plumbing; it was written against GitHub labels, so the GitLab mapping is part of the unfinished port.

**The list is a cached read with its age.** It costs calls per project, so it never rides the 5 s poll: the Board's refresh starts it in a background thread (one at a time, no closer than a minute apart), and `GET /api/board` carries the cache as `owner_checks` (`available`, `fetched_at`, `error`, `refreshing`, `unknown`, `items`). `available: false` until a list has landed, so the Board says *Owner checks not loaded yet* instead of an empty list; a read that did not land is `unknown`, never "none".

**On the Board** the items sit in Your turn under the sessions in three disjoint groups: *Before you start*, *Validate to close* and *Try what shipped*. The launcher keeps four marks of its own in `webapp/owner-check-marks.json` (`later` snooze, `problem`, `paused`, `closing`); a marked item stays listed but leaves the "needs you" count. Answers have their own sheet, apart from the chief's, with the Chat composer's paperclip for attaching files (`answer-files.js`).

## The chief (planned)

> **Not active until fleet-config-lite ships the chief.** The launcher code for the chief is present (`src/chief/`, `app/webapp/routers/board_chief.py`, `app/webapp/static/board/chief-*.js`) but depends on helpers fleet-config-lite does not have yet, so the chief card, plan card and answer sheet stay hidden and the Board works fully without them. Every behavior below is the design, not something verified.

The chief is a conversational orchestrator: one long-lived Copilot session that reads the Board and dispatches work to lane sessions.

- **Plan.** The chief writes a plan file (`chief_plan_file`, default `~/.copilot/hooks/state/chief-plan.json`). `src/chief/plan.py::read_plan_index` reads it; the Board shows a plan card with lanes, queue status (`src/chief/queue_status.py`) and open questions, and `GET /api/board/chief-plan` serves it. Pruning stale rows is `POST /api/board/chief-plan/prune`.
- **Mailbox.** Events (a job failed, a script is missing, a worker cap raised, an owner answer) are published into `chief_inbox_dir` (default `~/.copilot/hooks/state/chief-inbox`) by `src/chief/job_failed.py`, `script_missing.py`, `cap_raised.py` and `answers.py`. The format contract is fleet-config-lite's.
- **Journal and identity.** `src/chief/journal.py` reads the chief's journal for the Board; `identity.py` and `pointer.py` identify the chief session among the live rows (`chief_managed_file`), and the chief is never shown in Your turn.
- **Doorbell and compaction.** The launcher types a short wake-up line into an idle chief's composer, and a threshold-based compaction asks the chief to run `/compact`. Both need the chief's composer keys; whether the doorbell keys, a queued `/compact` runs at turn end, `sessionStart` `additionalContext` is honoured and `agentStop` can force another turn are all **unknown** on Copilot CLI 1.0.105.
- **Dispatch.** A dispatcher may add an optional `brief` to the issue-start route: the launcher writes it to `webapp/briefs/<uuid>.md` and appends only that path to the lane's prompt (`--brief <path>`), because free text must never reach an unquoted command line (`src/chief/dispatch_brief.py`).
- **Answer sheet.** The Chief's plan card opens a sheet of the chief's open questions; answers go back to the chief through the same composer path as a drawer reply. Routes: `POST /api/board/chief/answered` and the other `/api/board/chief/*` routes, all Tailscale and passkey gated.
- **Settings.** `chief_model`, `chief_context` and `chief_effort` (the chief's Copilot model, `--context` and `--effort`; the last two empty means the Coding agent's) are in the webapp config and set in Settings > Chief.

## The drill-down drawer and its PTY-write path

Tapping a live session card opens an **inline drawer** on the card (`board/drawer.js::buildDrawer`). It shows the last user-to-assistant exchange rendered as markdown, the shared composer (`composer.js`, the same component the session overlay mounts) and actions: **Chat**, **Terminal**, and a kebab holding **Rename** and **Stop** (Stop always asks in the themed confirm sheet).

**Reading the exchange.** `GET /api/board/sessions/{sid}/exchange` first resolves the exact live session-host row; a missing session returns `reason: session_not_found`, so the endpoint never guesses by cwd. It builds one answer at a time per session off the default executor; past 1.5 s the last good answer returns with `stale: true` and `age_seconds`. The sources, in order:

1. **Native transcript.** When the claimed hook row names an existing structured transcript, `board.last_exchange()` reads its last 256 KB and extracts the newest assistant text and the nearest preceding user prompt. For Copilot this depends on the planned Copilot transcript flavor and is **not yet available**.
2. **Exact-id launcher capture.** The working path for Copilot today: the last 512 KB of `webapp/sessions/<launcher-session-id>.transcript` is replayed through `pyte`; assistant blocks are selected by the terminal's leading-bullet colour contract, and the matching `<id>.log` supplies the last submitted input. Raw ANSI and repaint bytes never reach the API. How well the bullet contract matches Copilot's rendering is **to be verified**.

All reads happen only when the drawer opens; the 5 s poll performs no transcript parsing. Display caps are 6000 assistant and 1500 user characters. The response carries `source`, a machine-readable unavailable `reason`, and the drawer distinguishes a true `no_exchange` empty state from a sanitized source error.

**Replying to a live session.** The composer is offered for every `alive` card. A detached session is typed into its PC console and answers `delivered: "unconfirmed"`; an agent the console-input probe never proved keeps the draft but greys Send. The toast is `sessions.js::sendOutcome()`'s verdict wording, shared with Chat mode: *Sent*, *Sent, not confirmed*, *Queued* and *Not submitted* stay distinct, and a failed send keeps the draft. Keys are disabled in the drawer: the Board never opens the PTY's WebSocket.

**Delivery honesty.** `{"ok": true}` means the write (and, if requested, the submit) reached a live session; a dead session reports 409 instead of a false 200. Bracketed-paste framing is applied only when the PTY's own output has announced DECSET 2004. A bulk payload is checked for **ingest evidence** (the PTY painted it back) before its Enter is written at all, so a blind Enter never lands in a repainting composer:

| `reason` | HTTP | Meaning |
|---|---|---|
| `ok` | 200 | Ingest seen, Enter followed an echo-then-quiet settle. |
| `deferred` | 202 | Ingest seen, but the agent is still working. No Enter yet; a bounded background watcher holds the submit. |
| `unverified` | 200 | Nothing checked by design: a short payload or a bare submit. |
| `not_ingested` | 502 | Written, never painted back. Treated as not delivered; no Enter sent. |
| `dropped` | 409 | The write never reached the PTY. |

`delivered` is `true` only when every write landed and either the submit was sent or none was asked for. `submit_state` names the submit's fate (`confirmed`, `unconfirmed`, `pending`, `not_submitted`, `not_requested`). `submit_confirmed` and `ingested` are tri-state: `null` means *not established*, never folded into the passing state. The same verdict is recorded on the session as `last_input`, and every non-delivery leaves an info breadcrumb in the session-host log.

**Deferred submit.** The watcher writes one bare Enter, never a resend of the text, and only when there is a real quiet window (1.5 s within a 30 min cap), the payload it pasted is still in the composer, and no dialog is on screen. Any check failing writes nothing and the message stays stranded and honestly reported (`defer_timeout`, `defer_vanished`, `defer_unclear`, `defer_failed`). A newer `/input` call or a stop supersedes the watcher. Callers treat anything other than `reason: "ok"` as unconfirmed and verify independently. Whether Copilot queues a message typed mid-turn (type-ahead) is set per agent in `src/agents.py`.

**Pending messages.** Each session row carries `pending_input`, the messages accepted through the route that the agent has not taken yet, each with a `state` (`in_composer`, `queued`, `discarding`); `DELETE /api/claude-code/sessions/{sid}/pending-input/{id}` discards one. A detached session reports `null`, not tracked.

**One-tap issue start.** A Backlog card whose project is in the projects folder carries **Start** and **YOLO**: `POST /api/board/issues/start`, body `{repo, number, mode, model, rows, cols}`. It launches a Copilot session in that folder and types `/issue-start <N>` or `/issue-yolo <N>` (skills shipped by fleet-config-lite, using `glab`). The model comes from the `copilot_models` list through the same flag builder as every launch (`src/launching/flags.py::build_copilot_flags`).

**`?board=<sid>` deep link** (`board.js::openBoardCard`): activates the Board tab, finds the column holding that session, opens its drawer and scrolls the card into view. If the session is already gone it toasts and leaves the board browsable.

## Refresh / cache contract

GitLab data is fetched **server-side via `glab`** into a lock-guarded module-level cache in `src/clients/gitlab.py`. `snapshot()` is the pure in-memory read the 5 s poll hits for free; `refresh(group, host)` runs the reads and replaces the cache. On failure the previous data is kept and only `error` is set, so a flaky `glab` degrades to a badge, not an empty board. That holds for every failure, including a malformed row the normalisers choke on.

**Never fetched is not empty.** The cache is process memory, so every webapp restart starts it with `fetched_at: null` and empty lists. `GET /api/board` reports that as `github.available: false`, and the Board renders it as unknown: Backlog and Done show `—` instead of a count and a "not loaded" message, and Other keeps its real job count but says open MRs are not loaded. A refresh that genuinely finds nothing gives `available: true` and a plain `0`.

| `available` | `error` | Meaning |
| --- | --- | --- |
| `true` | `null` | Fresh data; an empty list is a real zero. |
| `true` | set | The last refresh failed; the lists are the older good data. |
| `false` | `null` | Never fetched in this process: unknown. |
| `false` | set | Never fetched successfully; the first attempt failed: unknown. |

An empty `gitlab_group` keeps the panel dormant with a Settings hint; `glab` missing from `PATH` or not signed in surfaces as the same `error` badge.

The cache is refreshed **only** by the manual **Refresh** button, by **tab activation** when the cache is stale (never fetched, or older than two minutes), and by the Board's **own timer**: from a visible, foreground page on the Board tab, a poll refreshes a cache that was never filled or is older than five minutes (gate in `board/github-refresh.js`). A hidden page never refreshes, at most one attempt runs per age so a failing `glab` cannot loop, and the failure stays quiet (the Backlog footer keeps showing the cache's age). A poll of a fresh cache never runs `glab`. This mirrors the Coding tab's git-status on-demand contract.

## Security boundary

The **read-only board** (`GET /api/board`, the refresh) is bearer-token gated and reachable over a public tunnel: it exposes only issue, MR and session metadata. The **terminal-grade surfaces** (the drill-down exchange, the drawer reply, issue filing and one-tap start, the proof report and the chief routes) are **private-network-only and passkey-gated**: refused over a public tunnel entirely, and on a trusted network they additionally require a valid WebAuthn terminal token, exactly like the live terminal. No token is ever stored in the repo: `glab` keeps its own credentials, set by `glab auth login`.

## Verification

The pre-ship gate (`C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -File scripts/verify-before-ship.ps1`) covers the Board through `tests/unit/board/` (column assembly, the cwd join, the transcript overlay, the GitLab cache and its pinned fixtures), `tests/api/board/` (the exchange read and reply path), `tests/api/chief/` and `tests/e2e/board/` (the kanban, drill-down, reply and one-tap start in a real browser). All `glab` and session-host calls are mocked at the client seams, so a passing suite proves the code against GitLab's documented shapes, not against a live instance.
