@echo off
chcp 65001 >nul
REM ============================================================================
REM  SETUP - one-shot installer for a fresh clone
REM ----------------------------------------------------------------------------
REM  1. Creates .venv (if missing).
REM  2. Installs Python deps from requirements.txt.
REM  3. Offers to install the Copilot hooks and skills (copilot\install.ps1).
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
echo The Copilot side (the session-state and chief hooks, and the issue skills) installs
echo into ~\.copilot. It writes nothing else: not ~\.claude, no trust or agent settings.
echo Coming from fleet-config-lite? Answer n and follow
echo docs\migrating-from-fleet-config-lite.md instead.
set "ANSWER="
set /p "ANSWER=Install it now? [y/N] "
if /i not "%ANSWER%"=="y" goto copilot_skipped
"%VENV_PY%" -m src.launching.ps_script copilot\install.ps1 || goto copilot_failed
goto copilot_done
:copilot_failed
echo.
echo The Copilot install did not finish. Fix what it printed, then run:
echo   .venv\Scripts\python.exe -m src.launching.ps_script copilot\install.ps1
exit /b 1
:copilot_skipped
echo Skipped. Install it later with:
echo   .venv\Scripts\python.exe -m src.launching.ps_script copilot\install.ps1
:copilot_done

echo.
echo ============================================================================
echo  Setup complete. Start the tray with:  tray.bat
echo  Or run the webapp standalone:        webapp.bat
echo ============================================================================
exit /b 0
