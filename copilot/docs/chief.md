# The lite fleet chief: helpers

app-launcher-lite carries the launcher side of the fleet chief (the plan and journal cards, the mailbox, the doorbell, the dispatch brief). This repo carries the helper side: the code the chief session runs and the state files the launcher reads. Every state file keeps the full `fleet-config` format byte for byte, so app-launcher-lite's readers parse it unchanged; only the location moves, from `~/.claude/hooks/state/` to `~/.copilot/hooks/state/`.

## The chief skill

`.github/skills/chief/SKILL.md` is the chief's brain. app-launcher-lite spawns the chief session with this checkout as its working directory and types `/chief`. Copilot discovers the skill as a project skill from `.github/skills/`, so it loads only in sessions started here and is not one of the skills `install.ps1` junctions into `~/.copilot/skills/`.

It is written for any model behind Copilot. It gives one table from what woke the chief (a message, the doorbell, a mailbox event kind, the Board's answers) to the verbs to run, plus the rails `dispatch` enforces and those it cannot. It also sets two habits: decide only from `chief_ops.py`'s `KEY=value` lines, with a missing line counted as unknown, and end every turn with a standalone summary and no in-turn waiting, because the only wake a Copilot chief has is Roberto's next message. It names only verbs this checkout carries.

## Entry point

The chief calls one CLI, `skills/_lib/chief_ops.py`, and acts only on the `KEY=value` lines it prints to stdout. Breadcrumbs go to stderr. A refused write exits 2 with `ERROR: ...` on stderr and leaves the state file as it was.

```powershell
.venv\Scripts\python.exe skills\_lib\chief_ops.py plan show
.venv\Scripts\python.exe skills\_lib\chief_ops.py plan add <repo>#<N> "<title>" [--status S] [--model M] [--kind K]
.venv\Scripts\python.exe skills\_lib\chief_ops.py plan lane <repo> building|gate|idle|waiting [--item "#N"]
.venv\Scripts\python.exe skills\_lib\chief_ops.py plan ask "<question>" --ref <repo>#<N> --option "Label::why" ...
.venv\Scripts\python.exe skills\_lib\chief_ops.py plan prune             # drop merged rows
.venv\Scripts\python.exe skills\_lib\chief_ops.py note "<one line>" [--kind K] [--ref <repo>#<N>]
.venv\Scripts\python.exe skills\_lib\chief_ops.py inbox [--count]     # claim pending mailbox events
.venv\Scripts\python.exe skills\_lib\chief_ops.py ack <id> [<id> ...]  # retire handled events
.venv\Scripts\python.exe skills\_lib\chief_managed.py mark <sid> <repo> <number>
.venv\Scripts\python.exe skills\_lib\chief_ops.py board | sessions | chief-sid
.venv\Scripts\python.exe skills\_lib\chief_ops.py dispatch <repo> <N> [--mode start|yolo] [--model M] [--brief-file F]
.venv\Scripts\python.exe skills\_lib\chief_ops.py open <project> [--message-file F]
.venv\Scripts\python.exe skills\_lib\chief_ops.py file <repo> --title T --body-file F
.venv\Scripts\python.exe skills\_lib\chief_ops.py say <sid> --file brief.md --verify
.venv\Scripts\python.exe skills\_lib\chief_ops.py exchange <sid> | stop <sid> [--kill]
.venv\Scripts\python.exe skills\_lib\chief_ops.py issues <repo>#<N> ...
.venv\Scripts\python.exe skills\_lib\chief_ops.py verify <repo> --expect mr-open|merged [--branch B]
```

Launcher calls go to app-launcher-lite on the webapp port its own `config/config.json` sets (`webapp.port`, default 8465; read by `skills/_lib/lite_ports.py` from app-launcher-lite's own `config/config.json`, the checkout `copilot/` sits in, or from the file `FLEET_LITE_CONFIG` names, so a Lite running beside another launcher is the one addressed; `--base-url` overrides; any non-loopback host is refused). A launcher that stays silent prints `UNCONFIRMED=...` and one that refuses the connection prints `UNREACHABLE=...`; neither is folded into a verdict. Forge reads go through `glab` against `<namespace>/<repo>` on GitLab. The namespace is app-launcher-lite's `gitlab_group` (`GET /api/config` on loopback); when that is empty, the checkout's own `origin` remote names it, and `--owner` overrides both on `issues`, `verify` and `merge`. `dispatch`'s owner check, `drain` and `sweep` resolve it the same way. The host works like the namespace: `gitlab_host` from the launcher, else the host of the checkout's `origin` (a GitHub remote does not count), else gitlab.com. Every chief and owner-check `glab` call carries it as `GITLAB_HOST`, because `glab` sends a bare `<namespace>/<repo>` to gitlab.com, which answers a self-hosted project with a 404. A `glab` failure on `issues`, `verify`, `merge` or an owner-check read ends with `(tried host <host>, project <path>)`, so a wrong host or group reads off one line. The token stays in `glab`'s own store (`glab auth status` shows it per host); the chief never reads, stores or logs one. When no source names one, the read is `OWNER=unknown` (`issues` exits 1, `verify` prints `STATUS=UNKNOWN`, `merge` prints `MERGE=unknown`), never a 404 that reads as "no such issue". When `glab` is missing or signed out, `issues` prints an `error:` row for the ref and `verify` prints `STATUS=UNKNOWN`.

`dispatch` keeps fleet-config's rails verbatim (`chief/rails.py`). It refuses before the POST if the repo is occupied, the worker cap is reached, a yolo is unconfirmed, the issue carries `needs-owner`, or a fleet-wide brief would run while a fleet-wide Job is running. A spawn the launcher may still be running prints `DISPATCH=unconfirmed` and must not be retried. `--model` is a Copilot model id, sent to the launcher as typed.

`open <project> [--message-file F]` starts a plain Copilot session in a Coding-tab project through `POST /api/apps/<id>/launch`, with no issue involved. It refuses an unknown project, an occupied one and a full worker cap before the POST, as `dispatch` does. It is not lane work: the session is never marked chief-managed, never enters the plan, and is left running (`stop` ends it). A launch that fails server-side the same way `dispatch` can prints `OPEN=unconfirmed` and must not be retried. The launch route takes no prompt, so the message goes in afterwards, once. A message sent while Copilot is still drawing its TUI is dropped although the launcher answers `delivered`, so `open` waits `--settle` seconds (default 10) after the spawn, posts once, and then reads the session transcript for the message as a user turn. Only that prints `OPENED session=<sid>` and `DELIVERED`. A transcript that stays empty prints `STRANDED`, an unreadable one `UNKNOWN`, each with `SESSION_LIVE session=<sid>` and exit 1: the session runs without its message, and resending or stopping is the caller's call. The transcript, not `say --verify`, is the evidence because the Copilot exchange read is not usable yet.

`file <repo> --title T --body-file F` files one GitLab issue through the launcher's `POST /api/board/issues/file`, the route the Board's file button uses, so the chief files without any `glab` write of its own. The body comes from a file and the route takes no labels, so a filed issue has none and its lane's `issue-start` treats it as plan mode. It prints `FILED=<repo>#<N>` and `URL=<issue url>`; `REFUSED=<reason>` (exit 1) for an empty title or body, an unreadable body file or any 4xx the launcher answers (unknown project, over-long title or body); and `UNCONFIRMED=...` (exit 1) for a timeout, a 5xx or a reply with no issue number, because the issue may exist and a retry can file a duplicate. A launcher that refuses the connection prints `UNREACHABLE=...`. The launcher appends its own footer to an issue filed with the delegate token.

`verify` is lite's own: the lane ships through a GitLab merge request, so `--expect mr-open` means a clean checkout plus an open merge request from the branch, and `--expect merged` means a clean checkout back on the default branch plus a merged one. It prints the same `STATUS=CLEAN|DIRTY|UNKNOWN`, `BRANCH=` and `REASON=` lines as upstream.

| Subcommand | Writes | Read by (app-launcher-lite) |
|---|---|---|
| `plan ...` | `chief-plan.json`, format v1; each transition also journalled | `src/chief/plan.py`, `src/chief/queue_status.py`; `src/chief/plan_prune.py` loads `chief_plan.py` by path and calls `prune_plan_file` |
| `note ...` | `chief-journal.jsonl`, one JSON line per event, beside the plan file | `src/chief/journal.py` |
| `inbox`, `ack` | claims events into `chief-inbox/.claimed/`, deletes them on ack | the chief itself; the launcher publishes `session-end`, `job-failed`, `cap-raised` and other events into the same directory |
| `chief_managed.py mark` | `chief-managed.json`, one row per chief-dispatched launcher session | called by the launcher's `POST /api/board/issues/start` through `<this repo>/.venv/Scripts/python.exe`; read by `hooks/chief_inbox.py` and the launcher's reaper (`src/chief_session_end.py`) |

The state directory is `~/.copilot/hooks/state/`, or `COPILOT_HOOKS_STATE_DIR` when set (the same override `hooks/session_state.py` honours). `skills/_lib/fleet_core/hooks_state.py` is the one place that resolves it.

## The mailbox

A worker the chief dispatched has its launcher session id in `chief-managed.json`. On that worker's `agentStop` and `sessionEnd`, `hooks/chief_inbox.py` (called from `hooks/session_state.py` on the same hook invocation) publishes one event file into `chief-inbox/`, in fleet-config's mailbox v2 format: `kind` `turn-end` or `session-end`, `event` `Stop` or `SessionEnd`, the registry row's `repo`, `number` and `dispatch_id`. Every other session returns before any I/O. A second `turn-end` from one session within 45 s is not written. An unreadable registry writes nothing and logs that the state was undetermined.

Copilot's `agentStop` payload carries no reply text, only `transcriptPath`, the session's `events.jsonl`. The excerpt is therefore the newest `assistant.message` content in the last 256 KiB of that file (`excerpt_source: transcript`, at most 1,500 characters). The `busy` flag upstream sets from the process table is never written here, which the contract allows: an absent flag means unknown.

Not carried: upstream skips a nested print-mode child (a `claude -p` a lane starts) that inherits the lane's `APP_LAUNCHER_SESSION_ID`. Copilot exposes no entrypoint marker for that, so a `copilot -p` run from inside a chief-managed lane still writes events as the lane. The chief dedupes by `id` and reads the exchange, so the cost is a spurious wake.

## Merge path

`chief_ops.py mr-create <N> [--timeout S]` opens the current branch's merge request for the lite `issue-finish` skill, which `git push`es first. It runs `glab mr create --fill --yes --push=false` with stdin closed and a kill timeout (60 s by default), so glab can never wait on a confirmation nobody reads and leave an orphaned process holding the lane's worktree folder. It prints `MR=created url=<merge request url>` (exit 0), or exits 1 on `MR=failed reason=<glab's first message line>`, `MR=timeout reason=...` (glab gave no answer and was killed, so the request may or may not exist: read it with `glab mr view` before retrying) or `MR=unknown reason=glab not installed`. A clean exit with no request URL is `failed`, never `created`.

`chief_ops.py merge <repo>#<N> --branch <branch> [--ask]` asks GitLab to merge the lane's merge request as soon as its pipeline passes (`glab mr merge --auto-merge --squash --remove-source-branch --yes`), then reads the request back with `glab mr view`. GitLab's state, not the command's exit code, is the verdict: `MERGE=merged`, `MERGE=queued` (auto-merge set, the pipeline still running), `MERGE=refused reason=<GitLab's first message line>` or `MERGE=unknown` when glab is missing, signed out or unreadable. With `--ask`, a refusal becomes a question on the chief's plan, id `merge-<repo>-<N>`, reused on a repeat refusal and journalled like any `plan ask`. So a merge GitLab refuses reaches Roberto's Board instead of stopping silently in a lane. The lite `issue-finish` skill merges through this verb and passes `--ask` in chief-dispatched sessions. glab boxes its failures under a bare `ERROR` banner, which the reason skips, and when its message is only a retry lead-in (a 405 right after another merge) the reason gets the request's own `detailed_merge_status` appended, such as `conflict`. A merge and a conflict refusal, each with its `--ask` question, ran live on a GitLab test project.

## Worktrees, claims and sweep

Two sessions on one repo collide in the study phase, long before either cuts a branch. The lite issue skills therefore claim the repo first, with `skills/_lib/worktree_claim.py`, a byte-identical port. The first session owns the primary checkout (`MODE=primary`). Every later one, and every launcher or chief lane (`--force-worktree` when `APP_LAUNCHER_SESSION_ID` is set), builds in a sibling worktree `<repo>-wt-<N>` with the primary's `.venv` junctioned in. The claim lives in the repo's git common dir in the full fleet's format, so the full and lite helpers see each other's claims on a shared checkout. `issue-finish` removes the worktree (junction first) and lands the primary (`land-primary`, one `PRIMARY=` line), or, in the primary, runs `assert-owner`, the untrack-guarded fast-forward and `release`. `land-primary`'s service guard reads the full fleet's machine-local projects table, which lite does not carry, so it reports that it could not read the declaration instead of claiming a service is down.

`chief_ops.py sweep [--apply]` classifies finished lane worktrees with the ported `chief/chief_sweep.py`. A worktree is finished once a merged request covers its HEAD. The lookup dispatches on the remote's host: `glab mr list` for GitLab, normalised to the `gh pr list` rows the scan reads, and the ported `gh` lookup for GitHub. The sweep folder is the parent of this checkout, which on a full-fleet machine also holds that fleet's worktrees. So lite keeps only worktrees of lite repos (named in lite's own registry or active-issue markers, or named `*-lite`). Every other worktree is listed as `OUT_OF_SCOPE=` and never removed.

## Owner checks and drain

`skills/_lib/owner_check/` is fleet-config's owner-check package. `cli.py`, `block.py`, `rule.py`, `backfill.py`, `reclassify.py` and `repair.py` are byte-identical ports. The forge seam `github.py` keeps upstream's module name and every function they call, but answers through `glab` against GitLab: issue view, notes and closing merge requests via `glab api`, labels via `glab issue update`, and merge request files via the `diffs` API. A failed call raises `GhError`, which every caller reports as `unknown`. The launcher's Board (`cli.py list --json`, `clear`), lite `issue-start`'s gate (`cli.py gate <repo>#<N>`), `dispatch`'s owner rail and `plan add --kind owner` all read it.

Labels are plain `needs-owner` and `needs-validation` by default, which work on any GitLab tier. Opting in with `[owner_check] label_scope = "<scope>"` in this repo's `.fleet.toml` writes GitLab scoped labels instead (`<scope>::needs-owner`, one of a kind per issue). Reads map them back to the plain name, so every reader's `"needs-owner" in labels` check is unchanged. The fleet is every sibling checkout with a GitLab remote. GitLab numbers merge requests apart from issues, so a "PR" in the package's vocabulary is a merge request iid.

`chief_ops.py drain [--dry-run]` is fleet-config's `chief/drain.py` with four seams changed. It scans the open issues of the lite GitLab fleet through `glab`, gets the audit-managed marker from `issue_host`, has no machine-local projects table (so no repo is switched off from auto-ship), and dispatches through the lite rails. Its generic brief is `chief/drain_brief.md`, which is lite's own. With Settings' `auto_drain` on, it queues open audit issues that need no owner step and dispatches queued rows into free worker slots in plan order. `chief_ops.py slots-idle` is drain's idle-slot line (`SLOTS_IDLE=<n|unknown|off> reason=...`), which the guard hook reads; `off` and `0` never hold.

## Guard hooks

`hooks/chief_guard.py` is registered for four Copilot events through `hooks/chief-guard.template.json`, which `install.ps1` renders next to the session-state config. Each behaviour was verified live by `scripts/copilot_probe.py`: on CLI 1.0.112, `sessionStart` `additionalContext` reaches the model, `agentStop` `decision: block` forces another turn, and `preToolUse` `permissionDecision: deny` stops the tool call; on CLI 1.0.114, `userPromptSubmitted` `modifiedPrompt` replaces an interactive session's expanded skill (manual item `prompt_hook_replaces_skill`). Each `sessionStart` and `preToolUse` answer is emitted in both Copilot's own fields and the Claude Code `hookSpecificOutput` shape Copilot also reads.

- **`sessionStart`**, chief only: the handover log `chief-handover.md` comes back as `additionalContext` (its last 24,000 characters), so a boot, a resume or a compaction never starts without the chief's intent. No log means a one-line note to start one.
- **`agentStop`**, chief only, and never while Copilot is already continuing after a hold (`stop_hook_active`). It holds the turn end with a reason naming the next command, in this order, each at most once per 10 minutes: `compact-due` prints `COMPACT_DUE percent=..` (not when `self-compact` already queued one); `slots-idle` reports free worker slots while plan rows wait as `queued` and no `plan ask --id slots-idle` covers it. It never asks Roberto for the launcher's doorbell: that switch is the owner's Setting in the launcher, not a request the chief makes (below). An unknown reading never holds.
- **`preToolUse`**: `ask_user` is denied for the chief and for every chief-managed lane, because it renders only in that session's terminal. A chief-managed lane's shell call (`powershell`, `bash`) is also denied when it starts a `gh` command, names `notify_complete`, or runs a helper under a Claude Code user-scope `skills/` folder or the full fleet's checkout `skills/` folder. The reason names this checkout's `skills/` so the lane retries with lite's helper. On a PC that also runs the full fleet, that fleet's own `preToolUse` hook has already rewritten the command to a `context_filter_cli.py ... --encoded <base64>` wrapper by the time this guard runs, so the guard judges the decoded command, and the wrapper's own path (in that fleet's `hooks/` folder) is not a match. Every other tool, and every other session, passes untouched.
- **`userPromptSubmitted`**, any session app-launcher-lite's session-host lists: a prompt that invokes a skill this checkout also ships, expanded from another folder, comes back as `modifiedPrompt` with that skill block holding this checkout's `SKILL.md` (next section). A prompt whose skill already comes from this checkout, a skill lite does not ship, and a session the host does not list pass untouched.

## Lanes on a shared machine

The launcher types `/issue-start <N>` into a lane, and Copilot resolves it to the user-scope skill. On a PC that also runs the full fleet, `install.ps1` leaves `~/.copilot/skills` alone, so that skill is the full fleet's GitHub one: it would run `gh` and its own completion ping. The ordering Copilot applies, probed on CLI 1.0.114 with `copilot skill list --json` and a live run: project skills (`.github/skills`, also those of a `--add-dir` folder) beat personal skills, and personal skills beat folders added through `COPILOT_SKILLS_DIRS` or `copilot skill add`, which are dropped on a name clash. A lane's working directory is the repo it works on, not this checkout, so lite's skills cannot arrive as project skills without a launcher flag.

`dispatch` therefore always opens the brief it sends with `lane_preamble` (`skills/_lib/chief/lite_lanes.py`): the paths of this checkout's `issue-start` or `issue-yolo` and `issue-finish`, the instruction to use `glab` and the helpers under this checkout's `skills/_lib`, and the statement that the issue's repository is the session's working directory. A brief the chief supplies follows the preamble. The preamble is not a fleet-wide brief for the rails. Text a lane reads after it starts does not settle which skill it follows: a model that rightly trusts the skill it was invoked with over a later redirect stops instead of switching. So the swap happens in the prompt itself. Copilot hands `userPromptSubmitted` an interactive skill invocation already expanded (`<skill-context name="issue-start">`, `Base directory for this skill: <dir>`, the body, `ARGUMENTS: <args>`), and the guard's `modifiedPrompt` replaces that block with the same shape built from this checkout's `skills/<name>/SKILL.md` before the first model turn. The session's `skill.invoked` event still names the folder the CLI resolved; the lane's first user message carries lite's base directory and body. This covers every session app-launcher-lite's session-host lists, a Board start included, because those sessions work on lite's GitLab repos. The `preToolUse` deny stays as the backstop for anything that reaches the full fleet's helpers anyway.

The chief is the launcher session whose working directory is the chief's folder and whose id `chief_ops.py chief-sid` names. The chief's folder is Lite's `fleet_config_dir` setting, read from `config/webapp_config.json`, or this `copilot/` folder when the setting is empty or the file is missing. Every session elsewhere is allowed before any subprocess runs. A fact the guard cannot read allows the action and is logged to stderr.

`chief_ops.py wake` prints `WAKE=doorbell ...` or `WAKE=waiter doorbell=off|unknown` from the launcher's `chief_doorbell` switch (`chief/mailbox.py`'s `resolve_wake_mode`, verbatim). The lite chief has no background waiter, because a background shell waking the agent is still an unknown probe item. So with the doorbell off only Roberto's own messages wake it, and `inbox` is read at the start of every turn it gets.

