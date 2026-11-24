#Requires -Version 5.1
<#
    VENDORED COPY -- provenance:
      Source: ferraroroberto/fleet-config, tray/tray_lifecycle.ps1
      Copied: 2026-08-02, byte-for-byte except: two em-dashes in comments
      normalized to "--" per the ASCII-only rule below, and the example
      ports in comments updated to this fork's 8465/8466. Local code change:
      reclaim also owns a session-host started from this repo root without
      .venv in its command line, fails when an owned holder survives, matches
      this repo's paths under either drive spelling of a junctioned root,
      says so when a restart found nothing to stop, and claims processes
      started from this repo's own `-wt-<N>` git worktrees, ends a plain start
      on the same build check as a restart, and appends the failing phase
      (helper, reclaim, launch, readiness) with the old and new PID, the scheme
      and the last error line to webapp\restart.log.
      Vendored for the lite fork so the tray has zero external dependencies;
      this repo-local copy replaces the machine-shared
      %USERPROFILE%\.claude\tray\tray_lifecycle.ps1. Keep ASCII-only.

    Original header follows (the "ONE MACHINE-LOCAL COPY" contract described
    there applies to the fleet-config original, not this vendored copy):
#>
<#
    Canonical tray lifecycle helper.

    THE ONE MACHINE-LOCAL COPY, owned here (fleet-config, project-scaffolding).
    Every fleet tray.bat on this machine calls this exact file by path --
    `%USERPROFILE%/.claude/tray/tray_lifecycle.ps1`, exposed by this repo's
    install.ps1 junction -- instead of vendoring a byte-copy into each sister
    repo. Do **not** edit this file per-app; there is no per-app copy left to
    diverge. App-specific values (the .venv path, the tray-match regex, the
    owned ports, the tray launch command, and optional version URL) are passed
    in as arguments by each app's tray.bat, never hardcoded here -- that is
    what keeps one file correct for every tray. `app/tray/single_instance.py`
    is a separate, still-vendored primitive (imported Python that ships with
    each app, not a shelled-to helper) -- it stays in project-scaffolding.
    Full reasoning: project-scaffolding's docs/windows-tray.md.

    Why a committed .ps1 instead of cmd-side lifecycle logic:
the old batch shape first embedded CIM/port logic
    in `powershell.exe -Command "..."`, then moved that logic to `-File` but
    still captured detect output through `for /f usebackq`. That cmd capture
    could return empty under non-interactive callers even when the helper worked
    standalone. The `launch` action below removes that failure mode by owning
    the whole detect -> kill -> reclaim -> start -> verify sequence inside one
    PowerShell process, so tray.bat does not parse helper output at all.

    A separate outer-shell hazard happens before this file can run:
