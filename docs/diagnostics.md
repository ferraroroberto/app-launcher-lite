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
