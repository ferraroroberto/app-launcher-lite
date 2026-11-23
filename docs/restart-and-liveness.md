# Restart and liveness verification

The hand-off procedure for making a verified change actually live, and for
proving it is. `CLAUDE.md` (`## This repository`) carries the one-line recipe
and points here; the `:8466` session-host's machine-parsed declaration (path
list, update command, liveness signal) stays in `CLAUDE.md`'s `## session-host`
block, which this doc does not restate.

## Restart the `:8465` webapp before hand-off

The running webapp has no hot-reload — code edits do nothing until the `:8465`
process is restarted.

1. After the pre-ship gate (`scripts/verify-before-ship.ps1`) passes, restart
   the webapp so the user can immediately test on the phone, *unless they said
   not to*.
2. The canonical restart is **`tray.bat --restart`** — orphan-proof
   reclaim-then-start: it kills the tray subtree (tray + webapp `:8465` +
   cloudflared), reclaims `:8465` by PID scoped to this repo's `.venv`
   (CommandLine-matched), then starts fresh. Run `--restart`, don't hand-roll
   the kill.
3. **`:8466` session-host is excluded from that reclaim and survives**
   (mechanism: `CLAUDE.md`'s `## session-host`), so `--restart` preserves open
   Coding/PTY sessions — including one hosted on `:8466` itself — is safe to
   run from inside such a session, and never restarts `:8466`. A reclaim owns a
   port holder whose command line is under this repo's `.venv` **or** is this
   repo's `launcher.py session-host` (a host re-execed from the base interpreter
   carries no `.venv` path), and it exits 1 when an owned holder is still alive
   after the stop, so a restart that did not replace the old process fails
   instead of reporting success. A tray, webapp or session-host started from one
   of this repo's own `-wt-<N>` worktrees counts as this repo's too: the helper
   resolves those worktrees from `git worktree list` (a lookalike directory git
   does not list is never claimed), stops their trays with the primary's, and
   names the checkout of each process it reclaims. When git cannot list the
   worktrees it says so and claims none.
4. Run the restart from **PowerShell**, wrapped by the replacement check:

   ```
   .\.venv\Scripts\python.exe -m src.launching.restart_check snapshot
   tray.bat --restart
   .\.venv\Scripts\python.exe -m src.launching.restart_check verify
   ```

   `verify` polls `GET /api/version` (60 s by default) and exits 0 only when
   the answer carries a **different `pid`** than the snapshot **and** a
   `git_sha` matching `HEAD`. A matching `git_sha` alone proves nothing: a
   restart that never ran leaves the old process serving, and when that
   process already loaded `HEAD` the sha matches anyway. Exit 1 = the process
   was not replaced (or nothing answers); exit 2 = no baseline, so a restart
   cannot be confirmed. Neither is a pass. Report the `verify` line.
5. From Git Bash, `cmd.exe /c "tray.bat --restart"` never enters the script:
   MSYS rewrites `/c`, cmd opens an interactive prompt, and the call exits 0
   with no output and nothing restarted. Use PowerShell (or `cmd.exe //c`).
   `tray.bat` appends `entered` and `helper exit=<code>` lines to
   `webapp/restart.log`, as does `restart_check`, so a restart with no
   `entered` line was never run.
6. `built_at` is the webapp process's start time, captured at import; it moves
   only when a webapp process starts. It does not distinguish a tray restart
   from a webapp-only restart (the tray's Restart webapp item, a self-heal),
   so it is no proof that `tray.bat --restart` ran. `pid` is.
7. Don't hand off "done" with a stale process still serving.

The `:8466` restart, `.\.venv\Scripts\python.exe -m src.launching.ps_script scripts\restart-session-host.ps1 -Confirm`
(the launcher starts it under any execution policy, `AllSigned` included), ends with
`python -m src.launching.restart_check session-host` instead of a loop of its own. It reads
`/api/version` over https and falls back to http (the tray serves plain http when no TLS cert
exists), and ends in one of three states, each logged to `webapp/restart.log`: **live** (exit 0:
`freshness` is `fresh` or `stale-irrelevant`, reported the moment it is seen however long the
cold start took), **not confirmed** (the host answers but `freshness` is anything else) and **not
answering** (the host never answered, or no webapp did), the last two exit 1 with the time waited.
The deadline is 45 s (`SESSION_HOST_WAIT_S`): the host itself answers about 2 s after its spawn,
and the rest is the webapp's `/api/version` snapshot, up to 15 s old.

## A session-host change is not live until `:8466` itself restarts

A diff touching `src/session_host.py` or `app/webapp/session_host/` can merge, pass a
fully green gate, and go through a correct `tray.bat --restart` while the
running session-host keeps executing days-old code, silently.

**Before reporting a session-host-path change as shipped**, read
`GET /api/version`'s `session_host` block —
`{"reachable", "git_sha", "started_at", "stale", "stale_relevant", "freshness"}`:

- `git_sha` is the *loaded* SHA (captured once at that process's own start, not
  live git state).
- `stale` means it differs from the repo's resolved `origin/main` tip,
  re-resolved fresh on every call — **not** the primary checkout's current
  branch, which can transiently sit on an unrelated feature branch while a
  worker occupies the tree, under-reporting `stale_relevant` as `false`.
- `stale_relevant` scopes that to whether a declared session-host path was
  actually touched between the loaded SHA and that resolved ref.
- Both read `null` (never a confident false) when either SHA, or the diff
  itself, can't be resolved.
- `freshness` folds the three into one declared state: `unreachable`
  (no answer from `:8466`), `unknown` (a SHA couldn't be resolved), `fresh`
  (loaded SHA equals the resolved ref), `stale-irrelevant` (differs, but no
  declared path was touched), `stale-relevant` (differs and a declared path
  was touched) and `stale-unknown` (differs, but the scoped diff couldn't run).
  The unknown states are never folded into `fresh`.

**Gate on `freshness`, not raw `stale`** — `stale-relevant` means **report the
change as merged but not yet live**, never as shipped; `stale-irrelevant`
means nothing to restart for; `unknown` and `stale-unknown` mean it could not
be confirmed either way.

**Do not restart `:8466` to "fix" this** as a side effect of finishing an
issue: it kills every live PTY on the machine. The one supported restart path is the `update command` in `CLAUDE.md`'s
`## session-host` block.

## The declared path list and the restart command

`CLAUDE.md`'s `## session-host` block is the machine-read declaration, so its bullets stay there; this section holds the detail behind two of them.

**Path list.** The list is the session-host server module's first-party import closure. `src.session_host_paths.session_host_import_closure()` computes it statically from the import graph, and `tests/unit/session_host/test_session_host_paths.py` fails when the declaration drifts from it, so add a module to the block in the same change that makes the session-host import it. Out of scope: the argparse bootstrap above the server module (`launcher.py` to the CLI dispatcher). Deliberately kept off the closure: `src/console_input.py`, which `RemoteSession.submit_input` runs as a fresh `python -m` subprocess per message, so a fix there is live on the next send without a `:8466` restart.

**Update command.** `scripts/restart-session-host.ps1 -Confirm` works under `AllSigned`. It wraps `tray_lifecycle.ps1 reclaim -Ports 8466`, then `tray.bat --restart`, then the `restart_check session-host` confirmation described above.