Git Bash/MSYS rewrites `cmd.exe /c` to `cmd.exe C:/`.
    That opens an interactive cmd prompt, so tray.bat and this helper are never
    entered. Automation must invoke tray.bat through a real PowerShell caller
    (or use the MSYS-safe `cmd.exe //c` spelling); the fleet Bash hook and issue
    workflow skills enforce the PowerShell path.

    Keep this file ASCII-only: a stray non-ASCII char breaks Windows PowerShell
    5.1 parsing (scaffold docs/windows-tray.md, "Platform gotcha").

    Usage (from tray.bat):
      detect  -> emits the matching tray PIDs, one per line (empty if none):
        powershell.exe -NoProfile -NonInteractive -File tray_lifecycle.ps1 `
          detect -VenvDir "<repo>\.venv" -TrayMatch "launcher\.py\s+tray"
      reclaim -> kills the owning PID of each listed port whose CommandLine is
                 under this repo's .venv, or is this repo's
                 `launcher.py session-host` (orphan-proof), printing each reclaim
                 and exiting 1 when an owned holder survives the stop:
        powershell.exe -NoProfile -NonInteractive -File tray_lifecycle.ps1 `
          reclaim -VenvDir "<repo>\.venv" -Ports "8465,8466"
      launch  -> idempotent start, or restart with port reclaim and git_sha
                 verification:
        powershell.exe -NoProfile -NonInteractive -File tray_lifecycle.ps1 `
          launch -AppName "my-app" -ScriptDir "<repo>" -VenvDir "<repo>\.venv" `
          -TrayMatch "launcher\.py\s+tray" -Ports "8465" `
          -TrayLaunch "launcher.py tray" -Restart
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('detect', 'reclaim', 'launch')]
    [string] $Action,

    [Parameter(Mandatory)]
    [string] $VenvDir,

    # detect: regex matching THIS app's tray invocation (e.g. 'launcher\.py\s+tray').
    [string] $TrayMatch,

    # reclaim: comma-separated owned ports (e.g. '8465,8466'). Parsed here rather
    # than bound as [int[]] so a single cmd token survives -File arg parsing.
    [string] $Ports,

    # launch: display name used in user-facing messages.
    [string] $AppName,

    # launch: repository root / tray working directory.
    [string] $ScriptDir,

    # launch: arguments passed to python/pythonw to start the tray.
    [string] $TrayLaunch,

    # launch: restart instead of idempotent start.
    [switch] $Restart,

    # launch: optional explicit version endpoint. Blank infers first owned port.
    [string] $VersionUrl,

    # launch: bounded stale-serve detection.
    [int] $VerifyTimeoutSeconds = 30
)

# Scope every match by the holder's CommandLine containing this repo's .venv path
# (ordinal, case-insensitive) -- NEVER the process image path. On Python 3.14
# Windows venvs a venv-launched pythonw.exe re-execs the base interpreter, so the
# image path reports the shared base python while only the CommandLine still
# carries the .venv path; an image-path guard never matches the real process and
# the operation silently no-ops. (scaffold docs/windows-tray.md)
# A path is compared by its segments, never as a literal string: the same tree is reachable under
# two spellings (a drive-letter junction, `C:\automation\x` and `E:\automation\x`, either slash
# style) and a live process keeps the spelling it was started from, while -VenvDir carries the
# spelling of the tray.bat that is running now. The drive letter is dropped, and the match starts
# at a path boundary and ends at a separator or the end of the token, so a sibling checkout
# (`<repo>-wt-N`) or a longer parent path is not this repo.
function Get-PathPattern {
    param([string] $Path, [string] $Tail)
    $segments = @($Path -replace '^[A-Za-z]:', '' -split '[\\/]+' | Where-Object { $_ })
    if ($segments.Count -eq 0) { return $null }
    $root = ($segments | ForEach-Object { [regex]::Escape($_) }) -join '[\\/]+'
    return '(^|[\s"''=])([A-Za-z]:)?[\\/]+' + $root + $Tail
}

function Test-CommandLineMatches {
    param([string] $CommandLine, [string] $Pattern)
    if (-not $CommandLine -or -not $Pattern) { return $false }
    return [regex]::IsMatch($CommandLine, $Pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
}

function Test-UnderVenv {
    param([string] $CommandLine, [string] $Dir = $VenvDir)
    return Test-CommandLineMatches $CommandLine (Get-PathPattern ($Dir -replace '[\\/]+$', '') '([\\/]|["''\s]|$)')
}

# A session-host re-execs from the base interpreter, so its CommandLine can carry the repo's
# launcher.py but no .venv path. Ownership is then the repo root plus the role
# (`launcher.py session-host`), never the port or image name alone.
function Get-RepoRoot {
    if ($ScriptDir) { return $ScriptDir }
    return (Split-Path -Parent ($VenvDir -replace '[\\/]+$', ''))
}

function Test-IsRepoSessionHost {
    param([string] $CommandLine, [string] $Root = (Get-RepoRoot))
    return Test-CommandLineMatches $CommandLine (Get-PathPattern $Root '[\\/]+launcher\.py["'']?\s+session-host(\s|$)')
}

# This repo's own sibling worktrees (`<repo>-wt-<N>`), read from `git worktree list` so a directory
# that merely looks like one is never claimed: a tray, webapp or session-host started from a
# worktree runs under that worktree's .venv and never matches the primary's. The name pattern only
# narrows git's answer to the repo's sibling convention. Run from a worktree itself, the leaf
# is `<repo>-wt-<N>`, so no `-wt-<M>` sibling matches and the primary is never claimed. A listing
# git cannot give is said out loud and claims nothing, never a guess.
function Get-RepoWorktreeRoots {
    $root = (Get-RepoRoot) -replace '[\\/]+$', ''
    $leaf = Split-Path -Leaf $root
    $listing = @()
    $listed = $false
    $savedLocks = $env:GIT_OPTIONAL_LOCKS
    $env:GIT_OPTIONAL_LOCKS = '0'
    try {
        $listing = @(& git -C $root worktree list --porcelain 2>$null)
        $listed = ($LASTEXITCODE -eq 0)
    } catch {
        $listed = $false
    } finally {
        $env:GIT_OPTIONAL_LOCKS = $savedLocks
    }
    if (-not $listed) {
        Write-Host ("NOTE: could not list the git worktrees of {0}; processes started from its -wt-N worktrees are not claimed." -f $root)
        return @()
    }
    $found = @()
    foreach ($line in $listing) {
        if ($line -notmatch '^worktree (.+)$') { continue }
        $path = $Matches[1].Trim() -replace '[\\/]+$', ''
        if ((Split-Path -Leaf $path) -match ('^' + [regex]::Escape($leaf) + '-wt-\d+$')) { $found += $path }
    }
    return $found
}

function Get-WorktreeRoots {
    if ($null -eq $script:WorktreeRoots) { $script:WorktreeRoots = @(Get-RepoWorktreeRoots) }
    return $script:WorktreeRoots
}

# Which checkout of this repo a command line belongs to: the primary (Kind 'primary') or one of its
# worktrees (Kind 'worktree'); $null when it is neither. -VenvOnly drops the session-host form, which
# tray detection never wants.
function Get-OwningCheckout {
    param([string] $CommandLine, [switch] $VenvOnly)
    if ((Test-UnderVenv $CommandLine) -or (-not $VenvOnly -and (Test-IsRepoSessionHost $CommandLine))) {
        return [pscustomobject]@{ Kind = 'primary'; Root = (Get-RepoRoot) }
    }
    foreach ($wt in (Get-WorktreeRoots)) {
        if ((Test-UnderVenv $CommandLine (Join-Path $wt '.venv')) -or
            (-not $VenvOnly -and (Test-IsRepoSessionHost $CommandLine $wt))) {
            return [pscustomobject]@{ Kind = 'worktree'; Root = $wt }
        }
    }
    return $null
}

function Test-OwnsProcess {
    param([string] $CommandLine)
    return $null -ne (Get-OwningCheckout $CommandLine)
}

# Each tray of this repo, from the primary or from one of its worktrees, with the checkout it runs from.
function Get-TrayProcesses {
    if (-not $TrayMatch) { throw "detect/launch requires -TrayMatch" }
    $trays = @()
    foreach ($cim in @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'")) {
        # The repo launcher that started this script has the venv path and the tray's launch
        # args on its own command line; it is not a tray, and stopping it would end this run.
        if (-not $cim.CommandLine -or $cim.CommandLine -notmatch $TrayMatch -or
            $cim.CommandLine -like '*src.launching.ps_script*') { continue }
        $checkout = Get-OwningCheckout $cim.CommandLine -VenvOnly
        if ($checkout) { $trays += [pscustomobject]@{ ProcessId = $cim.ProcessId; Kind = $checkout.Kind; Root = $checkout.Root } }
    }
    return $trays
}

function Get-TrayProcessIds {
    return @(Get-TrayProcesses | ForEach-Object { $_.ProcessId })
}

function Get-OwnedPorts {
    $result = @()
    if (-not $Ports) { return $result }
    foreach ($p in ($Ports -split '\s*,\s*')) {
        if (-not $p) { continue }
        $result += [int] $p
    }
    return $result
}

# A failed launch leaves one line in webapp\restart.log (the file restart_check appends to), naming
# the phase that failed, the old and new PIDs, the scheme that answered and the last error line. The
# tray.bat output alone is gone once the caller's console closes, so the reason is kept here. Never
# throws: a log-write failure must not replace the error it is reporting.
$script:Phase = 'helper'
$script:OldPids = @()
$script:NewPid = $null
$script:Scheme = $null

function Format-OneLine {
    param([string] $Text, [int] $Max = 300)
    $flat = (($Text -replace '[\r\n]+', ' ') -replace '\s+', ' ').Trim()
    if ($flat.Length -gt $Max) { return $flat.Substring(0, $Max) }
    return $flat
}

function Write-RestartBreadcrumb {
    param([string] $Mode, [string] $ErrorText)
    try {
        $dir = Join-Path (Get-RepoRoot) 'webapp'
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $oldPid = if ($script:OldPids.Count -gt 0) { (@($script:OldPids | Select-Object -Unique)) -join ',' } else { 'none' }
        $newPid = if ($script:NewPid) { [string] $script:NewPid } else { 'none' }
        $scheme = if ($script:Scheme) { [string] $script:Scheme } else { 'none' }
        $line = '{0} tray.bat {1} FAILED phase={2} old_pid={3} new_pid={4} scheme={5} error={6}' -f `
            (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss'), $Mode, $script:Phase, $oldPid, $newPid, $scheme, (Format-OneLine $ErrorText)
        [System.IO.File]::AppendAllText((Join-Path $dir 'restart.log'), $line + "`n", (New-Object System.Text.UTF8Encoding $false))
    } catch {
        Write-Host ("NOTE: could not write the restart breadcrumb: {0}" -f $_.Exception.Message)
    }
}

