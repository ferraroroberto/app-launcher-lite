@echo off
chcp 65001 >nul
REM ============================================================================
REM  SETUP - install App Launcher Lite on this PC, or bring its setup up to date
REM ----------------------------------------------------------------------------
REM  Safe to run any number of times; it never overwrites what this PC has
REM  configured. The work is scripts\install.py (docs\install.md):
REM    setup.bat            create .venv, install requirements, create missing
REM                         configs, add keys a config lacks (after a backup)
REM    setup.bat -WhatIf    list every change, make none
REM    setup.bat -Startup   also start the tray at log on
REM  Then it offers the Copilot hooks and skills (copilot\install.py).
REM  A batch file, not a .ps1, so an AllSigned execution policy never refuses it.
REM  (PWA/tray icons are committed in the repo -- no generation step needed.)
REM  After this runs once, `tray.bat` is enough for day-to-day use.
REM ============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b 1

REM  The repo's own python once .venv exists, else the system one (the py
REM  launcher first: a bare `python` on PATH can be the Microsoft Store stub).
set "PY_EXE=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "PY_ARGS="
if exist "%PY_EXE%" goto :run
set "PY_EXE="
where py >nul 2>nul && set "PY_EXE=py" && set "PY_ARGS=-3"
if not defined PY_EXE where python >nul 2>nul && set "PY_EXE=python"
if not defined PY_EXE goto :no_python

:run
"%PY_EXE%" %PY_ARGS% "%SCRIPT_DIR%scripts\install.py" %*
if errorlevel 1 exit /b %ERRORLEVEL%

REM  A -WhatIf run makes no change, so it asks nothing and installs nothing.
echo %* | find /i "-WhatIf" >nul && exit /b 0

set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
echo.
echo The Copilot side (the session-state and chief hooks, and the issue skills) installs
echo into ~\.copilot. It writes nothing else: not ~\.claude, no trust or agent settings.
echo Coming from fleet-config-lite? Answer n and follow
echo docs\migrating-from-fleet-config-lite.md instead.
set "ANSWER="
set /p "ANSWER=Install it now? [y/N] "
if /i not "%ANSWER%"=="y" goto copilot_skipped
"%VENV_PY%" copilot\install.py || goto copilot_failed
goto copilot_done
:copilot_failed
echo.
echo The Copilot install did not finish. Fix what it printed, then run:
echo   .venv\Scripts\python.exe copilot\install.py
exit /b 1
:copilot_skipped
echo Skipped. Install it later with:
echo   .venv\Scripts\python.exe copilot\install.py
:copilot_done

echo.
echo ============================================================================
echo  Setup complete. Start the tray with:  tray.bat
echo  Or run the webapp standalone:        webapp.bat
echo ============================================================================
exit /b 0

:no_python
echo ERROR: no Python found. Install Python 3 from python.org, then run setup.bat again.
exit /b 1
