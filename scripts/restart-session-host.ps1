# Manually restart the app-launcher session-host on its configured port (default :8466).
#
# tray.bat --restart deliberately EXCLUDES :8466 to protect live PTY
# sessions -- so a change under src/session_host.py
# or app/webapp/session_host/ is not live until this process restarts, and nothing
# else restarts it automatically. This script is the one supported,
# documented way to do that: an explicit, operator-initiated action, never
# a side effect of a normal ship.
#
# THIS KILLS EVERY LIVE CODING-TAB / CHIEF SESSION ON THIS MACHINE. There is
# no drain, no idle check -- every PTY dies immediately, including any
# standing fleet chief. Only run this at a clean boundary (no live sessions
# mid-work), never unattended, never as part of building or testing a
# session-host change itself.
#
# Usage, from the repo root. The launcher starts this script under any execution policy,
# AllSigned included, so nobody has to sign this tracked file:
#   .\.venv\Scripts\python.exe -m src.launching.ps_script scripts\restart-session-host.ps1 -Confirm
#   (bare, with no -Confirm, prints the warning and exits 1 -- does nothing)

param(
    [switch]$Confirm
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$trayPs = Join-Path $PSScriptRoot "tray_lifecycle.ps1"
$trayBat = Join-Path $repoRoot "tray.bat"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

# Both ports come from config/config.json (webapp.port, webapp.session_host_port) through the
# resolver the Python code uses, so a side-by-side install never touches the default ports.
# A port that cannot be resolved stops the script: reclaiming a guessed port could kill
# another launcher's process.
function Get-ConfiguredPort([string[]]$ResolverArgs) {
    $out = & $venvPython -m scripts.webapp_port @ResolverArgs 2>$null
    $port = 0
    if ($LASTEXITCODE -ne 0 -or -not [int]::TryParse(("$out").Trim(), [ref]$port) -or $port -lt 1) {
        Write-Host "Could not resolve the configured port via scripts.webapp_port $ResolverArgs" -ForegroundColor Red
        exit 1
    }
    return $port
}
Push-Location $repoRoot
try {
    $webappPort = Get-ConfiguredPort @()
    $sessionHostPort = Get-ConfiguredPort @("--session-host")
} finally {
    Pop-Location
}

if (-not $Confirm) {
    Write-Host "This restarts the app-launcher session-host (:$sessionHostPort)." -ForegroundColor Yellow
    Write-Host "It KILLS EVERY LIVE PTY SESSION ON THIS MACHINE, including any standing chief." -ForegroundColor Red
    Write-Host "There is no drain and no idle check. Only proceed at a clean boundary." -ForegroundColor Red
    Write-Host ""
    Write-Host "Re-run with -Confirm to proceed:" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\python.exe -m src.launching.ps_script scripts\restart-session-host.ps1 -Confirm"
    exit 1
}

if (-not (Test-Path $trayPs)) {
    Write-Host "Missing shared tray helper: $trayPs" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $trayBat)) {
    Write-Host "Missing tray.bat at repo root: $trayBat" -ForegroundColor Red
    exit 1
}

Write-Host "Reclaiming :$sessionHostPort (this kills every live PTY now)..." -ForegroundColor Cyan
# Through the repo launcher, which loads the script as a ScriptBlock when the execution policy
# refuses an unsigned -File (AllSigned), so the policy stays as the machine set it.
& $venvPython -m src.launching.ps_script $trayPs reclaim `
    -VenvDir (Join-Path $repoRoot ".venv") -Ports $sessionHostPort
if ($LASTEXITCODE -ne 0) {
    Write-Host "tray_lifecycle.ps1 reclaim exited $LASTEXITCODE -- session-host may still be running." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "Restarting the tray (also spawns a fresh session-host since none is left to adopt)..." -ForegroundColor Cyan
& $trayBat --restart

# Bounded wait on the webapp's /api/version `session_host` block, run by the Python check so it
# tries both https and http (the tray serves plain http when no TLS cert exists, which an
# https-only probe never sees) and ends in one of three distinct states: live, answering but not
# provably on the merged code, or not answering, each with the time waited. It gates on
# `freshness`, not raw `stale`: `stale` is true after any merge anywhere in the repo, `freshness`
# carries the stale_relevant scoping (docs/restart-and-liveness.md). Exit 0 only when live.
Push-Location $repoRoot
try {
    & $venvPython -m src.launching.restart_check session-host --port $webappPort
    $waitExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($waitExit -ne 0) {
    Write-Host "session-host restart NOT confirmed -- see the line above and webapp/restart.log." -ForegroundColor Red
    exit 1
}
Write-Host "session-host restart confirmed live." -ForegroundColor Green
