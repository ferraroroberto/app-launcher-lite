# Jobs tab — reference

The launcher's remote-fireable layer for scripts and scheduled jobs. A job is defined once in `config/jobs.json`; every trigger (phone tap, Stream Deck button, Task Scheduler slot, webhook, chain) funnels through one executor and produces a uniform run record with status, duration and output log.

The Apps tab launches long-running services and the Coding tab launches Copilot sessions; neither fits a one-shot script that runs, exits and needs a "did it work?" record. Jobs is that surface.

## Architecture

```
   phone tap  --+
 Stream Deck  --+--> POST /api/jobs/<id>/run --+
   webhook    --+                              +--> launcher.py run-job <id>
   schedule   --+ (Task Scheduler) -------------+         |
                                                          +-- resolve interpreter
                                                          +-- capture output + exit code
                                                          +-- write run record
                                                                     |
                                              webapp/jobs/<id>/<run>/  <-- history
```

The single executor is `app/cli/commands/run_job_cmd.py` (`launcher.py run-job <id>`). Task Scheduler calls it directly; the webapp's `POST /api/jobs/<id>/run` pre-creates the run directory, returns the `run_id` at once, then spawns the executor detached so the request never blocks.

## Data model — `config/jobs.json`

Gitignored; the committed template is `config/jobs.sample.json`. It is separate from `apps.json` because the shape differs (schedule, run lifecycle). Add and edit jobs through the Jobs tab or `POST`/`PUT /api/jobs`, which validate and re-sync Task Scheduler; a hand edit skips both.

```json
{
  "jobs": [
    {
      "id": "reporting-daily",
      "name": "Daily Reporting",
      "script_path": "E:\\automation\\reporting\\run_report.bat",
      "args": "auto",
      "schedule": { "type": "daily", "at": "06:00" }
    },
    {
      "id": "scrape",
      "name": "Scrape",
      "script_path": "E:\\automation\\scraper\\scrape.py",
      "schedule": { "type": "daily_times", "at": ["06:00", "12:00", "18:00"] }
    }
  ]
}
```

### Job kinds

`src/jobs_kinds/` holds one module per kind, each with a `validate()` (save-time pre-flight) and a `build_argv()` (the invocation). `kind` is optional: absent, it is inferred from `script_path`'s suffix (`.py` → `python`, `.bat` → `batch`). Every other kind needs an explicit `kind`.

| `kind` | `script_path` | `kind_config` | How it runs | cwd |
| --- | --- | --- | --- | --- |
| `python` | required, `.py` | — | `<venv>/python.exe <script> <args>`, `PYTHONPATH = <project root>` | project root (the dir holding the resolved `.venv`) |
| `batch` | required, `.bat` | — | `cmd.exe /c <script> <args>` | `script_path.parent` |
| `powershell` | required, `.ps1` | — | through `src/launching/ps_script.py`, which never overrides the execution policy: `powershell.exe -NoProfile -NonInteractive -File <script> <args>` where the policy lets a local file run (`Bypass`, `Unrestricted`, `RemoteSigned`), otherwise (`AllSigned`, `Restricted`, or a policy that could not be read) the script is loaded as text and run as a ScriptBlock, its path and arguments passed in the child's environment. Under the ScriptBlock launch `$PSScriptRoot` is the script's folder, but `$PSCommandPath` and `$MyInvocation.MyCommand.Path` are empty | `script_path.parent` |
| `shell-wsl` | required, `.sh` | — | `wsl bash <script> <args>` | `script_path.parent` |
| `inline-shell` | empty | `{script_body, ext}` | body written to `webapp/jobs/<id>/<run>/_inline<ext>`, then run as the kind matching `ext` (`.ps1` / `.bat` / `.sh`) | matches the delegated kind |
| `http-check` | empty | `{url, method?, expect_status?, timeout?}` | `python -m src.jobs_kinds.http_check_probe`, a built-in probe with no external script | project root |

`python`'s venv walk-up (`src.jobs.spawn.resolve_venv_python`) starts at `script_path.parent` and falls back to `sys.executable` when no ancestor `.venv\Scripts\python.exe` exists; setting `PYTHONPATH` lets an out-of-tree script import its project's packages. `shell-wsl` is opt-in: pre-flight warns (does not block) when `wsl.exe` is not on PATH.

`args` is split on whitespace and appended to every kind's argv except `http-check`. An argument containing spaces belongs inside the script or wrapper, not in `args`.

`inline-shell` suits a script too small to deserve a file. The body lands in the run's own directory beside `run.json` and `output.log`, so the exact script that ran is preserved:

```json
{
  "id": "disk-cleanup-inline",
  "name": "Disk cleanup",
  "kind": "inline-shell",
  "kind_config": {
    "script_body": "Get-ChildItem -Path $env:TEMP -Recurse | Remove-Item -Force -ErrorAction SilentlyContinue\n",
    "ext": ".ps1"
  },
  "schedule": { "type": "weekly", "day": "SUN", "at": "03:00" }
}
```

`http-check` polls a URL and succeeds or fails on the status code; `method` defaults to `GET`, `expect_status` to `200`, `timeout` to `10` seconds. The probe's output lands in `output.log` like any script's.

A new kind is a module under `src/jobs_kinds/` implementing the `JobKind` protocol in `base.py` (`name`, `validate(job)`, `build_argv(job, tail, param_env, run_dir)`), registered in the `KINDS` dict of `src/jobs_kinds/__init__.py`. Settings live in the free-form `job.kind_config`; the executor, pre-flight and router need no change.

### Schedule types

A bounded set, no raw cron. A new shape is a code change.