function Wait-ProcessGone {
    param([int] $ProcessId, [int] $TimeoutSeconds = 5)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    return -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

# Returns the number of owned holders that were still alive after the kill. A stop that was
# acknowledged but did not end the process is a failed reclaim, never a quiet success.
function Invoke-ReclaimPorts {
    $survivors = 0
    foreach ($port in (Get-OwnedPorts)) {
        $holders = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique)
        foreach ($ownerProcessId in $holders) {
            $cim = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ownerProcessId) -ErrorAction SilentlyContinue
            if (-not $cim) { continue }
            $checkout = Get-OwningCheckout $cim.CommandLine
            if ($checkout) {
                $script:ReclaimedCount++
                Write-Host ("Reclaiming :{0} from PID {1} ({2} checkout {3})" -f $port, $ownerProcessId, $checkout.Kind, $checkout.Root)
                $script:OldPids += $ownerProcessId
                Stop-Process -Id $ownerProcessId -Force -ErrorAction SilentlyContinue
                if (-not (Wait-ProcessGone -ProcessId $ownerProcessId)) {
                    $script:ReclaimFailure = ("old holder PID {0} is still running on :{1} after the stop; the restart did not replace it." -f $ownerProcessId, $port)
                    Write-Host ("FAILED: {0}" -f $script:ReclaimFailure)
                    $survivors++
                }
            } else {
                Write-Host ("Leaving :{0}: held by PID {1}, which is not this repo's process." -f $port, $ownerProcessId)
            }
        }
    }
    return [int] $survivors
}

