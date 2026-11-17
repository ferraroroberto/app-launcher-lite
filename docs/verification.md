# Verification

The pre-ship gate and its browser routing. `CLAUDE.md` keeps the one-line rule (run the gate before declaring a webapp, launcher or session-host change done); this page holds the procedure. The structure, instruction, doc-refs, dead-code, conventions and comment ratchets and the screenshot baseline have their own bullets in `CLAUDE.md`.

## The gate

Before declaring any webapp, launcher or session-host change done, run the pre-ship gate:

```
C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -File scripts/verify-before-ship.ps1
```

It boots its own disposable webapp and session-host, so no tray is needed.

The last stdout line of a run is `GATE-RESULT: PASS` or `GATE-RESULT: FAIL` (failed stage, missing `.venv`, unhandled error, box lock timeout); exit codes are 0 and 1 (3 for the lock timeout). Never pipe the gate when its exit code matters (`| tail` reports `tail`'s status); a caller that must pipe asserts on that line, and a run without one is not a pass. `tests/unit/tooling/test_gate_result.py` pins it.

## Stages that always run

- **Byte-compile** and the **non-e2e pytest suite**.
- **Static ratchet.** `scripts/static_ratchet_gate.py` around the vendored `scripts/static_ratchet.py` fails on any duplicate block, unused import or variable (`ruff` `F401,F841`), or likely bug or security finding (a curated set of `ruff` `B` and `S` rules, `S113` strict) that is not in `.static-ratchet-baseline.json`. Exit 3 means a detector such as Node/`npx` or ruff could not run; that fails as NOT CONFIRMED, never a skip. It needs `ruff` in `.venv`.
- **Docs ratchet.** `scripts/docs_ratchet_gate.py` around the vendored `scripts/docs_ratchet.py` fails when `README.md` is over its size budget, or when `README.md` or `docs/**/*.md` carries a new history marker (an issue reference, a dated heading or a changelog heading) beyond `.docs-ratchet-baseline.json`. Exit 3 means a file could not be read; that fails as NOT CONFIRMED.

## Browser slice

The browser slice is diff-proportionate, routed against `main` by the vendored `scripts/classify_e2e.py` from the `[e2e]` rule table in `.fleet.toml` (`tests/unit/tooling/test_classify_e2e.py` pins the table):

- static-asset-only diff → Chromium-only smoke;
- backend-only or docs-only diff → no browser suite;
- any `.js`/`.css`/real-page/session-host/e2e-test change, or any ambiguous diff → the full suite, Chromium driving both a desktop and a phone (Android device-emulated) projection. This is an Android-only fork: no WebKit or iPhone engine.

Routing is fail-safe: uncertainty escalates to full, never narrows. The chosen tier prints to the console and to `webapp/verify-progress.log`. Separately, `scripts/session_host_touched.py` reads CLAUDE.md's `## session-host` block and makes the gate warn when the diff touched a path the `:8466` session-host loads, whatever tier it routed to.

## The e2e budget

The fleet budget is fewer than 15 e2e tests; this suite holds one `test_*` per user-visible flow, listed by `pytest tests/e2e --co -q`. A flow test takes the `run_flow` fixture from `tests/e2e/conftest.py` and hands it plain `scenario_*` functions. Each scenario gets its own page, a context reset to a fresh one's state (see the next section) and, when it asks for `launched_pty_session`, its own PTY session, so route mocks and storage never leak between scenarios, exactly as when each was a separate test. A scenario that fails does not stop the others: the flow fails once, at the end, with every failing scenario's name and traceback; a flow whose scenarios all skip is skipped. Each flow carries `@pytest.mark.timeout(FLOW_TIMEOUT_S)` because the 120 s per-test cap is sized for one scenario.

Add a browser check as a `scenario_*` function in the flow it belongs to, never as a new `test_*`. Checks that read one page's geometry from a single boot (the Coding row layout) are one test that shares the page.

## Keep the harness off the loopback port pool; a drained pool names itself

Every closed connection to a local server leaves an (ephemeral port, server port) pair in TIME_WAIT for about two minutes, and Windows fails a `connect()` that lands on one with `net::ERR_ADDRESS_IN_USE` instead of trying another port. A browser context opens its own connections to the app (one HTTP/2 connection plus a socket per WebSocket), so a fresh context per scenario spends a pair per scenario from a pool every other lane's gate draws on too.

- **One context per distinct context arguments per projection.** `run_flow` leases the scenario's context from `tests/e2e/_shared_context.py`, which resets it after the scenario: pages closed, routes, cookies and permissions cleared, and the app origin's storage and service workers cleared through a routed scratch page, which opens no socket to the app. The bearer token is seeded once per context as an init script, not per scenario.
- **State a reset cannot undo marks the context.** An init script, the page clock, a WebSocket route, offline mode, geolocation, extra headers, an exposed function or a context listener set on the *context* makes that context close after the scenario instead of being reset, so no scenario inherits it and none has to declare it. Page-level calls (`page.add_init_script`, `page.route_web_socket`) die with the page and cost nothing. A reset that raises discards the context too. `shared contexts: N created for M scenarios` closes every run; the screenshot baseline builds its own pinned-clock contexts.
- **Keep-alive.** The app speaks HTTP/2 on one connection per context, and the harness's own HTTP calls (health polling, session launch and stop) go through `src/_loopback_http.py::pooled_request`, one pooled session, instead of a connection per call.
- **A drained pool reads as one.** A failure whose text carries `ERR_ADDRESS_IN_USE` or `connect ENOBUFS` leads with `PORT POOL EXHAUSTED`, and the run ends with a section of that name listing the tests (`tests/e2e/_port_pool.py`), so a gate reader waits out the TIME_WAIT pairs and re-runs instead of debugging a page.
- **Measure before cutting.** Sample `netstat -ano -p tcp` once a second through one full run and count each (local, remote) TIME_WAIT pair once, each time it appears, grouped by server port; count only the disposable app's own ports, since other apps on the box add their own. Never change the dynamic port range or `TcpTimedWaitDelay` to hide it.

## Dev-loop commands

- Smoke against the live tray: `.\scripts\run-e2e.ps1` (sets `LAUNCHER_E2E_LIVE=1`). A bare `pytest tests/e2e` without opt-in env vars exits with a guard.
- Non-browser suite alone: `pytest tests -m "not smoke" -v`.