The doorbell can ring a Copilot chief, with the owner's Settings > Chief > Doorbell switch on. app-launcher-lite's session host tracks Copilot's composer from the keys probed on CLI 1.0.114 and reports `composer_dirty` as `True`/`False`, or `None` when it cannot tell; the bell rings only on `False`. Against the live chief, idle with pending mailbox events, a ring was seen 3 seconds after the debounce, and the chief started a turn and claimed the event. A chief started or restarted through the launcher had a composer left unknown by its own `/chief` until the first ordinary message; the launcher no longer does that, once its session host runs that build. A person's half-typed `/cmd` or `#` mention still reads unknown, and the bell holds off. The item `composer_keys_doorbell` stays `unknown` in this repo's probe results until the owner runs `copilot_probe.py record` for it.

`chief_ops.py slots-idle` prints `SLOTS_IDLE=<n> free=<f> queued=<q>`, or `unknown` with a reason, plus `SLOTS_COVERED=slots-idle` when that question is open.

## Compaction

`chief_ops.py context [<sid>]` reads a session's context-window use where Copilot records it: the newest `session.usage_record` in the session's `events.jsonl`, as `round(100 * inputTokens / maxPromptTokens)`. The launcher session id is joined to Copilot's session through the `sessions-state.json` row the hook writes (`launcher_session_id`, newest `updated_at`), and the log is that row's `transcript_path`, else `~/.copilot/session-state/<id>/events.jsonl`. app-launcher-lite's own `/context` endpoint is not used, because it reads a statusline Copilot does not print. If any step can't be read (no row, no log, no usage record, a missing field), the reading is `percent=unknown` with the reason, never 0.

