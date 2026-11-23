@echo off
REM ============================================================================
REM  COPILOT INSTALL - run copilot\install.py with no PowerShell script involved
REM ----------------------------------------------------------------------------
REM    copilot\install.bat                  install the hooks and skills
REM    copilot\install.bat --migrate --yes  move off the old fleet-config-lite install
REM    copilot\install.bat --what-if        list every change, make none
REM  Every argument goes to install.py unchanged and its exit code comes back.
REM  A batch file, not a .ps1, so an AllSigned execution policy never refuses it.
REM ============================================================================

setlocal
set "REPO_DIR=%~dp0.."
cd /d "%REPO_DIR%" || exit /b 1
set "REPO_DIR=%CD%"

REM  The repo's own python once .venv exists, else the system one (the py
REM  launcher first: a bare `python` on PATH can be the Microsoft Store stub).
set "PY_EXE=%REPO_DIR%\.venv\Scripts\python.exe"
set "PY_ARGS="
if exist "%PY_EXE%" goto :run
set "PY_EXE="
where py >nul 2>nul && set "PY_EXE=py" && set "PY_ARGS=-3"
if not defined PY_EXE where python >nul 2>nul && set "PY_EXE=python"
if not defined PY_EXE goto :no_python

:run
"%PY_EXE%" %PY_ARGS% "%REPO_DIR%\copilot\install.py" %*
exit /b %ERRORLEVEL%

:no_python
echo ERROR: no Python found. Install Python 3 from python.org, then run copilot\install.bat again.
exit /b 1
