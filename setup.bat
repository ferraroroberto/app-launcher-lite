@echo off
chcp 65001 >nul
REM ============================================================================
REM  SETUP - install App Launcher Lite on this PC, or bring its setup up to date
REM ----------------------------------------------------------------------------
REM  The scaffold's setup.bat plus one app step. What is specific to the app is
REM  declared in the [install] tables of .fleet.toml.
REM  Safe to run any number of times; it never overwrites what this PC has
REM  configured. The work is src\installer.py (docs\install.md):
REM    setup.bat            create .venv, install requirements, create missing
REM                         configs, add keys a settings file lacks (after a
REM                         backup), end with required and optional checklists
REM    setup.bat -WhatIf    list every change, make none
REM    setup.bat -Startup   also start the app at log on
REM    setup.bat -MigrateFrom <old folder> [-StopOld] [-Json]
REM                         in a fresh clone: copy this PC's declared files from
REM                         an existing install, install, start, verify
REM    setup.bat -Replace <install> [-WhatIf] [-Json]
REM                         in a fresh clone beside an install of any age:
REM                         stop the install, rename it to
REM                         <install>.bak-YYYYMMDD-N, move this clone to its
REM                         path, migrate from the backup
REM    setup.bat -InPlace [-WhatIf] [-Json]
REM                         the same from the install itself: clone its origin
REM                         to <folder>.incoming first
REM  A plain run (also with -Startup) then offers the Copilot hooks and skills
REM  (copilot\install.py); -WhatIf, -Json, -MigrateFrom, -Replace and -InPlace
REM  do not.
REM  (PWA/tray icons are committed in the repo -- no generation step needed.)
REM  A batch file, not a .ps1, so the PowerShell execution policy (AllSigned
REM  included) never refuses it. Keep this file ASCII-only: cmd.exe reads ahead
REM  in the codepage that was active before chcp 65001 ran.
REM ============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"

REM  -Replace names the install relative to the caller's folder, so it is made
REM  absolute before the cd below; "\." drops a trailing backslash, which would
REM  escape the closing quote it is passed in.
set "IN_PLACE="
set "REPLACE="
set "REPLACE_FLAG="
set "TAKE="
for %%A in (%*) do (
  if defined TAKE (
    for %%P in ("%%~A\.") do set "REPLACE=%%~fP"
    set "TAKE="
  )
  if /i "%%~A"=="-InPlace" set "IN_PLACE=1"
  if /i "%%~A"=="-Replace" (
    set "IN_PLACE=1"
    set "REPLACE_FLAG=1"
    set "TAKE=1"
  )
)
if defined REPLACE_FLAG if not defined REPLACE goto :no_replace_target

cd /d "%SCRIPT_DIR%" || exit /b 1

REM  The repo's own python once .venv exists, else the system one (the py
REM  launcher first: a bare `python` on PATH can be the Microsoft Store stub).
REM  -InPlace and -Replace always take the system one: they rename the folder
REM  .venv is in.
set "PY_EXE=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "PY_ARGS="
if not defined IN_PLACE if exist "%PY_EXE%" goto :run
set "PY_EXE="
where py >nul 2>nul && set "PY_EXE=py" && set "PY_ARGS=-3"
if not defined PY_EXE where python >nul 2>nul && set "PY_EXE=python"
if not defined PY_EXE goto :no_python
if defined IN_PLACE goto :in_place

:run
REM  -I: isolated mode keeps src\ off sys.path, so the app's src package is never
REM  imported before its requirements exist.
"%PY_EXE%" %PY_ARGS% -I "%SCRIPT_DIR%src\installer.py" %*
if errorlevel 1 exit /b %ERRORLEVEL%

REM  A verified migration also moves app cards still pointing at a -new or -e2e copy beside this
REM  folder (-InPlace and -Replace get here through their own -MigrateFrom).
for %%A in (%*) do if /i "%%~A"=="-MigrateFrom" goto :repoint

REM  -WhatIf changes nothing, -Json prints one report that nothing may follow, and
REM  -MigrateFrom has already started the app: none of them asks or prints more.
for %%A in (%*) do for %%S in (-WhatIf -Json -MigrateFrom) do if /i "%%~A"=="%%S" exit /b 0

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
echo  Or run the webapp standalone:        scripts\webapp.bat
echo ============================================================================
exit /b 0

:repoint
"%PY_EXE%" %PY_ARGS% -B -m src.launching.repoint_cards %*
exit /b %ERRORLEVEL%

:in_place
REM  Windows will not rename a folder a process lives in, so the run moves out:
REM  a temporary copy of this clone's migration modules, the system python, and
REM  a current folder outside the tree. The run, the removal of the stage folder
REM  and the exit share one line because once the folders are renamed, the next
REM  line cmd.exe would read is in another file, or in none. Even ending the
REM  batch looks the file up again, which fails with exit code 1 once -Replace
REM  has renamed it away, so (goto) first leaves this batch, then cmd /c exit
REM  sets the code. The current folder is %TEMP%, not the stage, so nothing
REM  holds the stage open when it is removed.
set "STAGE=%TEMP%\setup-inplace-%RANDOM%%RANDOM%"
mkdir "%STAGE%" || exit /b 1
copy /y "%SCRIPT_DIR%src\installer_migrate.py" "%STAGE%\" >nul || goto :stage_failed
copy /y "%SCRIPT_DIR%src\installer_declaration.py" "%STAGE%\" >nul || goto :stage_failed
copy /y "%SCRIPT_DIR%src\installer_inplace.py" "%STAGE%\" >nul || goto :stage_failed
cd /d "%TEMP%"
if defined REPLACE goto :replace
"%PY_EXE%" %PY_ARGS% -I "%STAGE%\installer_inplace.py" -Folder "%SCRIPT_DIR%." %* & if errorlevel 1 ((goto) 2>nul & rmdir /s /q "%STAGE%" & cmd /d /c exit 1) else ((goto) 2>nul & rmdir /s /q "%STAGE%" & cmd /d /c exit 0)

:replace
"%PY_EXE%" %PY_ARGS% -I "%STAGE%\installer_inplace.py" -Folder "%REPLACE%" -Incoming "%SCRIPT_DIR%." %* & if errorlevel 1 ((goto) 2>nul & rmdir /s /q "%STAGE%" & cmd /d /c exit 1) else ((goto) 2>nul & rmdir /s /q "%STAGE%" & cmd /d /c exit 0)

:stage_failed
rmdir /s /q "%STAGE%"
exit /b 1

:no_replace_target
echo ERROR: -Replace needs the install to replace, e.g. setup.bat -Replace ..\^<app^>
exit /b 1

:no_python
echo ERROR: no Python found. Install Python 3.11 or newer from python.org, then run setup.bat again.
exit /b 1