`compact-due` compares the chief's reading with the Settings threshold (`/api/board/chief-settings`, `auto_compact_threshold`; 0 means off) and prints `COMPACT_DUE`, `COMPACT_NOT_DUE`, `COMPACT_OFF` or `COMPACT_DUE=unknown`. `inbox` prints the same line when the caller is the live chief and the context is due or unknown. `self-compact` (the chief) and `lane-compact` (a dispatched lane) post `/compact <focus>` to the caller's own session behind `chief/compact.py`'s refusals. `self-compact` refuses on an unknown percentage, below the threshold, or when the handover log (`chief-handover.md`) or the plan is older than 10 minutes. `lane-compact` refuses without a fresh `lane-handoffs/<sid>.md`, or while a gate runs in the lane's tree. That gate state is the in-flight marker `gate_evidence.py run` writes, which is why the lite issue skills now run their gate through it. Copilot queues a slash command typed mid-turn and runs it at turn end; that is the probe's manual item `compact_queued_midturn`, unknown until recorded.

## Port provenance

These files are byte-identical copies of `fleet-config`'s `skills/_lib/` at commit `173b70a`, re-vendored by replacing the file whole, never by editing it here. `tests/test_chief_port.py` fails when one drifts from its hash. The hashes are of the LF bytes; a CRLF checkout is normalised before hashing.

