# Verification

The pre-ship gate and its browser routing. `CLAUDE.md` keeps the one-line rule (run the gate before declaring a webapp, launcher or session-host change done); this page holds the procedure. The structure, instruction, doc-refs, dead-code, conventions and comment ratchets and the screenshot baseline have their own bullets in `CLAUDE.md`.

## The gate

Before declaring any webapp, launcher or session-host change done, run the pre-ship gate:

```
C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -File scripts/verify-before-ship.ps1
```

It boots its own disposable webapp and session-host, so no tray is needed.

## Stages that always run

- **Byte-compile** and the **non-e2e pytest suite**.
- **Static ratchet.** `scripts/static_ratchet_gate.py` around the vendored `scripts/static_ratchet.py` fails on any duplicate block, unused import or variable (`ruff` `F401,F841`), or likely bug or security finding (a curated set of `ruff` `B` and `S` rules, `S113` strict) that is not in `.static-ratchet-baseline.json`. Exit 3 means a detector such as Node/`npx` or ruff could not run; that fails as NOT CONFIRMED, never a skip. It needs `ruff` in `.venv`.
- **Docs ratchet.** `scripts/docs_ratchet_gate.py` around the vendored `scripts/docs_ratchet.py` fails when `README.md` is over its size budget, or when `README.md` or `docs/**/*.md` carries a new history marker (an issue reference, a dated heading or a changelog heading) beyond `.docs-ratchet-baseline.json`. Exit 3 means a file could not be read; that fails as NOT CONFIRMED.

## Browser slice

The browser slice is diff-proportionate, routed by `scripts/classify_e2e.py` against `main`:

- static-asset-only diff → Chromium-only smoke;
- backend-only or docs-only diff → no browser suite;
- any `.js`/`.css`/real-page/session-host/e2e-test change, or any ambiguous diff → the full suite, Chromium driving both a desktop and a phone (Android device-emulated) projection. This is an Android-only fork: no WebKit or iPhone engine.

Routing is fail-safe: uncertainty escalates to full, never narrows. The chosen tier prints to the console and to `webapp/verify-progress.log`.

## Dev-loop commands

- Smoke against the live tray: `.\scripts\run-e2e.ps1` (sets `LAUNCHER_E2E_LIVE=1`). A bare `pytest tests/e2e` without opt-in env vars exits with a guard.
- Non-browser suite alone: `pytest tests -m "not smoke" -v`.
