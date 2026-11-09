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

The fleet budget is fewer than 15 e2e tests; this suite holds one `test_*` per user-visible flow, listed by `pytest tests/e2e --co -q`. A flow test takes the `run_flow` fixture from `tests/e2e/conftest.py` and hands it plain `scenario_*` functions. Each scenario gets its own browser context, page and (when it asks for `launched_pty_session`) PTY session, so route mocks and storage never leak between scenarios, exactly as when each was a separate test. A scenario that fails does not stop the others: the flow fails once, at the end, with every failing scenario's name and traceback; a flow whose scenarios all skip is skipped. Each flow carries `@pytest.mark.timeout(FLOW_TIMEOUT_S)` because the 120 s per-test cap is sized for one scenario.

Add a browser check as a `scenario_*` function in the flow it belongs to, never as a new `test_*`. Checks that read one page's geometry from a single boot (the Coding row layout) are one test that shares the page.

## Dev-loop commands

- Smoke against the live tray: `.\scripts\run-e2e.ps1` (sets `LAUNCHER_E2E_LIVE=1`). A bare `pytest tests/e2e` without opt-in env vars exits with a guard.
- Non-browser suite alone: `pytest tests -m "not smoke" -v`.
