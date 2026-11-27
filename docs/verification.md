# Verification

The test and gate reference. `CLAUDE.md` names the one command that decides whether a change is done; this file explains what it runs and why.

## Pytest suite

An in-process FastAPI `TestClient` suite under `tests/` covers the webapp's API surface (config, auth, Apps, Jobs, Board, coding-agent detection and launch) plus unit tests by domain and a few static convention guards that parse the tree rather than exercise it (every `#i-NAME` icon resolves to a sprite symbol; every `subprocess.*` spawn carries the no-window flag).

```powershell
& .\.venv\Scripts\python.exe -m pytest tests -m "not smoke" -v
```

Every `glab` call and every session-host call is mocked at the client seams, so the suite runs on a machine with no `glab` and no `copilot` installed. GitLab response shapes are pinned in the `tests/unit/board/test_gitlab_client.py` fixtures; the suite never reads a live GitLab, so [gitlab.md](gitlab.md#verified-live-and-not) says which shapes are verified against one.

Runtimes live in `CLAUDE.md` and only there: they are a property of the machine measuring them, and a copy here would go stale unnoticed. `-m "not smoke"` excludes the live-tray Playwright suite below.

## Playwright suite

A `pytest-playwright` suite under `tests/e2e/` holds a boot smoke test and one focused regression test per behaviour worth pinning, in one folder per tab (`apps/`, `board/`, `chat/`, `coding/`, `jobs/`, `session/`, `settings/`, `terminal/`, ...). Every test runs on Chromium-desktop; tests whose outcome depends on the engine or the phone shape also run on WebKit projected onto an iPhone viewport (the `iphone` marker). Real Android Chrome is checked on a device, not here: see [android.md](android.md).

One-time setup:

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m playwright install chromium webkit
```

Dev loop, with the tray up (`tray.bat`):

```powershell
.\scripts\run-e2e.ps1                       # both projections
.\scripts\run-e2e.ps1 --browser chromium    # the faster loop
```

The dev loop targets the live tray on `https://127.0.0.1:8465` and boots nothing itself; if the tray is not up every test is skipped with a message. Because that instance is the one the phone uses, targeting it is an explicit opt-in (`LAUNCHER_E2E_LIVE=1`, which `run-e2e.ps1` sets): a bare `pytest tests/e2e` exits with a guard message instead of driving a live instance. Loopback access bypasses the bearer-token middleware and the passkey gate.

Terminal tests get a live PTY from `launched_pty_session`, a deterministic lightweight stub child that needs only Python, so the suite does not depend on `copilot` being installed. `launched_claude_pty_session` (a real agent child, for the few assertions that depend on the real agent's rendered output) skips on a machine without the CLI.

Byte loss at the PTY write boundary and concurrent first paint have non-browser guards in the pytest suite (`tests/unit/session_host/`, Windows/pywinpty-gated). Leaked browser helpers, stub children and disposable servers are swept after every session (`tests/e2e/_browser_sweep.py`, `tests/e2e/_process_teardown.py`, `tests/e2e/_owned_processes.py`).

The reasoning behind the suite's conventions (mocking boot fetches, the fake clock, `stable_read`, the port-pool guard) is in [e2e-conventions.md](e2e-conventions.md).

## Mac nightly

The fleet's Mac Mini nightly (project-scaffolding `docs/mac-runner.md`) runs this repo from the `[ci.nightly]` table in `.fleet.toml`: the unit leg (`pytest tests --ignore=tests/e2e -m "not windows_only"`) and the e2e leg (`pytest tests/e2e --e2e-autoboot --browser webkit`, the iPhone projection). Both run the disposable webapp and the API tests on macOS; only what needs Windows carries `@pytest.mark.windows_only` (registered in `pytest.ini`):

- ConPTY: the disposable session-host spawns PTYs through `pywinpty`, so every e2e test that takes `launched_pty_session` or `launched_claude_pty_session` is marked by `tests/e2e/conftest.py` itself, with no per-test decorator to forget.
- `.bat` and tray recipes, the Apps tab's refresh route, OEM-codepage decoding, the effective-`PATH` registry merge, drive-letter and `%VAR%` path semantics, and the Win32 cursor reader.

`-m "not windows_only"` is the unit leg's exclusion. The e2e leg passes no `-m`, so off Windows `tests/e2e/conftest.py` deselects the marked tests during collection; the local gate runs on Windows and keeps them. List what is marked with `pytest tests --ignore=tests/e2e -m windows_only --collect-only -q`; e2e is listed with `pytest tests/e2e --e2e-autoboot --browser chromium --browser webkit -m windows_only --collect-only -q` on Windows.

A test that fails only off Windows because it pins a Windows path by accident is made portable instead of marked. The simulator smoke (`views`) is not enabled: it needs a `[design.review.synthetic] command` that boots without a PTY session.

## Design review on synthetic data


- **Code:** a `git archive` copy of `HEAD` on free ports, so the checkout is never written.
- **Data:** synthetic only: a demo project, one stub PTY session with a synthetic transcript, one job and one app. Config paths and the user profile point at the temp tree.
- **Lifecycle:** it prints `URL=` once ready and, when its stdin closes, stops its servers and deletes the temp tree. The live tray and the `:8466` session-host are never touched.

## Pre-ship gate

`run-e2e.ps1` is the dev loop: fast, but it skips the whole browser suite when the tray is not up, which is the wrong default for a final check (a forgotten tray looks like a green run). The gate closes that hole:

```powershell
C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -File scripts\verify-before-ship.ps1
```

Run it before declaring any change to `app/webapp/`, `src/launching/launcher.py` or the session-host done. It runs, as one pass/fail, in order:

1. **Byte-compile** `app`, `src` and `tests`.
2. **Ratchets**, each failing only when a count rises above its committed `.<name>-ratchet-baseline.json`: comment, suite weight, static, docs, structure, instruction, doc refs, dead code, conventions, runtime write, usage track, UX surface, request budget, windows only, port host, install standard (`.ratchets/install-standard.json`, empty: the four vendored installer modules, the `[install.migrate]` and `[install.endpoint]` tables and the three sections of [install.md](install.md)) and clean tree (a disposable plain-HTTP boot on a free port with scratch runtime data; it never touches the tray). Every ratchet reads the whole tree and is never diff-routed. Exit 3 means the ratchet could not establish its fact (git unusable, a file unreadable) and fails as NOT CONFIRMED, never as a skip. Where code goes: [layout.md](layout.md).
3. **Runtime write probe** snapshot, then the **non-e2e pytest suite**.
4. **Browser slice**, routed to the diff (below), against a disposable webapp and session-host. Right after it, **request budget on WebKit** (`scripts/tools/webkit_budget_check.py`, skipped for an inert-asset diff) reruns the budget tests on the engine the Mac nightly uses, which the Chromium slice never exercises. Exit 4 (WebKit not installed) and 5 (no budget test) print `[NOT RUN]`, never a pass; any other non-zero exit, an unconfirmed run included, fails the gate.
5. **Screenshot baseline**, the last browser stage.
6. **Orphan sweep and runtime write probe**: no disposable server may outlive the run, and no test run may leave a file under a code folder.

The last line is always `GATE-RESULT: PASS` or `GATE-RESULT: FAIL`, on every path the script can still run code on. Never pipe the gate when its exit code matters: a pipeline reports its last command's status, so a trailing `tail` turns a failed gate into exit 0. Redirect to a file and read the file; a caller that cannot avoid a pipe asserts on the final `GATE-RESULT:` line.

```bash
C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -File scripts/verify-before-ship.ps1 > gate.log 2>&1; echo "exit=$?"
tail -40 gate.log
```

### Execution policy `AllSigned`

`-File` refuses an unsigned script under `AllSigned` (or `Restricted`), and `-ExecutionPolicy Bypass` would weaken the machine's own control, so no repo script passes it. `src/launching/ps_script.py` is the one place a repo `.ps1` is started: `tray.bat`, the tray's port reclaim and `restart-session-host.ps1` go through it. It reads the effective policy; one that lets unsigned local files run (`Bypass`, `Unrestricted`, `RemoteSigned`) gets the plain `-File` launch, anything else (or a policy it could not read, reported as such) loads the script text and runs it as a ScriptBlock, which is not a file execution, so the policy stays as set. On that route `$PSScriptRoot` is rewritten to the script's folder, and the gate dot-sources its sibling scripts as text for the same reason.

To run the gate under `AllSigned`, for this one process tree only (the machine and user policy are never touched):

```powershell
$env:PSExecutionPolicyPreference = "AllSigned"
.\.venv\Scripts\python.exe -m src.launching.ps_script scripts\verify-before-ship.ps1 > gate.log 2>&1; echo "exit=$?"
```

A ScriptBlock gate has no script path of its own, so the shared box load lock is not taken: the gate prints `box load lock NOT CONFIRMED` and runs unlocked. Every other stage runs.

### What the disposable instance isolates

- A tray on `:8465` may be running or not. Autoboot picks a free port for its own webapp and always spawns its own disposable session-host on another free port, never the live `:8466`, whose sessions are real Copilot PTYs. The tray is left untouched.
- It serves HTTPS with `webapp/certificates/` when a cert pair exists, plain HTTP otherwise. Subprocess output goes to `webapp/e2e-autoboot-*.log`.
- It reads and writes its own temp config (`LAUNCHER_WEBAPP_CONFIG`), built from a per-run disposable `auth_token` and pinned paths, so no test can mutate `config/webapp_config.json` and no live credential reaches a test run.
- It scans only the launch-target checkout for projects, so the Coding list is deterministic.
- Its audit trail (`webapp/sessions`) and upload directory are redirected so a throwaway session never lands in the real ones.
- No gate run reaches a real forge: forge calls are mocked or answered by synthetic rows, so no real issue title enters a screenshot.

### Routing the browser slice

Byte-compile, the ratchets and the non-e2e suite always run. Only the browser slice scales with what the branch changed, classified by `scripts/classify_e2e.py` against `main` from the rules in `.fleet.toml` `[e2e]`:

- **skip**: every changed path is declared to have no browser impact (docs, backend-only code outside the session-host and launcher closure).
- **static**: the worst path is an inert asset; a narrow smoke target runs.
- **surface**: the diff would route full but every full path belongs to one declared `[[e2e.surface]]`; only that surface's tests run.
- **full**: any path matches a full rule, any path matches no rule, or the diff is empty or mixed. Uncertainty always escalates, never narrows.

The full and surface tiers run on parallel `pytest-xdist` workers, each with its own disposable webapp and session-host, then a short serial pass for the tests marked `serial`. A browser test that timed out under load gets one retry; a timeout that passes on retry is listed as flaky in the output. Two full-tier gates in sibling worktrees queue on a machine-wide mutex rather than overlap. The e2e suite size is a ratchet: `.fleet.toml` `[e2e] test_budget` equals the collected node count and moves only with a written reason.

### Screenshot baseline

The last stage runs `scripts/shot_baseline.py` (with `tests/e2e/_shot.py`) over `tests/e2e/shots`: the five work tabs at phone and desktop width, light and dark, captured by `tests/e2e/screens/test_screen_shots.py` from route-mocked fictional data, local gate only. A move-only change touches no PNG and ends with `shot baseline: PASS -- N of N committed shot(s) recaptured, 0 drifted`. A deliberate UI change re-baselines in the same change:

```powershell
$env:LAUNCHER_E2E_AUTOBOOT = "1"; & .\.venv\Scripts\python.exe -m pytest tests/e2e/screens/test_screen_shots.py
git diff --stat -- tests/e2e/shots
```

then stage the changed PNGs. A drift that is not deliberate means something on screen is unpinned: fix the capture, never widen the bands. Exit 3 is NOT CONFIRMED and fails.

### Gate runtime and workers

Gate runtime is a property of the host measuring it: re-measure before trusting any figure. Worker counts live in `scripts/e2e-gate-route.ps1`: `$UnitDefaultWorkers` (override `UNIT_WORKERS`) for the non-browser phase and `$E2EDefaultWorkers` (override `E2E_WORKERS`; `1` runs serially) for the browser slice. The disposable webapp stays scoped to the launch target (`pin_launch_target`), so the Coding list does not scan the whole projects folder. Tests marked `serial` run after the workers finish. A run that looks wedged is read from the progress log below: every `START` without a matching `DONE` is a test still in flight, so read them all before killing anything.

### Progress log

The gate writes `webapp/verify-progress.log` (gitignored, overwritten each run, worker lines tagged `[gw1]`): phase markers plus one `START`/`DONE` line per test with its total including fixture cost, and a slowest-tests summary. If an outer timeout kills the gate, the last lines name the active phase and test node. Recent browser-leg runs are also appended to `webapp/verify-progress-history.log` in the primary checkout.

### After the gate: restart and confirm

The gate proves the code, not that the running process has it. The webapp has no hot reload: restart it with `tray.bat --restart` and confirm with a bounded poll of `GET /api/version` that `git_sha` matches `HEAD` and `asset_hash` changed. A session-host change is not live until the `:8466` process itself restarts, which `tray.bat --restart` never does: read `session_host.stale_relevant` from the same endpoint. See [restart-and-liveness.md](restart-and-liveness.md).