function Stop-TrayProcesses {
    param([int[]] $ProcessIds)
    foreach ($trayProcessId in $ProcessIds) {
        & taskkill /T /F /PID $trayProcessId > $null 2>&1
    }
}

function Get-PythonLauncher {
    $venvScripts = Join-Path $VenvDir "Scripts"
    $venvPythonw = Join-Path $venvScripts "pythonw.exe"
    $venvPython = Join-Path $venvScripts "python.exe"
    if (Test-Path $venvPythonw) { return $venvPythonw }
    if (Test-Path $venvPython) { return $venvPython }
    return "pythonw"
}

function Start-TrayProcess {
    if (-not $ScriptDir) { throw "launch requires -ScriptDir" }
    if (-not $TrayLaunch) { throw "launch requires -TrayLaunch" }
    $python = Get-PythonLauncher
    Write-Host ("Starting {0} tray..." -f $AppName)
    $started = Start-Process -FilePath $python -ArgumentList $TrayLaunch -WorkingDirectory $ScriptDir -WindowStyle Hidden -PassThru
    if ($started) { $script:NewPid = $started.Id }
}

function Get-GitHead {
    if (-not $ScriptDir) { throw "version verification requires -ScriptDir" }
    $head = (& git -C $ScriptDir rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $head) {
        throw "restart verification requires git and a valid repository HEAD"
    }
    return ([string] $head).Trim()
}

