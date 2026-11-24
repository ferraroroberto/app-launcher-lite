"""A failing ``tray_lifecycle.ps1 launch`` leaves its phase and first error in ``webapp/restart.log``.

``tray.bat`` hands every start and restart to this helper, and its output is gone once the caller's
console closes. The helper appends one line per failure naming the phase (helper, reclaim, launch,
readiness), the old and new PID, the scheme that answered and the last error line, so a restart
check that reports "tray.bat exited 1" has the reason one line above it. The tests run the real
script with a stand-in interpreter (a copy of ``cmd.exe``, or an unrunnable file) as the fake helper
process, never a real tray.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

from src.git_utils import run_git
from src.subprocess_flags import NO_WINDOW

pytestmark = [
    pytest.mark.windows_only,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell + CIM"),
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
TRAY_PS = REPO_ROOT / "scripts" / "tray_lifecycle.ps1"
POWERSHELL = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
CMD_EXE = Path(os.environ.get("COMSPEC", "C:/Windows/System32/cmd.exe"))  # host-identity-exempt: the system shell, not a user path

_LINE = re.compile(
    r"^\S+ tray\.bat (?P<mode>\w+) FAILED phase=(?P<phase>\w+) old_pid=(?P<old>\S+) "
    r"new_pid=(?P<new>\S+) scheme=(?P<scheme>\S+) error=(?P<error>.*)$"
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))  # port-host-exempt: an ephemeral loopback port
        return probe.getsockname()[1]


def _launch(repo: Path, *extra: str, restart: bool = True) -> Tuple["subprocess.CompletedProcess[str]", List[re.Match]]:
    """Run ``launch`` against ``repo`` and return the process plus the parsed breadcrumb lines."""
    port = _free_port()
    args = [
        "launch", "-ScriptDir", str(repo), "-VenvDir", str(repo / ".venv"),
        "-TrayMatch", "no-such-tray-zzz", "-Ports", str(port),
        "-VersionUrl", f"http://127.0.0.1:{port}/api/version",  # port-host-exempt: the unused ephemeral port
        "-VerifyTimeoutSeconds", "2",
    ]
    if restart:
        args.append("-Restart")
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSEXECUTIONPOLICYPREFERENCE"}
    done = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(TRAY_PS),
         *args, *extra],
        capture_output=True, text=True, env=env, timeout=120, creationflags=NO_WINDOW,
    )
    log = repo / "webapp" / "restart.log"
    lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    return done, [m for m in (_LINE.match(line) for line in lines) if m]


def _git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    assert run_git(repo, ["init", "-q", "-b", "main"]) is not None
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    assert run_git(repo, [*ident, "commit", "-q", "--no-verify", "--allow-empty", "-m", "x"]) is not None


def test_a_missing_app_name_is_a_helper_phase_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    done, found = _launch(repo)

    assert done.returncode != 0
    assert len(found) == 1
    assert found[0]["phase"] == "helper"
    assert "launch requires -AppName" in found[0]["error"]
    assert found[0]["old"] == "none" and found[0]["new"] == "none" and found[0]["scheme"] == "none"


def test_a_launch_that_cannot_start_names_the_launch_phase_and_its_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_bytes(b"")  # a launcher the OS cannot run: the fake helper process
    done, found = _launch(repo, "-AppName", "probe", "-TrayLaunch", "-c pass")

    assert done.returncode != 0
    assert len(found) == 1
    assert found[0]["mode"] == "restart" and found[0]["phase"] == "launch"
    assert found[0]["error"]  # the first error line, not an empty reason


def test_a_tray_that_never_serves_names_the_readiness_phase_with_pid_and_scheme(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy(CMD_EXE, scripts / "python.exe")  # starts, exits at once, never serves
    _git_repo(repo)
    done, found = _launch(repo, "-AppName", "probe", "-TrayLaunch", "/c exit 0")

    assert done.returncode != 0
    assert len(found) == 1
    assert found[0]["phase"] == "readiness"
    assert re.fullmatch(r"\d+", found[0]["new"])  # the process Start-Process returned
    assert found[0]["old"] == "none"
    assert found[0]["scheme"] == "none"  # nothing answered on either scheme
    assert "restart verification failed" in found[0]["error"]


def test_a_plain_start_ends_on_the_same_build_check_and_leaves_the_same_line(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy(CMD_EXE, scripts / "python.exe")
    _git_repo(repo)
    done, found = _launch(repo, "-AppName", "probe", "-TrayLaunch", "/c exit 0", restart=False)

    assert done.returncode != 0
    assert len(found) == 1
    assert found[0]["mode"] == "start" and found[0]["phase"] == "readiness"


# ASCII only: Windows PowerShell 5.1 runs this. The real functions of the script, then the same
# reclaim-then-record sequence the launch action runs, with the stop disabled so the holder survives.
_RECLAIM_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:TL_SCRIPT, [ref]$null, [ref]$null)
foreach ($fn in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    . ([scriptblock]::Create($fn.Extent.Text))
}
$VenvDir = $env:TL_VENV
$ScriptDir = $env:TL_SCRIPTDIR
$Ports = $env:TL_PORTS
function Stop-Process { }
function Get-RepoWorktreeRoots { return @() }
$script:Phase = 'reclaim'
$script:ReclaimFailure = $null
if ((Invoke-ReclaimPorts) -gt 0) {
    Write-RestartBreadcrumb -Mode 'restart' -ErrorText ('restart failed: ' + $script:ReclaimFailure)
}
"""


def test_a_holder_that_survives_the_reclaim_names_the_reclaim_phase_and_the_old_pid(tmp_path: Path) -> None:
    from tests.unit.tray.test_tray_lifecycle_reclaim import _spawn_listener

    repo = tmp_path / "repo"
    proc, port, pid = _spawn_listener(repo)
    try:
        harness = tmp_path / "harness.ps1"
        harness.write_text(_RECLAIM_HARNESS, encoding="ascii")
        env = {k: v for k, v in os.environ.items() if k.upper() != "PSEXECUTIONPOLICYPREFERENCE"}
        env.update({"TL_SCRIPT": str(TRAY_PS), "TL_VENV": str(repo / ".venv"),
                    "TL_SCRIPTDIR": str(repo), "TL_PORTS": str(port)})
        done = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
            capture_output=True, text=True, env=env, timeout=120, creationflags=NO_WINDOW,
        )
    finally:
        proc.kill()
    assert done.returncode == 0, done.stderr
    lines = (repo / "webapp" / "restart.log").read_text(encoding="utf-8").splitlines()
    found = [m for m in (_LINE.match(line) for line in lines) if m]
    assert len(found) == 1
    assert found[0]["phase"] == "reclaim" and found[0]["old"] == str(pid)
    assert f"old holder PID {pid} is still running on :{port}" in found[0]["error"]
