#Requires -Version 5.1
<#
    VENDORED COPY -- provenance:
      Source: ferraroroberto/fleet-config, tray/tray_lifecycle.ps1
      Copied: 2026-08-02, byte-for-byte except: two em-dashes in comments
      normalized to "--" per the ASCII-only rule below, and the example
      ports in comments updated to this fork's 8455/8456. No code changes.
      Vendored for the lite fork so the tray has zero external dependencies;
      this repo-local copy replaces the machine-shared
      %USERPROFILE%\.claude\tray\tray_lifecycle.ps1. Keep ASCII-only.

    Original header follows (the "ONE MACHINE-LOCAL COPY" contract described
    there applies to the fleet-config original, not this vendored copy):
#>
<#
    Canonical tray lifecycle helper.

    THE ONE MACHINE-LOCAL COPY, owned here (fleet-config, project-scaffolding#153).
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
    Full reasoning: project-scaffolding's docs/windows-tray.md +
    project-scaffolding#29 / #36 / #54 / #153.

    Why a committed .ps1 instead of cmd-side lifecycle logic
    (project-scaffolding#54): the old batch shape first embedded CIM/port logic
    in `powershell.exe -Command "..."`, then moved that logic to `-File` but
    still captured detect output through `for /f usebackq`. That cmd capture
    could return empty under non-interactive callers even when the helper worked
    standalone. The `launch` action below removes that failure mode by owning
    the whole detect -> kill -> reclaim -> start -> verify sequence inside one
    PowerShell process, so tray.bat does not parse helper output at all.

    A separate outer-shell hazard happens before this file can run
    (fleet-config#385): Git Bash/MSYS rewrites `cmd.exe /c` to `cmd.exe C:/`.
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
                 under this repo's .venv (orphan-proof), printing each reclaim:
        powershell.exe -NoProfile -NonInteractive -File tray_lifecycle.ps1 `
          reclaim -VenvDir "<repo>\.venv" -Ports "8455,8456"
      launch  -> idempotent start, or restart with port reclaim and git_sha
                 verification:
        powershell.exe -NoProfile -NonInteractive -File tray_lifecycle.ps1 `
          launch -AppName "my-app" -ScriptDir "<repo>" -VenvDir "<repo>\.venv" `
          -TrayMatch "launcher\.py\s+tray" -Ports "8455" `
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

    # reclaim: comma-separated owned ports (e.g. '8455,8456'). Parsed here rather
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
function Test-UnderVenv {
    param([string] $CommandLine)
    return $CommandLine -and
        $CommandLine.IndexOf($VenvDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Get-TrayProcessIds {
    if (-not $TrayMatch) { throw "detect/launch requires -TrayMatch" }
    return @(
        Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
            Where-Object { (Test-UnderVenv $_.CommandLine) -and $_.CommandLine -match $TrayMatch } |
            Select-Object -ExpandProperty ProcessId
    )
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

function Invoke-ReclaimPorts {
    foreach ($port in (Get-OwnedPorts)) {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
            $ownerProcessId = $_.OwningProcess
            $cim = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ownerProcessId) -ErrorAction SilentlyContinue
            if ($cim -and (Test-UnderVenv $cim.CommandLine)) {
                Write-Host ("Reclaiming :{0} from PID {1}" -f $port, $ownerProcessId)
                Stop-Process -Id $ownerProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    }
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
    Start-Process -FilePath $python -ArgumentList $TrayLaunch -WorkingDirectory $ScriptDir -WindowStyle Hidden
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
    # PWA's verify leg unless it overrode -VersionUrl (project-scaffolding#147).
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
    # delegate does not. So install a C# callback scoped to loopback hosts
    # (project-scaffolding#147). A 127.0.0.1 probe can never match a public-name
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
                    $servedSha = $response.git_sha
                    if (-not $servedSha) { $servedSha = $response.gitSha }
                    $lastSha = [string] $servedSha
                    if (Test-GitShaMatches -ServedSha $lastSha -HeadSha $head) {
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
        Invoke-ReclaimPorts
    }
    'launch' {
        if (-not $AppName) { throw "launch requires -AppName" }
        $trayPids = @(Get-TrayProcessIds)
        if ($trayPids.Count -gt 0 -and -not $Restart) {
            Write-Host ("{0} tray is already running (PID: {1})." -f $AppName, ($trayPids -join " "))
            Write-Host 'Run "tray.bat --restart" to stop it and start fresh.'
            exit 0
        }

        if ($Restart) {
            if ($trayPids.Count -gt 0) {
                Write-Host ("Stopping previous {0} tray (PID: {1})..." -f $AppName, ($trayPids -join " "))
                Stop-TrayProcesses -ProcessIds $trayPids
            }
            Invoke-ReclaimPorts
            Start-Sleep -Seconds 2
        }

        Start-TrayProcess
        if ($Restart) {
            Wait-VersionMatchesHead
        }
    }
}