function Resolve-VersionUrls {
    # Returns the ordered candidate URLs to probe. An explicit -VersionUrl is
    # taken verbatim (the app knows its scheme + path -- e.g. a non-standard
    # /admin/api/version). With none given, the app didn't declare a scheme, so
    # probe HTTPS *then* HTTP on the first owned port: fleet PWAs serve HTTPS
    # (Service Workers + Web Push are HTTPS-only), so http-first would fail every
    # PWA's verify leg unless it overrode -VersionUrl.
    # Both candidates stay on 127.0.0.1 so an auth-gated endpoint takes its
    # loopback auth-bypass and the leaf-cert name-mismatch is handled below.
    if ($VersionUrl) { return @($VersionUrl) }
    $ownedPorts = @(Get-OwnedPorts)
    if ($ownedPorts.Count -eq 0) {
        throw "restart verification requires -VersionUrl or at least one owned port"
    }
    return @(
        ("https://127.0.0.1:{0}/api/version" -f $ownedPorts[0]),
        ("http://127.0.0.1:{0}/api/version" -f $ownedPorts[0])
    )
}

function Install-LoopbackCertBypass {
    # A fleet PWA serves HTTPS under a leaf issued for its public name (a
    # Tailscale .ts.net host, or a self-signed CA), never for 127.0.0.1 -- so a
    # loopback verify probe fails certificate validation on name mismatch. We
    # must skip validation, but ONLY for loopback, and Windows PowerShell 5.1
    # has no -SkipCertificateCheck. A PowerShell *scriptblock* callback throws
    # "no Runspace available" on .NET's TLS validation thread; a *compiled*
    # delegate does not. So install a C# callback scoped to loopback hosts.
    # A 127.0.0.1 probe can never match a public-name
    # leaf and a MITM on loopback is out of the threat model; every other host
    # is still fully validated.
    if (-not ([System.Management.Automation.PSTypeName]'LoopbackCertBypass').Type) {
        Add-Type @"
using System;
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public static class LoopbackCertBypass {
    static bool IsLoopback(string h) { return h == "127.0.0.1" || h == "localhost" || h == "::1"; }
    static bool Validate(object s, X509Certificate c, X509Chain ch, SslPolicyErrors e) {
        HttpWebRequest r = s as HttpWebRequest;
        if (r != null && IsLoopback(r.RequestUri.Host)) return true;
        // Non-loopback: this callback REPLACES .NET's own chain validation, so a
        // bare `false` here would reject every valid remote cert too (#151). Defer
        // to the policy errors .NET already computed instead of hardcoding reject.
        return e == SslPolicyErrors.None;
    }
    public static RemoteCertificateValidationCallback Previous;
    public static void Install() {
        Previous = ServicePointManager.ServerCertificateValidationCallback;
        ServicePointManager.ServerCertificateValidationCallback = Validate;
    }
    public static void Restore() { ServicePointManager.ServerCertificateValidationCallback = Previous; }
}
"@
    }
    [System.Net.ServicePointManager]::SecurityProtocol =
        [System.Net.ServicePointManager]::SecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12
    [LoopbackCertBypass]::Install()
}

function Test-GitShaMatches {
    param(
        [string] $ServedSha,
        [string] $HeadSha
    )
    if (-not $ServedSha) { return $false }
    return $HeadSha.StartsWith($ServedSha, [System.StringComparison]::OrdinalIgnoreCase) -or
        $ServedSha.StartsWith($HeadSha, [System.StringComparison]::OrdinalIgnoreCase)
}

