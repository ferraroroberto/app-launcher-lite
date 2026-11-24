@echo off
REM app-launcher-lite chief launcher: written by copilot\install.py, one command that works from any folder.
setlocal
set PYTHONUTF8=1
"{{PYTHON}}" "{{COPILOT}}\skills\_lib\chief_ops.py" %*
exit /b %ERRORLEVEL%
