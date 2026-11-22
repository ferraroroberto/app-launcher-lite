"""Reclaim ownership and loud failure in scripts/tray_lifecycle.ps1.

A session-host re-execs from the base interpreter, so its command line is
``<python> <repo>\\launcher.py session-host --port N`` with no ``.venv`` in it. Reclaim
used to match only the ``.venv`` path, skipped that host as not ours, and the "restart" left
the old process serving an old build. The tests run the script's real functions (parsed out
of the file, so nothing is copied) against synthetic command lines and throwaway listeners.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from src.subprocess_flags import NO_WINDOW

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell + CIM")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
TRAY_PS = REPO_ROOT / "scripts" / "tray_lifecycle.ps1"
POWERSHELL = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

# ASCII only: Windows PowerShell 5.1 runs this. Defines every function of the real script
# (not its param block or its action switch), then runs one scenario from the environment.
_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:TL_SCRIPT, [ref]$null, [ref]$null)
foreach ($fn in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    . ([scriptblock]::Create($fn.Extent.Text))
}
$VenvDir = $env:TL_VENV
$ScriptDir = $env:TL_SCRIPTDIR
$Ports = $env:TL_PORTS
if ($env:TL_NO_STOP) { function Stop-Process { } }
if ($env:TL_CASES) {
    $out = @()
    foreach ($c in (ConvertFrom-Json $env:TL_CASES)) { $out += (Test-OwnsProcess $c) }
    Write-Output ($out -join ',')
} else {
    $survivors = Invoke-ReclaimPorts
    Write-Output ('SURVIVORS=' + $survivors)
}
"""


def _run_ps(tmp_path: Path, env_extra: dict) -> str:
    harness = tmp_path / "harness.ps1"
    harness.write_text(_HARNESS, encoding="ascii")
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSEXECUTIONPOLICYPREFERENCE"}
    env["TL_SCRIPT"] = str(TRAY_PS)
    env.update(env_extra)
    done = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True, text=True, env=env, timeout=120, creationflags=NO_WINDOW,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _owns(tmp_path: Path, repo: str, lines: List[str]) -> List[bool]:
    out = _run_ps(tmp_path, {
        "TL_VENV": repo + "\\.venv",
        "TL_SCRIPTDIR": "",
        "TL_CASES": json.dumps(lines),
    })
    return [part.strip() == "True" for part in out.strip().split(",")]


class TestOwnership:
    def test_session_host_without_venv_is_owned_by_its_repo_root(self, tmp_path: Path) -> None:
        repo = "C:" + "\\automation\\app-launcher-lite"
        owned = _owns(tmp_path, repo, [
            # the re-execed form from the report: base interpreter, no .venv in it
            '"C:\\Python314\\pythonw.exe" C:\\automation\\app-launcher-lite\\launcher.py session-host --port 8466',
            # the same tree under its other drive spelling (a junction)
            '"E:\\Python314\\pythonw.exe" E:\\automation\\app-launcher-lite\\launcher.py session-host --port 8466',
            # forward slashes and a quoted launcher path
            'python.exe "C:/automation/app-launcher-lite/launcher.py" session-host',
            # the venv form keeps matching
            "C:\\automation\\app-launcher-lite\\.venv\\Scripts\\pythonw.exe -m something",
        ])
        assert owned == [True, True, True, True]

    def test_same_command_line_from_another_repo_is_not_owned(self, tmp_path: Path) -> None:
        repo = "C:" + "\\automation\\app-launcher-lite"
        owned = _owns(tmp_path, repo, [
            "C:\\Python314\\pythonw.exe C:\\automation\\app-launcher\\launcher.py session-host --port 8466",
            # a sibling checkout of this repo is a different tree
            "C:\\Python314\\pythonw.exe C:\\automation\\app-launcher-lite-wt-371\\launcher.py session-host",
            # a longer parent path that merely ends in the root's segments
            "C:\\Python314\\pythonw.exe C:\\other\\automation\\app-launcher-lite\\launcher.py session-host",
            # this repo, but not the session-host role
            "C:\\Python314\\pythonw.exe C:\\automation\\app-launcher-lite\\launcher.py tray",
            "",
        ])
        assert owned == [False, False, False, False, False]


