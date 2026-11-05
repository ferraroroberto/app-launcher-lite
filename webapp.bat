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
set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [ERROR] .venv missing. Run setup.bat first.
    exit /b 1
)

cd /d "%SCRIPT_DIR%" || exit /b 1

REM  The port is config\config.json webapp.port (default 8465 when unset) -- the
REM  same single setting the tray and the Python code read.
set "WEBAPP_PORT=8465"
for /f "usebackq delims=" %%P in (`"%VENV_PY%" -m scripts.webapp_port`) do set "WEBAPP_PORT=%%P"

set "CERT_DIR=%SCRIPT_DIR%webapp\certificates"
set "CERT=%CERT_DIR%\cert.pem"
set "KEY=%CERT_DIR%\key.pem"

REM Auto-renew a Tailscale cert expiring within 30 days (no-op on a
REM self-signed cert or when no cert exists) — project-scaffolding#89.
"%VENV_PY%" "%SCRIPT_DIR%scripts\gen_tailscale_cert.py" --check

REM --loop keeps uvicorn off the default Windows proactor loop, whose accept
REM path dies on a single aborted client connection (WinError 64) -- #388.
REM Keep in sync with app\webapp\event_loop.py:LOOP_FACTORY.
set "LOOP_ARG=app.webapp.event_loop:selector_loop_factory"

if not exist "%CERT%" (
    echo [INFO] No HTTPS cert found, running HTTP-only on :%WEBAPP_PORT%.
    echo        Run scripts\gen_tailscale_cert.py, or drop any cert.pem + key.pem
    echo        pair into webapp\certificates\, to enable HTTPS.
    "%VENV_PY%" -m uvicorn app.webapp.server:app --host 0.0.0.0 --port %WEBAPP_PORT% --loop "%LOOP_ARG%"
) else (
    echo [INFO] HTTPS via %CERT%
    "%VENV_PY%" -m uvicorn app.webapp.server:app --host 0.0.0.0 --port %WEBAPP_PORT% --ssl-keyfile "%KEY%" --ssl-certfile "%CERT%" --loop "%LOOP_ARG%"
)

exit /b %ERRORLEVEL%