| `type` | Fields | Materialises as |
| --- | --- | --- |
| `none` | — | No Task Scheduler entry (manual, webhook, Stream Deck only) |
| `minutes` | `every: int (>0)` | one task, `/SC MINUTE /MO <every>` |
| `hourly` | `every: int (1..23)` | one task, `/SC HOURLY /MO <every>` |
| `daily` | `at: "HH:MM"` | one task, `/SC DAILY /ST <at>` |
| `daily_times` | `at: ["HH:MM", …]` | **N tasks**, one per time, suffixed `-1`, `-2`, … |
| `weekly` | `day: "MON"…"SUN"`, `at: "HH:MM"` | one task, `/SC WEEKLY /D <day> /ST <at>` |
| `once` | `at: "YYYY-MM-DDTHH:MM"` | one task, `/SC ONCE /SD <YYYY/MM/DD> /ST <HH:MM>`, self-cleaning |

`daily_times` is the only fan-out: "06:00 / 12:00 / 18:00, skip midnight" fits no single preset. The user sees one row; Task Scheduler holds three entries.

**`once`** fires one time and removes itself. `at` is the format `<input type="datetime-local">` emits. The slash date form is the locale-independent input `schtasks` accepts everywhere. When a `once` job fires from Task Scheduler the executor removes the entry and flips the registry's schedule to `none`; a manual fire leaves the schedule intact.

**Pause / resume.** Pause is a state marker: the live `schedule` becomes `none` (so the sync layer deletes the entries) and the original is parked under `paused_schedule`; resume restores it byte for byte. `POST /api/jobs/<id>/pause` and `/resume`; pausing a manual-only job is a `400`, pausing a paused job is a no-op.

### Per-job flags

- **`visible: true`** runs the scheduled task under `python.exe` instead of `pythonw.exe`, so a console window shows when it fires in the logged-on session. The executor spawns the child with a pipe and tees the combined output to `output.log` and the console. The console half is best-effort and lossy (a bounded queue drops chunks when the console stops draining, e.g. a window in mark mode, which would otherwise deadlock the process tree); `output.log` is the record. Only scheduled fires have a console to tee to.
- **`elevated: true`** is for a target that needs admin. `/RL HIGHEST` can only be set by an already-elevated caller and the webapp is never elevated, so the launcher never creates the entry (it still deletes a stale non-elevated one). Register it by hand from an elevated shell and treat it as externally managed:

  ```
  schtasks /Create /F /TN "\AppLauncherLite\<id>" /TR '"<repo>\.venv\Scripts\pythonw.exe" "<repo>\launcher.py" run-job <id>' /SC HOURLY /MO 8 /RL HIGHEST
  ```

  Single-quote the `/TR` value in PowerShell. The job sheet shows a `🔒 external schedule` flag; Run now, pause and resume are refused (`409`), the dry-run check still works.
- **`session_less: true`** opts out of Windows' default `LogonType=InteractiveToken` principal, under which Task Scheduler silently drops a trigger that comes due while nobody is logged on, and `StartWhenAvailable` does not catch it up (that flag covers a powered-off machine, not a signed-out one). An S4U principal needs elevation, so the launcher never creates or deletes the entry; `GET /api/jobs` returns the generated PowerShell as `registration_command` on such a row, to paste into an elevated shell. It is mutually exclusive with `visible` (session 0 has no desktop); setting both is a `400`. The coverage scan reads `Logon Mode` and reports `principal_interactive` when the entry is still interactive-only; an unreadable value is `unknown`. Pause and resume are refused (`409`); Run now still works. The sheet shows `🌙 logged-out`.
- **`cooldown_seconds`** debounces rapid manual fires (`null`/`0` = off, else `1..86400`). The window is measured from the most recent non-skipped run's start, so a rejected fire never slides the window. A manual fire inside it gets `429` with `Retry-After` and a JSON body `{detail: "cooldown", retry_after_seconds, cooldown_seconds}`, and leaves no run directory. A scheduled fire inside it is admitted by the executor itself and recorded as `status: "skipped"`, `note: "cooldown"`; skipped runs are left out of stats and failure streaks. Chain fires and dry runs are exempt.
- **`confirm: true`** requires an explicit confirmation: `POST /api/jobs/<id>/run` returns `403 confirmation required` unless the request carries `?confirmed=1`. The UI asks through the confirm sheet and then adds it. A Stream Deck button on such a job must bake `?confirmed=1` in. A dry-run `check` is exempt; a dry-run `execute` is not.
- **`rerun_safe: true`** is the owner's statement that an unattended re-run is safe. It is read only by the chief's failed-run re-run, which is planned (Jobs feeding the chief is an open port step in the README). A strict JSON boolean; refused together with `confirm` or `elevated`.
- **`max_runtime_seconds` / `no_output_seconds`** tune the watchdog (see "Stopping and reconciling runs").
- **`env`** is a per-job environment overlay (see "Per-job secrets & env").

`visible`, `elevated`, `session_less`, `confirm` and `rerun_safe` round-trip through `POST`/`PUT` and are omitted from the stored row when false. `visible`, `elevated` and `session_less` have no UI checkbox: set them in `config/jobs.json` or through the API.

### DAG chaining

```json
{ "id": "scrape", "name": "Scrape", "script_path": "...", "on_success": ["transform"], "on_failure": ["alert-failure"] }
```

After a run finalises, the executor reloads the registry, picks `on_success` or `on_failure` by `status`, and dispatches each downstream through the same mutex-aware admission path the route uses. The downstream record carries `trigger="chain:<upstream_id>"` and `chained_from`.

- **Cycle guard.** `add_job` and `update_job` run a DFS over `on_success ∪ on_failure` and reject a cycle, naming its path; a self-chain has its own error.
- **Reference guard.** Every entry must name an existing job id, caught at save time.
- **Cascade strip.** Deleting a job removes its id from every other job's chain lists.
- **Mutex groups** apply to chain fires; **cooldown** does not.

### Mutex groups

