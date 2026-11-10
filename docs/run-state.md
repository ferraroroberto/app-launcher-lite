# Run state — the Jobs run lifecycle

A Jobs run has real modes (waiting, pending, running, succeeded, failed, killed, reaped, skipped, dry-run) that `run.json` stores as a free-text `status` string plus the flags `killed`, `reaped`, `dry_run` and the `pid` / `pid_create_time` pair. `src/jobs/run_state.py` declares them once, as a closed `RunState` enum, and `derive_run_state(record, *, now, pid_alive)` is the only code that maps those facts to a mode. `run.json` keeps writing the same `status` strings (the `STATUS_*` constants in the same module), so run history and the sqlite index need no migration.

## Why it is declared

Each reader used to decide on its own which strings mean "live", and they disagreed: the `running` field of `GET /api/jobs` counted only `running`, the mutex gate and the reaper counted `running` and `pending`, the output stream counted `queued` too, and the Jobs tab patched the first row client-side. The disagreement hid a real defect: the reaper only reconciled a record that carried a `pid`, and the pid is written only after the child spawns, so a record whose executor died before that point stayed live forever and held its mutex group until someone tapped Kill. A declared state turns that into a derived `failed` with a reason.

## States

| State | `status` on disk | Meaning |
| --- | --- | --- |
| `queued` | `queued` | Waiting behind its mutex group; no executor yet |
| `pending` | `pending` | Fired and the executor spawned; the child has not started |
| `running` | `running` | The executor wrote the running record; the child may be alive |
| `succeeded` | `success` | The child exited 0 (a dry-run *execute* run also lands here, flagged `dry_run`) |
| `failed` | `failed` | Finished without success; the reason says why |
| `killed` | `failed` + `killed: true` | An operator stopped it; wins over any other field |
| `reaped` | `failed` + `reaped: true` | The pid was provably dead and a sweep closed the record |
| `skipped` | `skipped` | Cooldown no-op, never executed |
| `dry_run_succeeded` | `dry_run_success` | Dry-run check passed; nothing executed |
| `dry_run_failed` | `dry_run_failed` | Dry-run check failed; nothing executed |
| `unknown` | anything else, or no status | The record's facts do not name a mode; never folded into a negative |

A `failed` state carries a `FailReason`: `exit_code`, `watchdog`, `spawn_error` (exit `-1`), `executor_never_started` (a pending or running record with no pid more than `SPAWN_DEADLINE_SECONDS` after `started_at`; the reaper stamps `fail_reason` on disk). A `reaped` state carries `executor_lost`.

## Derive rules

1. `killed` wins over everything, so a stale `success` finalise cannot resurrect a killed run.
2. `reaped` comes next.
3. `success`, `failed` and the terminal strings map directly; an unrecognised or missing status is `unknown`.
4. A pending or running record with a provably dead pid (`pid_alive` is `False`) is `reaped`; with no pid past the spawn deadline it is `failed`; otherwise it stays as recorded. `pid_alive` is the caller's probe result and `None` means "not probed", so cheap read paths never touch a process.

## Who reads it

| Reader | Helper |
| --- | --- |
| `src/jobs/history.py::is_running` (the `running` field of `GET /api/jobs`) | state is `running` |
| `src/jobs/queue.py::mutex_collision`, `drain_mutex_queue` | `is_live`, state is `queued` |
| `src/jobs/reap.py` (`finalize_dead_runs`, `_reap_one`) | `is_executor_owned`, `derive_run_state` |
| `src/jobs/stats.py` (`is_stuck`, `consecutive_failed_runs`, cooldown anchor, run stats) | `is_failure`, `is_real_fire`, state is `running` / `succeeded` |
| `app/webapp/routers/jobs.py` (`kill_job_run`, `_decorate_job`, run list and detail) | `is_killable`, `run_view` |
| `app/webapp/routers/jobs_run_store_routes.py` (output stream) | `run_view` (`open`, `live`) |
| `app/webapp/static/jobs-row.js`, `jobs.js` | the `live` field |

`run_view` adds `state`, `live`, `open` and `fail_reason` to the `last_run` payload of `GET /api/jobs`, to the run list and detail routes and (`live`) to the stream frames. `live` is `is_live` (pending or running); `open` also covers `queued`, which is why the stream stays attached to a queued run.

## Adding a mode

Add the state, give it a one-line meaning, and handle it in `derive_run_state`, the helper sets and the table test (`tests/unit/jobs/test_run_state.py`). A new reader calls `derive_run_state` or `run_view`; it never compares a `status` string. The machine is declared in `.fleet.toml` as `run-lifecycle`, and the proof tests carry the `state-machine-proof: run-lifecycle` marker.
