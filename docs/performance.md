# Performance

**Settings > Performance > Speed limits** holds every limit that decides how hard Lite works a PC: how often the page asks for fresh data, how many threads each kind of read may use, and how long a cache is trusted. A slow computer wants other values than a fast one, so each limit is a field the owner changes in Settings, with no file edit and no restart.

## Presets

| Preset | For | Values |
|---|---|---|
| **Standard** | an ordinary PC | every default in the table below |
| **Light machine** | a slow PC with several lanes | refreshes spaced out, smaller pools, longer caches, budgets doubled |
| **Custom** | shown when the saved values match neither preset | your own |

Choosing a preset saves every limit in one write. Editing any single field afterwards reads **Custom**; Custom is a state, so choosing it does nothing.

## The limits

The table lives in `src/webapp_settings/performance.py`: the config defaults, the validation, both presets and the Settings sheet all read it, so a limit added there appears in every one of them. Each is a field of `config/webapp_config.json`.

| Field | Default | Light | Range | What it controls |
|---|---|---|---|---|
| `poll_board_s` | 5 | 15 | 2–120 s | How often the Board refreshes while open. |
| `poll_sessions_s` | 5 | 15 | 2–120 s | How often the running-session list refreshes. |
| `poll_transcript_s` | 3 | 8 | 1–60 s | How often an open chat asks for new turns. |
| `poll_pause_hidden` | on | on | on/off | A hidden tab or locked phone asks for nothing and reads once on return. Applies to every page poll. |
| `pool_hot_poll` | 8 | 4 | 4–32 | Threads building the session list and the Board. |
| `pool_session_reads` | 12 | 6 | 4–48 | Threads reading open chats. |
| `pool_sends` | 0 (automatic) | 8 | 0 or 4–64 | Threads behind sending to a session and every route without its own pool; 0 is CPUs + 4, at most 32. |
| `pool_git_status` | 8 | 2 | 1–32 | Repos scanned in parallel. |
| `git_status_ttl_s` | 10 | 30 | 2–60 s | How long a git scan is served as it is; past it the next poll shows it at once and refreshes it behind the response. |
| `transcript_page_cache_size` | 32 | 16 | 0–256 pages | Parsed chat pages kept for unchanged files; 0 turns the cache off. |
| `transcript_resolve_miss_ttl_s` | 10 | 30 | 1–120 s | How long a session whose Copilot log was not found answers not found before the folder is scanned again. |
| `request_budget_scale` | 1.0 | 2.0 | 0.5–10 | Multiplies every request class's soft and hard budget ([diagnostics](diagnostics.md#request-timings-and-budgets)), so a slower PC does not log a breach for every ordinary request. Per-class values stay in `request_budgets`. |

A save outside a range is refused with the rule it broke ("Board refresh must be a whole number between 2 and 120 s; got 1") and changes nothing. A hand-edited file with a value out of range starts anyway: that value is put back to its default and a warning names it.

## How a change takes effect

- **Poll spacings and the hidden-tab pause** are read from the page's copy of the config at each re-arm, so a running poll picks up a change at its next tick. A page that has not loaded its config yet uses the defaults.
- **Pools** are `TimedExecutor`s (`src/request_timing.py`) built once with a fixed thread ceiling and a live concurrency limit. A save resizes the limit (`app/webapp/perf_apply.py`); a call already running finishes, so a smaller limit is met as running calls end. The session-host is never touched.
- **Caches, budgets and the git TTL** are read off the config on each use.

## The health line

Under the presets one read-only line shows whether the box is coping: CPU, free memory, requests in flight (and the peak over five minutes), coding-agent processes, and live lanes against the chief's worker cap. It reads `GET /api/performance/health`, refreshes every few seconds while the sheet is open, starts no scan and signals no process. A figure that could not be read says `unknown (<why>)`, never zero.

A lane is a chief-managed session whose hook state row is still live.