class TestVenvSpelling:
    """The tray is found whichever spelling of the junctioned repo root started it."""

    REPO = "C:" + "\\automation\\app-launcher-lite"

    def test_venv_command_line_matches_under_either_spelling_and_slash_style(self, tmp_path: Path) -> None:
        owned = _owns(tmp_path, self.REPO, [
            "C:\\automation\\app-launcher-lite\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            "E:\\automation\\app-launcher-lite\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            "C:/automation/app-launcher-lite/.venv/Scripts/pythonw.exe launcher.py tray",
            "E:/automation/app-launcher-lite/.venv/Scripts/pythonw.exe launcher.py tray",
            '"e:\\AUTOMATION\\App-Launcher-Lite\\.venv\\Scripts\\python.exe" -m src.launching.ps_script',
        ])
        assert owned == [True, True, True, True, True]

    def test_venv_of_a_sibling_checkout_or_another_repo_is_not_owned(self, tmp_path: Path) -> None:
        owned = _owns(tmp_path, self.REPO, [
            "E:\\automation\\app-launcher-lite-wt-416\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            "E:\\automation\\app-launcher\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            "E:\\other\\automation\\app-launcher-lite\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            "E:\\automation\\app-launcher-lite\\.venv-old\\Scripts\\pythonw.exe launcher.py tray",
            "C:\\Python314\\pythonw.exe launcher.py tray",
        ])
        assert owned == [False, False, False, False, False]


def _spawn_listener(script_dir: Path, role: str = "session-host") -> Tuple["subprocess.Popen[str]", int, int]:
    """A throwaway ``<script_dir>/launcher.py <role>`` that listens on a free port."""
    script_dir.mkdir(parents=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    (script_dir / "launcher.py").write_text(
        "import os, socket, sys, time\n"
        "s = socket.socket()\n"
        "s.bind(('127.0.0.1', int(sys.argv[3])))\n"
        "s.listen(1)\n"
        "print(os.getpid(), flush=True)\n"
        "time.sleep(120)\n",
        encoding="ascii",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script_dir / "launcher.py"), role, "--port", str(port)],
        stdout=subprocess.PIPE, text=True, creationflags=NO_WINDOW,
    )
    assert proc.stdout is not None
    return proc, port, int(proc.stdout.readline())


def _alive(pid: int) -> bool:
    done = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
        timeout=30, creationflags=NO_WINDOW,
    )
    return str(pid) in done.stdout


def _wait_dead(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.2)
    return not _alive(pid)


class TestReclaim:
    @pytest.fixture
    def listeners(self):
        spawned: List["subprocess.Popen[str]"] = []
        yield spawned
        for proc in spawned:
            proc.kill()

    def _reclaim(self, tmp_path: Path, repo: Path, port: int, no_stop: Optional[str] = None) -> str:
        env = {"TL_VENV": str(repo / ".venv"), "TL_SCRIPTDIR": str(repo), "TL_PORTS": str(port)}
        if no_stop:
            env["TL_NO_STOP"] = no_stop
        return _run_ps(tmp_path, env)

    def test_reclaims_a_session_host_with_no_venv_in_its_command_line(self, tmp_path, listeners) -> None:
        repo = tmp_path / "repo"
        proc, port, pid = _spawn_listener(repo)
        listeners.append(proc)
        out = self._reclaim(tmp_path, repo, port)
        assert "SURVIVORS=0" in out
        assert _wait_dead(pid)

    def test_leaves_another_repos_session_host_alone(self, tmp_path, listeners) -> None:
        mine, theirs = tmp_path / "mine", tmp_path / "theirs"
        proc, port, pid = _spawn_listener(theirs)
        listeners.append(proc)
        out = self._reclaim(tmp_path, mine, port)
        assert "SURVIVORS=0" in out and "not this repo's process" in out
        assert _alive(pid)

    def test_reports_failure_when_the_old_holder_survives_the_stop(self, tmp_path, listeners) -> None:
        repo = tmp_path / "repo"
        proc, port, pid = _spawn_listener(repo)
        listeners.append(proc)
        out = self._reclaim(tmp_path, repo, port, no_stop="1")
        assert "SURVIVORS=1" in out
        assert f"FAILED: old holder PID {pid} is still running on :{port}" in out
        assert _alive(pid)


class TestRestartWithNothingToStop:
    def test_a_restart_that_matched_no_process_says_so(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = {k: v for k, v in os.environ.items() if k.upper() != "PSEXECUTIONPOLICYPREFERENCE"}
        done = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(TRAY_PS),
             "launch", "-AppName", "probe", "-ScriptDir", str(repo), "-VenvDir", str(repo / ".venv"),
             "-TrayMatch", "no-such-tray-zzz", "-Ports", str(port), "-TrayLaunch", "-c pass",
             "-VerifyTimeoutSeconds", "1", "-Restart"],
            capture_output=True, text=True, env=env, timeout=120, creationflags=NO_WINDOW,
        )
        # The start and the build check fail in this empty repo; the message comes before both.
        assert "NOTE: no running probe tray or owned port holder matched" in done.stdout
        assert "nothing was stopped" in done.stdout
