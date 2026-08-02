@echo off
chcp 65001 >nul
REM ============================================================================
REM  SETUP - one-shot installer for a fresh clone
REM ----------------------------------------------------------------------------
REM  1. Creates .venv (if missing).
REM  2. Installs Python deps from requirements.txt.
REM  (PWA/tray icons are committed in the repo -- no generation step needed.)
REM  After this runs once, `tray.bat` is enough for day-to-day use.
REM ============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b 1

set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [1/2] Creating .venv...
    python -m venv .venv || exit /b 1
)

echo [2/2] Installing Python requirements...
"%VENV_PY%" -m pip install --upgrade pip || exit /b 1
"%VENV_PY%" -m pip install -r requirements.txt || exit /b 1

echo.
echo ============================================================================
echo  Setup complete. Start the tray with:  tray.bat
echo  Or run the webapp standalone:        webapp.bat
echo ============================================================================
exit /b 0
