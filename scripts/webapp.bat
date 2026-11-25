@echo off
chcp 65001 >nul
REM ============================================================================
REM  WEBAPP - standalone FastAPI launcher (HTTPS when cert present)
REM ----------------------------------------------------------------------------
REM  Daily use: launch tray.bat instead — it adopt-or-spawns the webapp for
REM  you. This bat is for headless boxes, dev iteration, or when you want
REM  the webapp without the tray icon.
REM ============================================================================

setlocal
for %%I in ("%~dp0..") do set "SCRIPT_DIR=%%~fI\"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [ERROR] .venv missing. Run setup.bat first.
    exit /b 1
)

cd /d "%SCRIPT_DIR%" || exit /b 1

REM  The port is config\config.json webapp.port (the resolver supplies its own
REM  default when unset) -- the same single setting the tray and the Python code
REM  read. No fallback is written here: a resolver that fails stops the start.
set "WEBAPP_PORT="
for /f "usebackq delims=" %%P in (`"%VENV_PY%" -m scripts.webapp_port`) do set "WEBAPP_PORT=%%P"
if not defined WEBAPP_PORT (
    echo [ERROR] scripts.webapp_port printed no port. Check config\config.json.
    exit /b 1
)

set "CERT_DIR=%SCRIPT_DIR%webapp\certificates"
set "CERT=%CERT_DIR%\cert.pem"
set "KEY=%CERT_DIR%\key.pem"

REM Auto-renew a Tailscale cert expiring within 30 days (no-op on a
REM self-signed cert or when no cert exists) — project-scaffolding#89.
"%VENV_PY%" "%SCRIPT_DIR%scripts\gen_tailscale_cert.py" --check

REM app.webapp.serve is the one launch entry: HTTP/2 over TLS, on the selector
REM event loop (the proactor loop's accept path dies on one aborted client).
REM --loop adds the GIL switch-interval tuning (app\webapp\event_loop.py).

if not exist "%CERT%" (
    echo [INFO] No HTTPS cert found, running HTTP-only on :%WEBAPP_PORT%.
    echo        Run scripts\gen_tailscale_cert.py to enable HTTPS.
    "%VENV_PY%" -m app.webapp.serve --host 0.0.0.0 --port %WEBAPP_PORT% --loop app.webapp.event_loop:selector_loop_factory
) else (
    echo [INFO] HTTPS via %CERT%
    "%VENV_PY%" -m app.webapp.serve --host 0.0.0.0 --port %WEBAPP_PORT% --loop app.webapp.event_loop:selector_loop_factory --ssl-keyfile "%KEY%" --ssl-certfile "%CERT%"
)

exit /b %ERRORLEVEL%