function Wait-VersionMatchesHead {
    $urls = @(Resolve-VersionUrls)
    $head = Get-GitHead
    Install-LoopbackCertBypass
    try {
        $deadline = (Get-Date).AddSeconds($VerifyTimeoutSeconds)
        $lastError = $null

        do {
            foreach ($url in $urls) {
                try {
                    $response = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 3
                    $script:Scheme = ([System.Uri] $url).Scheme
                    $servedSha = $response.git_sha
                    if (-not $servedSha) { $servedSha = $response.gitSha }
                    $lastSha = [string] $servedSha
                    if (Test-GitShaMatches -ServedSha $lastSha -HeadSha $head) {
                        # The answering process's own pid, not the launcher stub Start-Process returned.
                        if ($response.pid -is [int] -or $response.pid -is [long]) { $script:NewPid = [int] $response.pid }
                        $assetHash = $response.asset_hash
                        if (-not $assetHash) { $assetHash = $response.assetHash }
                        if ($assetHash) {
                            Write-Host ("Verified {0} serves git_sha {1} (asset_hash {2})." -f $url, $lastSha, $assetHash)
                        } else {
                            Write-Host ("Verified {0} serves git_sha {1}." -f $url, $lastSha)
                        }
                        return
                    }
                    $lastError = "$url served git_sha '$lastSha', expected HEAD '$head'"
                } catch {
                    $lastError = "$url : $($_.Exception.Message)"
                }
            }
            Start-Sleep -Seconds 1
        } while ((Get-Date) -lt $deadline)

        throw ("restart verification failed: {0}" -f $lastError)
    } finally {
        if (([System.Management.Automation.PSTypeName]'LoopbackCertBypass').Type) {
            [LoopbackCertBypass]::Restore()
        }
    }
}

switch ($Action) {
    'detect' {
        Get-TrayProcessIds
    }
    'reclaim' {
        if ((Invoke-ReclaimPorts) -gt 0) { exit 1 }
    }
    'launch' {
        # A plain start and a restart both end on the same health and build check; a running tray is
        # found by its live command line (never a PID file), so nothing stale can make it look running.
        $mode = if ($Restart) { 'restart' } else { 'start' }
        try {
            if (-not $AppName) { throw "launch requires -AppName" }
            $trays = @(Get-TrayProcesses)
            $trayPids = @($trays | ForEach-Object { $_.ProcessId })
            $script:OldPids = @($trayPids)
            if ($trayPids.Count -gt 0 -and -not $Restart) {
                Write-Host ("{0} tray is already running (PID: {1})." -f $AppName, ($trayPids -join " "))
                Write-Host 'Run "tray.bat --restart" to stop it and start fresh.'
                exit 0
            }

            if ($Restart) {
                $script:Phase = 'reclaim'
                if ($trayPids.Count -gt 0) {
                    Write-Host ("Stopping previous {0} tray (PID: {1})..." -f $AppName, ($trayPids -join " "))
                    foreach ($tray in $trays) {
                        Write-Host ("  PID {0}: {1} checkout {2}" -f $tray.ProcessId, $tray.Kind, $tray.Root)
                    }
                    Stop-TrayProcesses -ProcessIds $trayPids
                }
                $script:ReclaimedCount = 0
                $script:ReclaimFailure = $null
                if ((Invoke-ReclaimPorts) -gt 0) {
                    $why = if ($script:ReclaimFailure) { $script:ReclaimFailure } else { "an old process still holds an owned port" }
                    throw ("restart failed: {0}" -f $why)
                }
                if ($trayPids.Count -eq 0 -and $script:ReclaimedCount -eq 0) {
                    # Starting fresh is not a restart: nothing was replaced, so a stale build may still be serving.
                    Write-Host ("NOTE: no running {0} tray or owned port holder matched {1}; nothing was stopped, so this starts a tray beside whatever else is running. The build check below says whether the new one is the one serving." -f $AppName, $VenvDir)
                }
                Start-Sleep -Seconds 2
            }

            $script:Phase = 'launch'
            Start-TrayProcess
            $script:Phase = 'readiness'
            Wait-VersionMatchesHead
        } catch {
            Write-RestartBreadcrumb -Mode $mode -ErrorText $_.Exception.Message
            throw
        }
    }
}
