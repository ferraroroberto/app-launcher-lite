# install.ps1 - forwards to copilot\install.py, which does all the work.
#
#   .\copilot\install.ps1             install (idempotent; re-run after moving the checkout)
#   .\copilot\install.ps1 -Uninstall  remove exactly what the installer put in ~/.copilot
#   .\copilot\install.ps1 -Migrate    move a machine off the old fleet-config-lite install, then install
#   add -WhatIf to any of them        list every change it would make, and make none
#
# The logic lives in one place, install.py, so this wrapper and the Python entry point cannot
# drift. A machine whose execution policy only allows signed scripts runs the Python directly:
#   .\.venv\Scripts\python.exe copilot\install.py [--uninstall|--migrate] [--yes] [--what-if]
# What it writes and why: install.py's docstring and copilot\docs\install.md.
#
# NOTE for agents and future edits: ASCII only in this file - it can run under Windows
# PowerShell 5.1, which chokes on BOM-less non-ASCII.

param(
    [switch]$Uninstall,
    [switch]$Migrate,
    [switch]$Yes,
    [switch]$SkipChecklist,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'

$copilotRepo = $PSScriptRoot
$repo = Split-Path -Parent $copilotRepo

# The repo venv when setup.bat made one, else a real python.exe (never the WindowsApps alias,
# which can hang). install.py is stdlib only, so any Python 3 runs it.
$python = $null
$venv = Join-Path $repo '.venv\Scripts\python.exe'
$candidates = @()
if (Test-Path -LiteralPath $venv -PathType Leaf) { $candidates += $venv }
if ($env:LOCALAPPDATA) {
    $candidates += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Filter python.exe -Recurse -Depth 1 -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
}
$fromPath = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($fromPath) { $candidates += $fromPath }
foreach ($candidate in $candidates) {
    if ($candidate -and ($candidate -notmatch '\\WindowsApps\\')) { $python = $candidate; break }
}
if (-not $python) {
    Write-Error 'No usable python.exe found (only the WindowsApps alias). Install Python and re-run.'
}

$forward = @()
if ($Uninstall) { $forward += '--uninstall' }
if ($Migrate) { $forward += '--migrate' }
if ($Yes) { $forward += '--yes' }
if ($SkipChecklist) { $forward += '--skip-checklist' }
if ($WhatIf) { $forward += '--what-if' }

& $python (Join-Path $copilotRepo 'install.py') @forward
exit $LASTEXITCODE