A job can declare a `mutex_group` (lowercase alnum plus `_`/`-`, starts with a letter, at most 32 characters; "Exclusive group" in the editor). Two jobs in one group never run at the same time: a fire that finds a member `running` or `pending` is **queued**, not rejected, and the head's finalisation starts the next queued entry.

- **Three admission points, one implementation** (`src.jobs.queue.admit_and_spawn`): manual/webhook/API fires at `_admit_and_spawn` in `app/webapp/routers/jobs_run.py`, chain fires at `dispatch_chain_run`, and scheduled fires in the executor (`_finalize_mutex_queue`), because Task Scheduler launches `run-job` without touching the webapp. The executor gate applies only to a scheduled fire with no `--run-id`, so a drained entry is not re-queued, and it runs after the cooldown gate.
- **Queue file.** `webapp/jobs/_queue.json`: group → FIFO list of `{job_id, run_id, trigger, params}`, replaced atomically, empty groups pruned.
- **Queued records** carry `status: "queued"`, `mutex_group` and `mutex_blocked_by`, and are left out of stats and failure streaks.
- **Drain** happens in the executor's finalisation and after the kill endpoint signals a stuck head. Before spawning the head the drain re-reads its `run.json` and refuses unless it is still `queued`, so a racing finaliser cannot double-fire it.
- **Failed spawns record why.** An `OSError` (`spawn_error` / `chain_spawn_error`) or a refused argv (`ArgvRejected`) finalises the run as `failed`, `exit_code: -1`, with a `refused` flag; the route answers `500` or `400 refused: <reason>`.
- Single host only.

### Parameters

A job can declare typed inputs. With none, **Run now** fires immediately; with some, it opens a dialog and the executor composes the values into argv and env.

```json
{
  "id": "scrape",
  "name": "Scrape",
  "script_path": "E:\\automation\\scraper\\scrape.py",
  "params": [
    { "name": "since", "kind": "date", "flag": "--since" },
    { "name": "tier", "kind": "enum", "options": ["smb", "mid"], "default": "smb", "flag": "--tier" },
    { "name": "verbose", "kind": "bool", "flag": "--verbose", "default": false }
  ]
}
```

`since = 2026-06-01`, `tier = mid`, `verbose = true` yields the argv tail `--since 2026-06-01 --tier mid --verbose`.

| Field | Type | Notes |
| --- | --- | --- |
| `name` | snake_case, unique per job | keys the input and labels the dialog field |
| `kind` | `string` \| `int` \| `enum` \| `bool` \| `date` | anything else fails validation |
| `default` | kind-typed, optional | pre-fills the dialog; makes the param non-required unless `required: true` |
| `required` | bool, optional | defaults to `false` with a `default`, else `true` |
| `options` | non-empty string list | required iff `kind: enum`; renders as a `<select>` |
| `flag` | `--…`, optional | emits `<flag> <value>` (just `<flag>` for a truthy bool); absent means positional |
| `env` | UPPER_SNAKE_CASE, optional | the value goes to the env overlay instead of argv; exclusive with `flag` |

A bool needs `flag` or `env`. Params compose in declaration order, so the list controls argv layout; the free-form `args` string follows as a tail.