| File | sha256 |
|---|---|
| `skills/_lib/chief_plan.py` | `24dd104adc2fd3e51dee7d5bdbdb780bd9284753cfc4ec2b79f13422946fec84` |
| `skills/_lib/chief/plan_cli.py` | `e6371d1e67093cd6edf623a41ba46db14644d5c0cc6218de4ffd3d058a8a65e0` |
| `skills/_lib/chief/journal.py` | `cada1e732a0b4f5213af2c336141771a3bf493a4f52185bc6775bfe4d3125c40` |
| `skills/_lib/chief/__init__.py` | `4374263a66818558e16a1b08fda0ae11ec96fe28e18315df5502f4298d2b3e16` |
| `skills/_lib/active_issue.py` | `4ca4b864e1cf95bdd73154566266ba3ad8739663a4f4448486cf928c937990d7` |
| `skills/_lib/git_run.py` | `22c69832c10b3321553394db35964a119dbeb1a2dc6008b1d24d8a8c25efba04` |
| `skills/_lib/worktree_residue.py` | `08243734d28a8d5957f2d57417facc2274a07484dc213f24918a0c2c4e36a144` |
| `skills/_lib/no_window.py` | `a2a46223f213908fbfad75e9544f136b7c8a08ec5a5f274f62a885c796e998d7` |
| `skills/_lib/fleet_core/__init__.py` | `19f21ca16675ba975cd8aaa8f3ca19a575d4f0057593bf1efe5528ad8e8d820c` |
| `skills/_lib/fleet_core/runtime_files.py` | `b25bd8e88f69993cb83a205b37362eb189dba22bc2321d04f34809a7bf52c7b6` |
| `skills/_lib/chief_managed.py` | `225b2fa8b39786916ae3cd0eca0e1cf983191bc07bd9db4f6f513031294f2dc2` |
| `skills/_lib/chief/mailbox.py` | `50c0498fb8edb3f2b99bd92bf328fa6ca377b11e98659804dbfd7e8fe2129a79` |
| `skills/_lib/chief/digest.py` | `152d2db0e73381b17ac77905376ba6d1b197f7f322625874e28182ef0addfd29` |
| `skills/_lib/chief/rails.py` | `8608a2ff8b7f4dbc00224fbaa943d165b2cd8a38e04d725f98c1d1ab3f5638bf` |
| `skills/_lib/owner_check/block.py` | `bf896b68ba15c6cab78b51e60cb9b5e778130ae87bfbbbffbad501642074e198` |
| `skills/_lib/owner_check/__init__.py` | `5b5d28ef222565346788384d0a32b6dbf7dd406fd8f7d010eda35aca4ac0e3de` |
| `skills/_lib/owner_check/cli.py` | `22582e7a1899d4c745e5aa49ffd1f2dd0964d6467bc2493fc74d2408f940a141` |
| `skills/_lib/owner_check/rule.py` | `2afbf5256e706e0a0626f63906f49007deda82539886d2085e6ad8ca3d2095b4` |
| `skills/_lib/owner_check/backfill.py` | `4ca958f116bc38b166d37f7449645e32edff58fca868764958f151479e7756f7` |
| `skills/_lib/owner_check/reclassify.py` | `95dc261cc8ac241a21c02202a2e32cc9e478c2a990460b5dfc7836cfe3223211` |
| `skills/_lib/owner_check/repair.py` | `f1a536abd71bfa76278ce3c3023d076422f92870efa52c676c26c39a786c7636` |
| `skills/_lib/fleet_core/fleet_repo_scan.py` | `038aa50e37a105e07dfb4e360d86702804e2ec009cc5b8a6e292416c7b5b656f` |
| `skills/_lib/chief/steer_delivery.py` | `164f17148fbcc86f4c9ae776f189c4d2bb18c439a85e9d8428f4b8243e7e5dae` |
| `skills/_lib/chief/compact.py` | `beaf990766b03bffd377797d4c37f2ea3d33e57d9e8a62f252bb501c8c69991a` |
| `skills/_lib/gate_evidence.py` | `af2e9ae0fd22973e5b6e41c2a120033f96d342e3e518ad8de1f3f61efe9b504f` |
| `skills/_lib/fleet_core/utf8_stdio.py` | `563f14f98d11b4b31f218d98066e532a0930aef53e8cb8a71f6affef2deacae5` |
| `skills/_lib/worktree_claim.py` | `4c97002155a9783b19922ca668f8a392185fb4a5ebc35c8db62de2577aaab11a` |
| `skills/_lib/untrack_guard.py` | `e06d92da6bf50534bf8428c5592945aff008db666756d8e62c481ef069a85442` |
| `skills/_lib/fleet_core/fleet_toml.py` | `29d8238944908b80a61e359878fa88723d0dcc33181a73baaa5fd6f6f3f93138` |
| `skills/_lib/chief/chief_sweep.py` | `9e4b7a8a63038ab89f01f74ed37631f20de161fac038fd039970a5138dbba702` |
| `skills/_lib/worktree/__init__.py` | `e00f5a72fb2d58440af5bc615579f3857870b53b6f93e1cd0c1c70c44f7bd51c` |
| `skills/_lib/worktree/claim.py` | `4e3f157759f1fb6dc066bb46a39797c1a2030d14445dd3eba3dacc94a3ed82cc` |
| `skills/_lib/worktree/gitops.py` | `b15134fdb4c3b3bb8ce3f615dc9dd0d82a9292d990eb96958d889d5dec286983` |
| `skills/_lib/worktree/service_probe.py` | `30d82159a90f1cb2979fbfa5bde3442526ecb103f63dc1b2a347622d279b66f5` |
| `skills/_lib/worktree/worktree_config.py` | `14e88f205b1685bf7c2dc4cf544f969a0cdfdb5ff88dd610b0e2ba3ec2ae7354` |

Lite-specific, not copies: `skills/_lib/chief_ops.py`, which mounts only the subcommands this tree carries; `skills/_lib/chief/lite_commands.py`, `lite_lanes.py`, `lite_verify.py`, `lite_compact.py`, `lite_merge.py`, `lite_sweep.py` and `copilot_context.py`, its handlers (`lite_lanes.py` keeps upstream's `chief/commands.py` bodies; only the grokbot line, the Codex catalog and the Telegram escalation are gone); `skills/_lib/chief/launcher.py`, upstream's copy with three seams changed: the lite base URL, `glab` for issue state, and no Codex catalog; `skills/_lib/owner_check/github.py`, the GitLab forge seam; `skills/_lib/chief/drain.py` and `drain_brief.md`, drain with its four named seams; `hooks/chief_inbox.py`, the mailbox writer for Copilot's payloads; and `skills/_lib/fleet_core/hooks_state.py`, which differs from upstream only in the Copilot state directory and its override variable. The ported docstrings still name `~/.claude/hooks/state/` and `CLAUDE_HOOKS_STATE_DIR`. Behaviour follows `hooks_state.py`, not that wording.
