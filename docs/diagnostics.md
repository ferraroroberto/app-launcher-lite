# Diagnostics

When App Launcher Lite misbehaves on a computer you cannot sit at, its logs are the only evidence, and they are spread over several files written by several processes. **Save diagnostics** gathers a recent window of all of them into one plain-text file, redacted, so it can be read, searched or sent.

## Saving a file

- **From the phone or the PC:** Settings > This PC > **Diagnostics**. Pick the window (last 15 minutes, last hour, last 3 hours) and tap **Save diagnostics**. The sheet shows how many log records were saved, the size and the path on the PC; **Download the file** then hands it to the browser. Both steps go through the passkey unlock, because the file carries session lifecycle and chief mailbox traffic.
- **From the command line**, on the PC itself, which also works when the webapp is down:

  ```
  .\.venv\Scripts\python.exe launcher.py diagnostics --minutes 60
  ```

  `--minutes` takes any whole number from 1 to 1440.

Each file lands in `webapp/diagnostics/diagnostics-<date>-<time>.txt` (gitignored with the rest of `webapp/`'s runtime state). Nothing is uploaded; old files stay until you delete them.

## What it collects

The file opens with a snapshot taken at the moment it is saved:

- the build: the checkout's git sha and, from Settings, the sha the running webapp loaded;
- the processes: the webapp, the session-host (by the ports they listen on) and the tray, each with its pid and start time;
- the chief mailbox: each pending and claimed event file with its age;
- the lane marks (`chief-managed.json`) and the session rows (`sessions-state.json`).

Then every log record from the window, oldest first, each line tagged with its source:

| Source tag | Written by | What it holds |
|---|---|---|
| `webapp-warnings` | webapp | every warning and error the webapp logs (rotated `.log.N` files too) |
| `slow-requests` | webapp | slow and over-budget requests, in-flight spikes, hot-poll step timings |
| `chief-doorbell` | webapp | each doorbell ring and its delivery verdict, each skip reason |
| `chief-events` | webapp | the mailbox events the webapp publishes (job failed, cap raised, script missing, a lane's missed turn end) and why one was skipped, and each lane mark a Board dispatch writes |
| `chief-decisions` | Copilot hooks and chief helpers | each mailbox event written or skipped, each claim and ack, each lane mark `chief_ops.py` writes |
| `terminal_audit` | webapp | session starts and stops |
| `session-host` | session-host | its own output, tracebacks included |
| `watchdog`, `restart`, `startup`, `registered_trays` | tray and `tray.bat` | the tray's tracking, self-heal and restart lines |
| `channel-autostart`, `auth`, others | webapp | any other `*.log` in `webapp/` |

A line without a timestamp of its own (a traceback, a server's bare warning) stays with the line above it. A log with no timestamps at all contributes its last 50 lines, stamped with the time the file last changed, and only when it changed inside the window.

Left out on purpose: the per-session logs under `webapp/sessions/` (they hold what was typed into each session), Job run output, and the disposable gate instances' `e2e-*` and `verify-progress*` logs.

`chief-decisions.log` lives beside the chief mailbox (`~/.copilot/hooks/state/` by default), because the processes that write it (the Copilot hooks and `chief_ops.py`) are not the webapp; it rolls to `chief-decisions.log.1` past 5 MB.

## Redaction

Before anything is written, every line passes through `src/diagnostics_bundle.py`'s `redact`, which replaces with `[REDACTED]`:

- auth headers, cookies and terminal tokens (`Authorization:`, `Cookie:`, `Set-Cookie:`, `X-Terminal-Token:`, `X-Api-Key:`) and `Bearer` / `Basic` values;
- any `key=value` or `"key": "value"` whose key names a secret (token, secret, password, api key, access or private key, credential, signature, auth);
- credential query values in URLs (`?tt=`, `&token=`, `&key=`, ...);
- known token shapes wherever they stand: GitHub, GitLab, OpenAI-style `sk-`, Slack, AWS access keys, Telegram bot tokens, JWTs and PEM private-key blocks;
- the exact value of every secret `config/webapp_config.json` holds (the bearer token, the login password, notification tokens, Job secrets).

The redaction errs toward removing too much: a harmless value under a key such as `author` is blanked too. `tests/unit/core/test_diagnostics_bundle.py` proves each rule on a written file.

## Request timings and budgets

A slow answer is either slow work or fast work that waited for a free thread. Every `/api/` response says which, in `Server-Timing`:

```
queue;dur=1.2;desc="default+hot-poll", worker;dur=31.5;desc="default+hot-poll", app;dur=63.3
```

- `queue` is the time the request's work waited for a pool thread, `worker` the time it ran on one, `app` the server's whole time to the response headers (the request's `total_ms`), all in milliseconds. `desc` names the pool: `default` (the event loop's own), `hot-poll`, `session-reads`, `git-status`, `git-sync`, or `none` for a request that used no thread.
- Parallel steps add up, so `queue + worker` can exceed `app`. A shared single-flight build is charged to the request that started it. The header carries what had finished when the response began; the log lines below carry the final figures.

`webapp/slow-requests.log` repeats the split as `queue_ms= worker_ms= total_ms= pool=` on every slow-request line (past `LAUNCHER_SLOW_REQUEST_S`, default 3 s) and on every budget line.

Each request class has a **soft** and a **hard** limit, in seconds:

| Class | Request | Soft | Hard |
|---|---|---|---|
| `transcript-tick` | `GET .../transcript?after=` | 0.5 | 1.5 |
| `transcript-open` | `GET .../transcript` | 1 | 1 |
| `session-list` | `GET /api/claude-code/sessions` | 0.5 | 1.5 |
| `board` | `GET /api/board` | 0.5 | 1.5 |
| `send` | `POST .../input` | 5 | 10 |
| `git-status` | `GET /api/claude-code/git-status` | 5 | 10 |
| `shell` | `GET /` | 0.25 | 3 |
| `jobs-list` | `GET /api/jobs` | 0.25 | 3 |

Past soft, the log gets a `⏱️ over budget` line; past hard, a `🚨 budget breach` line. To change a class, set it in `request_budgets` in `config/webapp_config.json`, for example `{"board": [1.0, 3.0]}`; the other classes keep their defaults, an entry that is not `[soft_s, hard_s]` with `0 < soft_s <= hard_s` is ignored with a warning, and a save applies live.

`GET /api/request-stats` (`?window_s=300`) reads the same counters: requests in flight now, the highest in-flight count in the window, the verdicts per class since boot, the budgets in force and the age of the last git-status scan. It reads counters and a cache and starts no work.

## Benchmark

`.\.venv\Scripts\python.exe -m scripts.bench_requests` times the Board, the session list, a transcript open, a live transcript tick and git status against a running webapp (`--base http://127.0.0.1:8465` by default, `-n` samples each) and reports p50, p95 and max, the median queue and worker time the server reported, and the highest in-flight count during the run. It sends GETs only, one at a time. Send writes into a session, so it is measured only with `--send-session <sid>` on a throwaway session. Each run is saved as JSON under `webapp/bench/`, so a run before and a run after a change compare directly. A webapp that predates `/api/request-stats` reports the in-flight figure as unknown.

## How the page polls

One open page asks for each read once at a time. The polls in `app/webapp/static/shared/poll.js` schedule the next tick when the last read has settled, so a slow server stretches the gap instead of stacking requests, and a caller that asks while a read is still out (a tab tap, a poll tick, a return to the foreground) shares it. A hidden tab or a locked phone arms no tick; coming back reads each shape once, then resumes the normal spacing. The session list and the Board poll every 5 s, the open Chat transcript every 3 s, and the `/context` ring and the plan picker only while the session view that shows them is open. `tests/e2e/shell/test_poll_single_flight.py` holds each read open at the route and checks that no second one starts.

## Health snapshot

`.\.venv\Scripts\python.exe launcher.py health` writes `webapp/health/health-<date>-<time>.txt`: the webapp, session-host and tray processes, available memory, the top CPU processes over one second, the running agent processes, the lane marks and session rows, requests in flight and verdicts per class, the git-status cache age and the last 20 lines of the slow-request log, redacted like the diagnostics file. It changes nothing: no process is signalled and no scan is started. A part it cannot establish (the webapp down, no psutil) reads `unknown (<why>)`, never an idle or zero figure.