**cmd.exe-unsafe values.** A value bound for a `.bat` dispatch or the `cmd /c start` re-parent hop is tokenised again by `cmd.exe`. `src.jobs.argv.reject_cmd_unsafe` refuses any of `"&|^<>` and any `%name%`-shaped substring (cmd would expand it to the PC's own environment variable, an information leak for a webhook-mapped value). A bare `%` such as URL encoding passes. A refused value raises `ArgvRejected` and the run route and webhook route answer `400 refused: <reason>`.

**Re-run from history.** A run record keeps its `params`; the ↻ button on a history row reopens the dialog pre-filled, dropping keys the schema no longer has and saying so.

## Webhook-target jobs

A webhook is the push direction: an external service POSTs to `POST /api/jobs/<id>/hook` and the job fires, with the payload mapped onto typed `params`.

### Security boundary — no bearer, ever

`/hook` sits outside the bearer-token gate (`is_webhook_hook_path` in `src/middleware/routes.py`): an external service cannot be handed the SPA's bearer. The route trusts only the job's own provider signature:

- **`github`**: `X-Hub-Signature-256: sha256=<hex>`, HMAC-SHA256 of the raw body, compared in constant time.
- **`stripe`**: `Stripe-Signature: t=<epoch>,v1=<hex>`, HMAC-SHA256 of `"{t}.{body}"`; `t` must be within 300 s of now, so a replayed signature fails.
- **`generic`**: `X-Webhook-Token: <secret>`, a constant-time compare, for IFTTT, Zapier or a custom script.

A failed verification, or an unknown provider secret, gets `401` and writes no run record, so the hook URL is safe to hand over in plaintext.

### Job shape

```json
{
  "id": "gh-push-deploy",
  "name": "Deploy on push",
  "script_path": "E:\\automation\\deploy\\deploy.py",
  "params": [
    { "name": "repo", "kind": "string", "flag": "--repo" },
    { "name": "branch", "kind": "string", "flag": "--branch" }
  ],
  "webhook": {
    "provider": "github",
    "secret": "$secret:gh_deploy",
    "mapping": { "repo": "$.repository.full_name", "branch": "$.ref" },
    "events": ["push"]
  }
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `provider` | `github` \| `stripe` \| `generic` | selects the verification scheme |
| `secret` | string | a literal, or `$secret:<key>` resolved against `webapp_config.json`'s `secrets` block at fire time |
| `mapping` | `{param_name: jsonpath}` | keys must match this job's `params`; resolution reuses the manual run's argv composition |
| `events` | string list, GitHub only | `X-GitHub-Event` allowlist; empty accepts all; a filtered event returns `204` with no record |

`mapping` values are a JSONPath-lite dot path (`$.repository.full_name`, `$.commits[0].id`). A path that does not resolve for one event's shape is omitted, so one mapping can list fields from several event types. A mapping key naming no declared param is a `400` with no run record. A mapped value refused as cmd-unsafe is a `400` at fire time.

A verified fire goes through the same cooldown and mutex admission as a manual run and writes `trigger: "webhook"`, `trigger_source: "webhook:<provider>"` and the mapped `params`. The raw payload (provider, event and content-type headers, never the signature or secret, and the parsed body) is kept in the run directory's `_webhook.json` and surfaces as `webhook_payload` in the run detail, shown in a collapsed "Webhook payload" block. A job with a webhook carries a `🪝 <provider>` chip; the editor's Webhook section has the provider picker, secret, event allowlist and a mapping editor.

## Task Scheduler — `\AppLauncherLite\` namespace

Every schtasks entry the Jobs tab creates lives under the `\AppLauncherLite\` folder, so it can share a machine with the full app-launcher's `\AppLauncher\`. The folder name is `config.json`'s `task_namespace` (default `AppLauncherLite`, read at startup).

- Single-task schedules are `\AppLauncherLite\<job_id>`; `daily_times` is `…\<job_id>-1`, `-2`, ….
- Sync is idempotent: each create or edit deletes every `\AppLauncherLite\<job_id>*` entry, then re-creates from the current schedule. Deleting a job removes the row and every entry.
- The stored `/TR` is quoted so paths with spaces survive: `"<repo>\.venv\Scripts\pythonw.exe" "<repo>\launcher.py" run-job <job_id>`. Scheduled runs use `pythonw.exe` (no window) unless the job is `visible`; a missing `.venv` falls back to `pythonw.exe` on PATH.
- Entries the launcher creates carry Windows' default interactive-token principal, so they do not fire while logged out. `session_less` is the opt-out; it cannot be applied from this process.
- `elevated` and `session_less` entries are externally managed (see "Per-job flags").

```powershell
schtasks /Query /TN "\AppLauncherLite\reporting-daily" /FO LIST /V
schtasks /Query /FO CSV /NH | findstr "AppLauncherLite"
```

**`next_run` cache.** One bulk `schtasks /Query /FO LIST /V` fills `{task_name: {next_run, enabled}}` for every entry under the namespace, cached for 30 s (`_NEXT_RUN_TTL_SECONDS` in `src/jobs/schtasks.py`), so `GET /api/jobs` runs at most one `schtasks` call per window. A failed query caches as `None`, distinct from `{}` ("no tasks"), so one bad query cannot flag every job as missing its entry. `sync_schtasks` and `delete_schtasks` invalidate it.

## Run history — `webapp/jobs/<job_id>/<run_id>/`

| File | Content |
| --- | --- |
| `run.json` | one run's metadata (schema below) |
| `output.log` | combined stdout and stderr, raw bytes |
| `artifacts/` | files the child keeps through `JOB_ARTIFACT_DIR` |
| `outbox/` | files the child sent to the owner (see below) |
| `_webhook.json` | the triggering webhook's payload and safe headers (webhook runs only) |

`run_id` is a sortable timestamp (`YYYYmmddTHHMMSS`); a same-second collision appends `-2`, `-3`. A script writes whatever it wants kept into `JOB_ARTIFACT_DIR`; the run detail lists those files and the download route is path-jailed (parent traversal and nested paths return `400`). Unpinned history is pruned to the newest **20 runs per job** when a run ends; a run with `pinned: true` is exempt, and its artifacts follow its directory.

Status goes `pending` (webapp pre-create) → `running` (executor takes over) → `success` | `failed`; the full set is in [job-run-state.md](job-run-state.md). The runs list refreshes on a 4 s poll; selecting a live run opens `WS /api/jobs/<id>/runs/<rid>/stream`, which sends a current-tail snapshot, incremental chunks and a final status frame. Finished output is fetched once over JSON.

### Sending files to the owner — the job outbox

A job has no hosted session, so it cannot use a session's outbox. The executor gives the child `APP_LAUNCHER_JOB_ID` and `APP_LAUNCHER_JOB_RUN_ID` beside `JOB_ARTIFACT_DIR`, and removes `APP_LAUNCHER_SESSION_ID` and `APP_LAUNCHER_AGENT` from the inherited environment so a job never drops files into an unrelated session's chat.

```bash
curl -k -X POST "https://127.0.0.1:8465/api/jobs/$APP_LAUNCHER_JOB_ID/runs/$APP_LAUNCHER_JOB_RUN_ID/outbox" -H 'Content-Type: application/json' -d '{"path": "shot.png", "caption": "screen"}'
```

The file passes the session outbox's guards, caps and refusals (see [interactive-terminal.md](interactive-terminal.md)) and is copied into the run's `outbox/`. It may come from `JOB_ARTIFACT_DIR` (a relative path resolves against it) or an `outbox_scratch_roots` folder. The POST is loopback-only and only a `running` run takes a file. Refusals: `loopback_only` 403, `bad_request` 400, `job_not_found` / `run_not_found` 404, `run_not_running` 409, then the shared `out_of_root` 403, `not_found` 404, `not_a_file` 422, `too_large` 413, `type_not_allowed` / `content_mismatch` 415. The selected run's panel shows "N sent files" with a **Show** button that loads them as Chat file cards; the list and bytes are Tailscale + passkey and load only on that tap.

### `run.json` schema

| Field | Type | Purpose |
| --- | --- | --- |
| `run_id`, `job_id` | str | sortable timestamp (matches the dir) and the FK to `config/jobs.json` |
| `name` | str | job name at run time (denormalised; survives renames) |
| `trigger` | `manual` \| `scheduled` \| `webhook` \| `chain:<upstream_id>` | where the run was fired from |
| `trigger_source` | `api` \| `schtasks` \| `webhook:<provider>` | provenance; absent on a direct CLI fire |
| `trigger_ip`, `trigger_ua` | str | API fires only: caller address and `User-Agent` |
| `trigger_token_id`, `trigger_token_label` | str | fires authenticated by a minted token: its id and label, never the secret |
| `script_path`, `args` | str | resolved at spawn time |
| `params` | object | typed payload, only when non-empty |
| `started_at`, `finished_at` | ISO 8601 | `finished_at` only on the final write |
| `status` | see [job-run-state.md](job-run-state.md) | final value lands at executor exit |
| `exit_code` | int | `-9` is reserved for `/kill`; input to the derived `outcome` |
| `pid`, `pid_create_time` | int, float | child PID persisted at spawn (so kill works even if the executor crashes) and its create time (so a recycled pid is told apart) |
| `executor_pid`, `executor_create_time` | int, float | the executor itself, stamped when it flips to `running` |
| `duration_seconds` | float | wall-clock seconds, 3 d.p. |
| `peak_rss_bytes`, `cpu_seconds` | int, float | peak resident set and CPU summed over the process tree, sampled at about 1 Hz |
| `killed` | bool | finalised via `/kill` (a human) |
| `reaped` | bool | finalised because the pid was provably dead and the executor never finalised it |
| `end_time_unknown` | bool | a reaped record whose end time could not be established; `finished_at` and `duration_seconds` are then omitted |
| `watchdog`, `watchdog_reason` | bool, `no_output` \| `max_runtime` | the executor watchdog tore the run down |
| `note` | str | one-liner for a non-obvious finalisation (`cooldown`, an invocation error, a watchdog message) |
| `pinned` | bool | keep-forever flag |

Plain files are the canonical store, readable with `cat`. `webapp/jobs/_index.sqlite` is a derived mirror: `runs` for queryable metadata and an FTS5 table for cross-run output search. `src/jobs/index.py` rebuilds it from the `run.json` / `output.log` pairs when it is missing, corrupt or has an older `PRAGMA user_version`; deleting it is the supported repair.

## Authoring safety

### Pre-flight on save

`src/jobs/preflight.py::preflight(job)` is a pure function (no subprocess, no `schtasks`) that checks a job before its schedule starts. An **error** blocks the save with `400`; a **warning** saves once acknowledged.

1. `script_path` exists: error.
2. A `.py` job has an ancestor `.venv\Scripts\python.exe`: warning (the executor would fall back to `sys.executable`).
3. A `.bat` job names a `.venv` interpreter or `activate` path that resolves: warning (best-effort text scan).

`POST /api/jobs` and `PUT /api/jobs/<id>` run it on the effective job: errors give `400 {"detail": {"reason": "preflight", "problems": [...]}}`; unacknowledged warnings give `200 {"saved": false, "warnings": [...]}` and the dialog offers **Save anyway**; with `"acknowledge_warnings": true` or no problems the row saves and warnings are echoed. A `Problem` is `{level, field, message}`. There is no `schtasks` round-trip check, which would put a shell-out on the request path.

### Dry-run

`POST /api/jobs/<id>/run` accepts `dry_run`:

- **`"check"`** (kebab **Dry-run check**) resolves the full invocation (script exists, venv walk-up, param composition) without spawning anything and writes a synthetic record, `dry_run_success` or `dry_run_failed` with the reason in `note`.
- **`"execute"`** (the Dry run toggle in the run dialog) runs the real executor with `JOB_DRY_RUN=1`; scripts that honour it suppress side effects. The record is stamped `dry_run: true`.

Both bypass cooldown and the mutex queue, and dry-run records never anchor a cooldown. History marks them with a `🧪 dry` chip.

## API surface

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /api/jobs` | bearer | list jobs with `schedule_chip`, `target_kind`, `next_run`, `next_run_epoch` / `next_run_iso`, `last_run`, `running`, `run_count`, `pinned_count`, `coverage`, `script_missing`, `rerun_safe` |
| `GET /api/jobs/agenda?days=7` | bearer | upcoming fires over `days` (1..14): `{occurrences, frequent, days, generated_epoch}` |
| `POST /api/jobs` | bearer | create |
| `PUT /api/jobs/<id>` | bearer | edit (re-syncs schtasks) |
| `DELETE /api/jobs/<id>` | bearer | remove the row and its schtasks entries |
| `POST /api/jobs/<id>/run` | bearer | trigger now; returns `run_id`, spawns the executor detached |
| `POST /api/jobs/<id>/hook` | provider signature only, never the bearer | trigger from an external service |
| `GET /api/jobs/<id>/runs` | bearer | newest-first history |
| `GET /api/jobs/runs/search?q=&job=&status=&since=` | bearer | FTS5 cross-run output search with snippets |
| `GET /api/jobs/<id>/runs/<run_id>` | bearer | metadata, last 64 KB of output, artifact list, `webhook_payload`, `sent_files` count |
| `PUT /api/jobs/<id>/runs/<run_id>` | bearer | set `{pinned}` |
| `GET /api/jobs/<id>/runs/<run_id>/artifacts/<filename>` | bearer | download one path-jailed artifact |
| `POST /api/jobs/<id>/runs/<run_id>/outbox` | loopback only | a running job sends `{path, caption?}` to the owner |
| `GET /api/jobs/<id>/runs/<run_id>/outbox[/<entry_id>]` | Tailscale + passkey | a run's sent files and one file's bytes |
| `WS /api/jobs/<id>/runs/<run_id>/stream` | bearer (token in the query string) | live tail |
| `POST /api/jobs/<id>/runs/<run_id>/kill` | bearer | terminate a stuck run's process tree |

## What the list shows

Each row is the shared action row: the job avatar (the green alive badge while it runs), the name, one meta line ("in 5m · Every 15 min", "running now · Daily 03:00", "Was daily 07:00" when paused), chips for exceptions only, a seven-dot sparkline, and a kebab (Run now, Pause/Resume, Dry-run check, Edit, Remove). Tapping the row opens the job sheet (the detail pane on wide layouts) with details, runs and output. Everything below is recomputed on each `/api/jobs` poll.

- **Sparkline.** The last seven runs, oldest left: green success, red failed, amber not confirmed, a hollow ring deferred, grey unknown, the accent colour for a run in progress.
- **Chips.** **failed**, **script missing** (a non-paused job whose script no longer exists) and **not firing** in danger; **stuck** and **not confirmed** in attention; **paused** neutral. A healthy job has none. The header reads "N failing" and "N not firing" in their tones, else "N jobs".
- **Details.** `p50 4.2s · p95 11s` over completed runs, a 30-day success rate, and CPU / peak RSS on the selected run's output label. Tapping the output pane copies the whole log (a manual selection suppresses it).
- **Stuck.** The latest run has been `running` for more than `max(p95 × 3, 300 s)`. The chip is surface-only; the sheet's **Kill stuck run** is the human's choice. The watchdog below is the automatic backstop.
- **Order and countdown.** `src.jobs.next_fire(schedule, *, now)` computes the next fire from the bounded schedule shape (paused and `none` give `None`) and `/api/jobs` exposes it as `next_run_epoch` / `next_run_iso`, deliberately apart from the locale-formatted, lexically sorted `schtasks` string. The client sorts ascending by it, with manual-only and paused jobs last; the sliders button opens a Sort and filter sheet that switches to A–Z, persisted in `localStorage` (`launcher.jobsSort`). Each scheduled row opens its meta line with `in 3h` (`due` once past).
- **Schedule agenda.** The **Next up** card shows the next three distinct jobs; its header opens a **Schedule** sheet grouping the week's fires by day (`app/webapp/static/jobs/jobs-agenda.js`). `src.jobs.upcoming_fires` enumerates fires by walking `next_fire`; dense `minutes` / `hourly` jobs are summarised in a footer rather than enumerated. Tapping a fire opens that job's sheet.

`src/jobs/__init__.py::run_stats(job_id)` feeds these: `p50`, `p95`, `success_rate_30d` (`None` with no known outcomes), `unconfirmed_30d`, `completed_count` and `last7`, cached 30 s per job and invalidated when a run finalises.

## Terminal outcomes — `outcome` vs `status`

`status` on disk is binary: `success` for exit 0, `failed` otherwise. A non-zero exit is not always a failure, so `src/jobs/outcome.py` classifies the exit code into a derived `outcome` (`success`, `failed`, `unconfirmed`, `deferred`) plus an `outcome_reason`, and `jobs_history.read_run()` adds both to every record it reads. They are derived on read, never persisted, so old records re-render with no migration and `status` keeps its meaning.

- **`unconfirmed`** means the run's delivery was never established, which is its own fact and not a failure. A block of codes (114 to 130) reserved by a scheduled-agent runner is classified in `src/jobs/outcome.py`; an ordinary script's own exit codes are never unconfirmed.
- **The code table applies only to Jobs that run the adapter.** `src/jobs/adapter_job.py` decides that from the Job's command, args, inline body or wrapper script text naming `claude_progress.py` or `scheduled_runner.py`, so a `run-*.bat` wrapper counts. Any other Job's non-zero exit reads `failed` with no reason, because a plain script's exit `2` is its own (the log has the cause). A wrapper that cannot be read is treated as not the adapter.
- **`deferred`** is declared per job, never globally, in `config/jobs.json`: `"exit_outcomes": {"3": "deferred"}`. It is for a child that exits non-zero on purpose, such as a task that has nothing to do in a session without a desktop. Only `deferred` is declarable, exit `0` cannot be, and a malformed declaration fails the row's load. It overrides the table for that job's declared codes only.
- A run this launcher killed, reaped or watchdogged is always `failed`.

Where it shows: `unconfirmed` is an attention dot and a **not confirmed** chip, a `?` glyph in history, and an `unconfirmed` card in the Board's Other column; `deferred` is a hollow ring, a clock glyph, no chip and no alert. Both are excluded from `success_rate_30d` and the p50/p95 pool, and both break a failure streak instead of extending it. Chaining still keys on the persisted `status`.

## Stopping and reconciling runs

**Stuck-run kill.** `POST /api/jobs/<id>/runs/<rid>/kill` returns `404` for an unknown job or run and `409` unless the run is `running` or `pending`. It reads the persisted `pid`, terminates the parent and every recursive child with `psutil`, waits 5 s, kills the survivors, and finalises `status: failed`, `exit_code: -9`, `killed: true`. If the executor already exited the route still finalises the record, which is the bug the button fixes.

**Executor watchdog.** The manual kill and the reap below do not cover a run wedged while its executor is alive. `RunWatchdog` in `app/cli/commands/run_job_supervision.py` is a daemon thread beside the resource sampler, watching two signals: `output.log` has not grown for `no_output_seconds` (size, not mtime, since Windows does not refresh a directory entry while a handle is open; a failed `stat` skips the tick), or wall-clock exceeds `max_runtime_seconds`. A breach kills the child's tree through `src.diagnostics.kill_process_tree`; the closed pipe then lets the executor finalise normally, so the watchdog only records why it fired.

Both fields are optional per job and tri-state: absent means the default, a positive int is that many seconds, and an explicit `0` disables that signal. Defaults: `no_output_seconds` is 1 hour (a job that prints only at the end is common), and `max_runtime_seconds` is `src.jobs.stats.derived_runtime_ceiling_seconds(job_id)`, the same `max(p95 × 3, 300 s)` as the stuck chip, but only once the job has `WATCHDOG_MIN_COMPLETED_RUNS` (5) completed runs; below that there is no runtime ceiling, because a false chip costs a glance and a false auto-kill of a healthy first run is unrecoverable. Both limits are resolved on the main thread before the loop starts, and the kill happens before the breadcrumb is logged (on a throwaway thread, since the log handler writes to the console a wedge may have jammed). A child that exited just before a breach is never reported as watchdog-killed.

The run finalises `failed` with `watchdog: true`, `watchdog_reason` and a `note`, and the run row shows one "who ended this" chip: `watchdog <reason>`, `killed` or `reaped` (`endedChip` in `app/webapp/static/jobs/run-row.js`). It feeds `on_failure` and the notifications like any failure; there is no auto-retry.

**Stranded-run reap.** If the executor itself dies (reboot, OOM, killed shell) nothing writes a terminal status. `src/jobs/reap.py` reconciles on read: every `GET /api/jobs` poll and every Board stuck sweep calls `reap_stranded_runs(job)`, and `src.jobs.queue.mutex_collision` calls the drain-less `finalize_dead_runs`.

- *Provably dead* means a recorded `pid` that `psutil` confirms gone or recycled (`pid_create_time` compared within 1 s). No `pid`, or an alive pid, is left alone.
- *A dead child is not a dead run.* While the stamped `executor_pid` is alive the reap is deferred (log line `reap deferred`); a reap needs both dead. The write re-checks under the run lock so a late exit code still wins.
- It sweeps every non-terminal record in the job's history, not just the latest.
- It writes `failed` with `reaped: true`, invalidates the stats cache, drains the mutex group once and fires the failure alert.
- `finished_at` comes from `output.log`'s last-write mtime when that is after `started_at`; otherwise it and `duration_seconds` are omitted and `end_time_unknown: true` is set, which also keeps the run out of p50/p95. Stamping "now" would fold hours of discovery gap into the duration.

## Failure notifications

Set the Pushover keys in `config/webapp_config.json` and flip `notify_on_failure: true`; the executor then pushes once per failed run. The master switch defaults off, so the feature ships dormant.

| Key | Default | Effect |
| --- | --- | --- |
| `pushover_api_token` / `pushover_user_key` | `""` | both required; otherwise the notifier is a no-op |
| `notify_on_failure` | `false` | master switch |
| `notify_failure_streak` | `0` | when above 0, also pushes "N consecutive failures" as the streak reaches exactly N |
| `notify_failure_summary` | `false` | pipe the last ~500 characters of `output.log` through the local LLM hub for a one-line root-cause prefix; hub down falls back to the raw tail |

Not every `failed` run is announced as one: a code meaning "delivery never established" pushes as `❓ <job> — not confirmed` at `warning` severity (Pushover priority 0, no quiet-hours bypass) with the code's meaning in the footer; `killed`, `reaped` and `watchdog` runs are always failures; a `deferred` run is silent. The body is the optional summary, the output tail, and a footer `— job=<id> run=<rid> exit=<code>` with `(reaped — end time not confirmed)` or `(watchdog: <note>)` appended when applicable. The whole path sits in one `try`/`except`: bad credentials, a Pushover 5xx or an unreachable hub log at `WARNING` and never block the executor's exit.

A failed run publishing a mailbox event for the fleet chief is planned: Jobs feeding the chief is an open port step in the README.

## Missed-fire coverage

Alerting covers a run that failed and the stuck chip covers one that never ended; `src/jobs/coverage.py` covers one that never **started** (a missing, disabled or never-created Task Scheduler entry). Two halves, both from data the tab already reads:

- **Structural.** Every non-paused scheduled job needs an **enabled** `\AppLauncherLite\<id>` entry (every fan-out slot for `daily_times`), read from the same cached bulk query as `next_run`.
- **Behavioural.** The schedule is expanded across the last `COVERAGE_WINDOW_DAYS` (3) via `upcoming_fires` and each elapsed slot is matched against run records.

The never-flag rules keep it from crying wolf:

| Rule | Why |
| --- | --- |
| Paused and `schedule: none` jobs are `exempt` | nothing to miss |
| `minutes` / `hourly` skip the behavioural half | too dense to enumerate; the structural half still catches a deleted entry |
| A slot is missed only 15 min after it (`MISSED_FIRE_GRACE_SECONDS`) | Task Scheduler starts late, a waking machine very late |
| Any run record near the slot counts as a fire | including `skipped` (a cooldown no-op did fire) and a manual run |
| The window never reaches past `added_at` or the oldest retained run at the 20-run cap | a pruned record is not evidence |
| A failed `schtasks` query is `unknown`, never "missing" | an unestablished fact gets its own state |

`GET /api/jobs` decorates each row with `coverage` (`{state, detail, problems, missing_tasks, disabled_tasks, missed_count, missed_fires}`, `state` one of `ok` / `problem` / `unknown` / `exempt`); only `problem` renders, as a **not firing** pill. A lifespan tick re-scans on an interval so alerting does not depend on the tab being open, and is skipped on a disposable instance (the autoboot webapp, identified by `LAUNCHER_SESSION_HOST_PORT`). Pings reuse the global Pushover channel gated by `notify_on_failure` and are de-duplicated through `webapp/jobs/coverage-alerts.json`: a job re-alerts when its problem signature changes or the class's repeat interval passes, and a recovered job is dropped so its next break alerts at once.

**Script missing.** Pre-flight proves a script existed at save time; `GET /api/jobs` adds `script_missing` per row (`true` only when the path is established absent; paused jobs, scriptless kinds and an unanswerable path read `false`) and the row shows a **script missing** chip. Nothing edits `jobs.json`.

Each problem class has its own wording, in `src/jobs/coverage.py::PROBLEM_ANNOUNCEMENTS`, most root-causal first, because one shared "never fired" title teaches readers to dismiss the channel:

| Class | Title | Severity | Re-ping |
| --- | --- | --- | --- |
| `task_missing` | 🕳️ *name* — Task Scheduler entry missing | `error` | 24 h |
| `task_disabled` | 🚫 *name* — Task Scheduler entry disabled | `error` | 24 h |
| `offset_pinned_trigger` | 🕐 *name* — trigger pinned to a UTC offset (drifts at DST) | `warning` | 24 h |
| `missed_fire` | 🕳️ *name* — scheduled run never fired | `error` | 24 h |
| `principal_interactive` | 🔒 *name* — will not fire while logged out | `warning` | 7 d |

`offset_pinned_trigger` is a `StartBoundary` carrying a UTC offset (`…+01:00` or `…Z`, read from one `schtasks /Query /XML` of the namespace folder): the job runs, but an hour off the wall clock after each daylight-saving change, and its missed slots are not reported separately. `principal_interactive` is a conditional risk, so it is a weekly warning that is quieter but never muted. With several classes present the title and severity come from the highest-precedence one, the body lists all, and the repeat interval is the shortest. An unknown class falls back to a vague "schedule coverage problem".

| Key | Default | Effect |
| --- | --- | --- |
| `jobs_coverage_interval_minutes` | `60` | minutes between background scans; `0` disables the tick (the badge still computes on poll) |

## Per-job secrets & env

A job can declare `env: {NAME: value}` in `config/jobs.json` or through the create/edit API. The executor merges it into the child's environment after `os.environ` and before the typed-param env, so a per-run param can override it. A value is a literal or a `$secret:<key>` reference resolved against `webapp_config.json`'s gitignored `secrets` block (`src.jobs.secrets`), the same mechanism as `webhook.secret`:

```json
{ "id": "sync", "name": "Sync", "script_path": "E:\\automation\\sync\\sync.py", "env": {"API_KEY": "$secret:sync_api"} }
```

`jobs.json` and every API response carry only the reference. An unresolvable reference finalises the run `failed` with `note: "secret '<key>' not found"`, and the dry-run check catches it without firing. Env names are UPPER_SNAKE_CASE. A rotated secret changes in one place.

## Scoped API tokens

Settings > **API tokens** mints bearer tokens scoped to one job (or `"*"` through the API). A job-scoped token can call only `POST /api/jobs/<id>/run` for its allowed jobs and gets a precise `403` everywhere else, so a leaked Stream Deck export unlocks one job, not the SPA. Records live in `api_tokens` as `{id, label, salt, hash, scope, created_at, last_used_at}`: only a salted SHA-256 hash is stored and the raw token is shown once at mint time, with a copy button and a ready-to-paste run URL when the tunnel URL is known. Revoking deletes the record; the legacy `auth_token` keeps working with implicit `"*"` scope. Endpoints `GET /api/tokens`, `POST /api/tokens {label, jobs: [ids]}`, `DELETE /api/tokens/<id>` need full-scope auth. A run fired by a token records its id and label (`trigger_token_*`); the history shows a `🎛` chip.

## Security boundary

Jobs sit on the Apps-tab side of the launcher's security model, not the interactive-terminal side:

- `POST /api/jobs/<id>/run` is bearer-gated and reachable over a public tunnel, which is the point: a Stream Deck button hits the same HTTPS endpoint as the phone. The legacy `auth_token` and a job-scoped token both pass; the scoped one passes only here.
- `POST /api/jobs/<id>/hook` is exempt from the bearer and authenticates through the job's provider signature.
- The per-run WebSocket is an output-only tail under the same bearer boundary, not an interactive terminal; the Tailscale + passkey gate does not apply.
- The `id` is checked against the registry on every call, so the launcher cannot be coerced into running an arbitrary path; editing `config/jobs.json` is the only way to register a target.

## Stream Deck recipe

A Stream Deck "Website / System" action calls the run endpoint directly, no plugin:

```
URL:    https://<host-reachable-from-the-deck>/api/jobs/reporting-daily/run?token=<job-scoped-token>
Method: POST
```

Mint the token in **Settings → API tokens**, scoped to exactly this job. Deck configs store button URLs in plaintext, so a scoped token caps a leak at firing that one job; revoke and re-mint to rotate it without touching the SPA's `auth_token`. Use a tunnel or Tailscale URL (`<host>.<tailnet>.ts.net:8465`), not loopback. The deck shows ✓ / ✗ from the HTTP status and the history shows the run with a token chip.

## Design notes

- **Files, not a database, are the canonical run store.** Cold-reader friendly; SQLite is only a rebuildable metadata and FTS mirror.
- **Task Scheduler, not an in-process scheduler.** Schedules survive a tray restart, a reboot and an uninstall; a second scheduler would couple firing to the launcher's lifecycle.
- **No live PTY per job.** A one-shot script needs no input; the output-only WebSocket tails `output.log` and terminal infrastructure stays with Coding.
- **No raw cron.** The presets cover real use without the day-of-week-field pitfall.

## Verification

The pre-ship gate (`C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -File scripts/verify-before-ship.ps1`) runs the unit and API suites for jobs plus the e2e Jobs-tab smoke check. Every `schtasks` call is mocked at the runner seam (`src.jobs._run_schtasks`), so tests never touch the real Task Scheduler.

Live check after a restart of `:8465`:

```powershell
schtasks /Query /FO CSV /NH | findstr "AppLauncherLite"
curl.exe -k -X POST "https://127.0.0.1:8465/api/jobs/reporting-daily/run"
type webapp\jobs\reporting-daily\<latest>\run.json
type webapp\jobs\reporting-daily\<latest>\output.log
```
