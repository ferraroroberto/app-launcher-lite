"""Reclaim ownership and loud failure in scripts/tray_lifecycle.ps1.

A session-host re-execs from the base interpreter, so its command line is
``<python> <repo>\\scripts\\launcher.py session-host --port N`` with no ``.venv`` in it. Reclaim
used to match only the ``.venv`` path, skipped that host as not ours, and the "restart" left
the old process serving an old build. The tests run the script's real functions (parsed out
of the file, so nothing is copied) against synthetic command lines and throwaway listeners.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from src.git_utils import run_git
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
# A fake `git worktree list`: the checkouts git would report for this repo.
if ($env:TL_WORKTREES) { function Get-RepoWorktreeRoots { return @(ConvertFrom-Json $env:TL_WORKTREES) } }
if ($env:TL_MODE -eq 'list') {
    Write-Output ('ROOTS=' + ((@(Get-RepoWorktreeRoots)) -join '|'))
} elseif ($env:TL_MODE -eq 'detect') {
    $TrayMatch = $env:TL_TRAYMATCH
    Write-Output ('PIDS=' + ((@(Get-TrayProcessIds)) -join ','))
} elseif ($env:TL_MODE -eq 'foreign') {
    $TrayMatch = $env:TL_TRAYMATCH
    $ReplaceOtherCheckout = [bool] $env:TL_REPLACE
    $script:ForeignHolders = @()
    $null = Invoke-ReclaimPorts
    try { Resolve-ForeignHolders; Write-Output 'RESOLVED' } catch { Write-Output ('THROWN=' + $_.Exception.Message) }
} elseif ($env:TL_MODE -eq 'portsfree') {
    try { Wait-OwnedPortsFree -TimeoutSeconds 2; Write-Output 'FREE' } catch { Write-Output ('THROWN=' + $_.Exception.Message) }
} elseif ($env:TL_MODE -eq 'owner') {
    Write-Output ('PROBLEM=' + (Get-AnswerOwnerProblem ([int] $env:TL_PID)))
} elseif ($env:TL_CASES) {
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


def _owns(tmp_path: Path, repo: str, lines: List[str], worktrees: Optional[List[str]] = None) -> List[bool]:
    """Ownership of each command line, given the worktrees a fake ``git worktree list`` reports."""
    out = _run_ps(tmp_path, {
        "TL_VENV": repo + "\\.venv",
        "TL_SCRIPTDIR": "",
        "TL_WORKTREES": json.dumps(worktrees or []),
        "TL_CASES": json.dumps(lines),
    })
    return [part.strip() == "True" for part in out.strip().split(",")]


class TestOwnership:
    def test_session_host_without_venv_is_owned_by_its_repo_root(self, tmp_path: Path) -> None:
        repo = "C:" + "\\stubfolder\\app-launcher-lite"
        owned = _owns(tmp_path, repo, [
            # the re-execed form from the report: base interpreter, no .venv in it
            '"C:\\Python314\\pythonw.exe" C:\\stubfolder\\app-launcher-lite\\launcher.py session-host --port 8466',
            # the same tree under its other drive spelling (a junction)
            '"E:\\Python314\\pythonw.exe" E:\\stubfolder\\app-launcher-lite\\launcher.py session-host --port 8466',
            # forward slashes and a quoted launcher path
            'python.exe "C:/stubfolder/app-launcher-lite/launcher.py" session-host',
            # the venv form keeps matching
            "C:\\stubfolder\\app-launcher-lite\\.venv\\Scripts\\pythonw.exe -m something",
        ])
        assert owned == [True, True, True, True]

    def test_session_host_started_from_scripts_is_owned_like_the_old_root_spelling(self, tmp_path: Path) -> None:
        repo = "C:" + "\\stubfolder\\app-launcher-lite"
        owned = _owns(tmp_path, repo, [
            '"C:\\Python314\\pythonw.exe" C:\\stubfolder\\app-launcher-lite\\scripts\\launcher.py session-host --port 8466',
            'python.exe "C:/stubfolder/app-launcher-lite/scripts/launcher.py" session-host',
            # the old root spelling is still found, so a restart replaces a host started before the move
            '"C:\\Python314\\pythonw.exe" C:\\stubfolder\\app-launcher-lite\\launcher.py session-host --port 8466',
            # a sibling checkout's moved launcher is still another tree
            "C:\\Python314\\pythonw.exe C:\\stubfolder\\app-launcher-lite-wt-371\\scripts\\launcher.py session-host",
        ])
        assert owned == [True, True, True, False]

    def test_same_command_line_from_another_repo_is_not_owned(self, tmp_path: Path) -> None:
        repo = "C:" + "\\stubfolder\\app-launcher-lite"
        owned = _owns(tmp_path, repo, [
            "C:\\Python314\\pythonw.exe C:\\stubfolder\\app-launcher\\launcher.py session-host --port 8466",
            # a sibling checkout git does not list for this repo is a different tree
            "C:\\Python314\\pythonw.exe C:\\stubfolder\\app-launcher-lite-wt-371\\launcher.py session-host",
            # a longer parent path that merely ends in the root's segments
            "C:\\Python314\\pythonw.exe C:\\other\\stubfolder\\app-launcher-lite\\launcher.py session-host",
            # this repo, but not the session-host role
            "C:\\Python314\\pythonw.exe C:\\stubfolder\\app-launcher-lite\\launcher.py tray",
            "",
        ])
        assert owned == [False, False, False, False, False]


class TestVenvSpelling:
    """The tray is found whichever spelling of the junctioned repo root started it."""

    REPO = "C:" + "\\stubfolder\\app-launcher-lite"

    def test_venv_command_line_matches_under_either_spelling_and_slash_style(self, tmp_path: Path) -> None:
        owned = _owns(tmp_path, self.REPO, [
            "C:\\stubfolder\\app-launcher-lite\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            "E:\\stubfolder\\app-launcher-lite\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            "C:/stubfolder/app-launcher-lite/.venv/Scripts/pythonw.exe launcher.py tray",
            "E:/stubfolder/app-launcher-lite/.venv/Scripts/pythonw.exe launcher.py tray",
            '"e:\\STUBFOLDER\\App-Launcher-Lite\\.venv\\Scripts\\python.exe" -m src.launching.ps_script',
        ])
        assert owned == [True, True, True, True, True]

    def test_venv_of_a_sibling_checkout_or_another_repo_is_not_owned(self, tmp_path: Path) -> None:
        owned = _owns(tmp_path, self.REPO, [
            "E:\\stubfolder\\app-launcher-lite-wt-416\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            "E:\\stubfolder\\app-launcher\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            "E:\\other\\stubfolder\\app-launcher-lite\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            "E:\\stubfolder\\app-launcher-lite\\.venv-old\\Scripts\\pythonw.exe launcher.py tray",
            "C:\\Python314\\pythonw.exe launcher.py tray",
        ])
        assert owned == [False, False, False, False, False]


class TestOwnWorktrees:
    """A tray, webapp or session-host started from this repo's own worktree is claimed."""

    REPO = "C:" + "\\stubfolder\\app-launcher-lite"
    # what `git worktree list --porcelain` prints: forward slashes
    WORKTREES = ["C:/stubfolder/app-launcher-lite-wt-478"]

    def test_tray_webapp_and_session_host_of_a_worktree_are_claimed(self, tmp_path: Path) -> None:
        owned = _owns(tmp_path, self.REPO, [
            "C:\\stubfolder\\app-launcher-lite-wt-478\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            '"C:\\stubfolder\\app-launcher-lite-wt-478\\.venv\\Scripts\\python.exe" -m uvicorn app.webapp.server:app --port 8978',
            # the session-host re-execs from the base interpreter: no .venv in its command line
            "C:\\Python314\\pythonw.exe C:\\stubfolder\\app-launcher-lite-wt-478\\launcher.py session-host --port 8466",
            # the same worktree under its other drive spelling
            "E:\\stubfolder\\app-launcher-lite-wt-478\\.venv\\Scripts\\pythonw.exe launcher.py tray",
        ], self.WORKTREES)
        assert owned == [True, True, True, True]

    def test_other_processes_are_not_claimed(self, tmp_path: Path) -> None:
        owned = _owns(tmp_path, self.REPO, [
            # app-launcher's own tray, session-host and worktree
            "C:\\stubfolder\\app-launcher\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            "C:\\Python314\\pythonw.exe C:\\stubfolder\\app-launcher\\launcher.py session-host --port 8446",
            "C:\\stubfolder\\app-launcher-wt-12\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            # a lookalike directory git does not list as this repo's worktree
            "C:\\stubfolder\\app-launcher-lite-wt-999\\.venv\\Scripts\\pythonw.exe launcher.py tray",
            "C:\\Python314\\pythonw.exe C:\\stubfolder\\app-launcher-lite-wt-999\\launcher.py session-host",
            # unrelated python under another .venv, and the bare interpreter
            "C:\\fixtures\\other\\.venv\\Scripts\\python.exe -m http.server",
            "C:\\Python314\\python.exe -m http.server",
            # a worktree process that is neither under its .venv nor its session-host
            "C:\\Python314\\pythonw.exe C:\\stubfolder\\app-launcher-lite-wt-478\\launcher.py tray",
            # a longer name that merely starts with the worktree's
            "C:\\stubfolder\\app-launcher-lite-wt-4781\\.venv\\Scripts\\pythonw.exe launcher.py tray",
        ], self.WORKTREES)
        assert owned == [False] * 9

    def test_an_unreadable_worktree_listing_claims_no_worktree(self, tmp_path: Path) -> None:
        # Real git against a directory that is not a repository: nothing is claimed and the note says why.
        out = _run_ps(tmp_path, {
            "TL_VENV": str(tmp_path / "missing" / "app-launcher-lite" / ".venv"),
            "TL_SCRIPTDIR": str(tmp_path / "missing" / "app-launcher-lite"),
            "TL_MODE": "list",
        })
        assert "ROOTS=" in out and "ROOTS=C" not in out
        assert "could not list the git worktrees" in out


class TestWorktreeListing:
    """The listing comes from git, narrowed to this repo's ``<repo>-wt-<N>`` siblings."""

    def test_only_registered_sibling_worktrees_are_listed(self, tmp_path: Path) -> None:
        repo = tmp_path / "app-launcher-lite"
        repo.mkdir()
        assert run_git(repo, ["init", "-q", "-b", "main"]) is not None
        ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        assert run_git(repo, [*ident, "commit", "-q", "--no-verify", "--allow-empty", "-m", "x"]) is not None
        sibling = tmp_path / "app-launcher-lite-wt-7"
        elsewhere = tmp_path / "scratch-tree"
        assert run_git(repo, ["worktree", "add", "-q", "-b", "b7", str(sibling)]) is not None
        assert run_git(repo, ["worktree", "add", "-q", "-b", "bx", str(elsewhere)]) is not None
        (tmp_path / "app-launcher-lite-wt-9").mkdir()  # looks like one, git does not know it

        out = _run_ps(tmp_path, {
            "TL_VENV": str(repo / ".venv"), "TL_SCRIPTDIR": str(repo), "TL_MODE": "list",
        })
        roots = [r for r in out.strip().split("ROOTS=", 1)[1].split("|") if r]
        assert [Path(r).resolve() for r in roots] == [sibling.resolve()]


def _spawn_listener(
    script_dir: Path, role: str = "session-host", host: str = "127.0.0.1"
) -> Tuple["subprocess.Popen[str]", int, int]:
    """A throwaway ``<script_dir>/launcher.py <role>`` that listens on a free port of ``host``."""
    script_dir.mkdir(parents=True)
    with socket.socket() as probe:
        probe.bind((host, 0))
        port = probe.getsockname()[1]
    (script_dir / "launcher.py").write_text(
        "import os, socket, sys, time\n"
        "s = socket.socket()\n"
        f"s.bind(({host!r}, int(sys.argv[3])))\n"
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


class TestWorktreeProcessesAreReclaimed:
    """Real listeners standing in for a tray and a session-host started from a worktree."""

    @pytest.fixture
    def listeners(self):
        spawned: List["subprocess.Popen[str]"] = []
        yield spawned
        for proc in spawned:
            proc.kill()

    def test_reclaim_stops_a_worktree_session_host_and_names_its_checkout(self, tmp_path, listeners) -> None:
        repo, worktree = tmp_path / "app-launcher-lite", tmp_path / "app-launcher-lite-wt-5"
        proc, port, pid = _spawn_listener(worktree)
        listeners.append(proc)
        out = _run_ps(tmp_path, {
            "TL_VENV": str(repo / ".venv"), "TL_SCRIPTDIR": str(repo), "TL_PORTS": str(port),
            "TL_WORKTREES": json.dumps([str(worktree)]),
        })
        assert "SURVIVORS=0" in out
        assert f"Reclaiming :{port} from PID {pid} (worktree checkout" in out
        assert _wait_dead(pid)

    def test_reclaim_leaves_an_unlisted_worktree_alone(self, tmp_path, listeners) -> None:
        repo, worktree = tmp_path / "app-launcher-lite", tmp_path / "app-launcher-lite-wt-5"
        proc, port, pid = _spawn_listener(worktree)
        listeners.append(proc)
        out = _run_ps(tmp_path, {
            "TL_VENV": str(repo / ".venv"), "TL_SCRIPTDIR": str(repo), "TL_PORTS": str(port),
            "TL_WORKTREES": json.dumps([]),
        })
        assert "SURVIVORS=0" in out and "not this repo's process" in out
        assert _alive(pid)

    def test_detect_finds_the_primary_and_worktree_trays_but_not_another_repos(self, tmp_path, listeners) -> None:
        repo, worktree = tmp_path / "app-launcher-lite", tmp_path / "app-launcher-lite-wt-5"
        other = tmp_path / "app-launcher"
        pids = {}
        # the tray's command line carries `<root>\.venv`: the listener script lives under it
        for name, root in (("primary", repo), ("worktree", worktree), ("other", other)):
            proc, _port, pid = _spawn_listener(root / ".venv", role="tray")
            listeners.append(proc)
            pids[name] = pid
        out = _run_ps(tmp_path, {
            "TL_VENV": str(repo / ".venv"), "TL_SCRIPTDIR": str(repo), "TL_MODE": "detect",
            "TL_TRAYMATCH": r"launcher\.py\s+tray",
            "TL_WORKTREES": json.dumps([str(worktree)]),
        })
        found = {int(p) for p in out.strip().split("PIDS=", 1)[1].split(",") if p}
        # A venv launcher can sit in front of the interpreter with the same command line, so the
        # set may hold more than one process per tray: assert on membership, not equality.
        assert {pids["primary"], pids["worktree"]} <= found
        assert pids["other"] not in found


class TestTrayMatchSpellings:
    """tray.bat's -TrayMatch finds a tray started from the root (before the move) and from scripts/."""

    @staticmethod
    def _tray_match() -> str:
        text = (REPO_ROOT / "tray.bat").read_text(encoding="utf-8")
        found = {m.group(1) for m in re.finditer(r'-TrayMatch "([^"]+)"', text)}
        assert len(found) == 1, found
        return found.pop()

    def test_detect_finds_the_tray_under_either_spelling(self, tmp_path: Path) -> None:
        repo = tmp_path / "app-launcher-lite"
        spawned = []
        try:
            pids = {}
            for name, folder in (("old", repo / ".venv"), ("new", repo / ".venv" / "scripts")):
                proc, _port, pid = _spawn_listener(folder, role="tray")
                spawned.append(proc)
                pids[name] = pid
            out = _run_ps(tmp_path, {
                "TL_VENV": str(repo / ".venv"), "TL_SCRIPTDIR": str(repo), "TL_MODE": "detect",
                "TL_TRAYMATCH": self._tray_match(), "TL_WORKTREES": json.dumps([]),
            })
        finally:
            for proc in spawned:
                proc.kill()
        found = {int(p) for p in out.strip().split("PIDS=", 1)[1].split(",") if p}
        assert {pids["old"], pids["new"]} <= found

    def test_tray_launch_points_at_scripts(self) -> None:
        text = (REPO_ROOT / "tray.bat").read_text(encoding="utf-8")
        assert 'set "TRAY_LAUNCH=scripts\\launcher.py tray"' in text


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


class TestAnotherCheckoutOnTheOwnedPort:
    """Two checkouts of the app: a restart from one never quietly leaves the other serving."""

    @pytest.fixture
    def listeners(self):
        spawned: List["subprocess.Popen[str]"] = []
        yield spawned
        for proc in spawned:
            proc.kill()

    @staticmethod
    def _other_checkout(root: Path) -> None:
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        (root / "tray.bat").write_text("@echo off\n", encoding="ascii")
        (root / "scripts" / "tray_lifecycle.ps1").write_text("# stand-in\n", encoding="ascii")

    def _restart_reclaim(self, tmp_path: Path, mine: Path, port: int, replace: bool) -> str:
        env = {
            "TL_MODE": "foreign", "TL_VENV": str(mine / ".venv"), "TL_SCRIPTDIR": str(mine),
            "TL_PORTS": str(port), "TL_WORKTREES": json.dumps([]),
            "TL_TRAYMATCH": r"launcher\.py\s+tray",
        }
        if replace:
            env["TL_REPLACE"] = "1"
        return _run_ps(tmp_path, env)

    def test_restart_refuses_and_names_the_other_checkout(self, tmp_path, listeners) -> None:
        mine, other = tmp_path / "app-launcher-lite", tmp_path / "old-install" / "app-launcher-lite"
        self._other_checkout(other)
        proc, port, pid = _spawn_listener(other / "web", role="webapp")
        listeners.append(proc)
        out = self._restart_reclaim(tmp_path, mine, port, replace=False)
        assert "THROWN=restart refused" in out
        assert f"held by PID {pid}" in out and str(other / "web") in out
        assert "--replace-other-checkout" in out
        assert _alive(pid)

    def test_replace_stops_the_other_checkouts_tray_and_holder(self, tmp_path, listeners) -> None:
        mine, other = tmp_path / "app-launcher-lite", tmp_path / "old-install" / "app-launcher-lite"
        self._other_checkout(other)
        holder, port, holder_pid = _spawn_listener(other / "holder", role="webapp")
        listeners.append(holder)
        self._other_checkout(other / "holder")
        # the other copy's tray, whose watchdog would bring its webapp back
        tray_proc, _tray_port, tray_pid = _spawn_listener(other / "holder" / ".venv", role="tray")
        listeners.append(tray_proc)
        out = self._restart_reclaim(tmp_path, mine, port, replace=True)
        assert "RESOLVED" in out, out
        assert _wait_dead(holder_pid)
        assert _wait_dead(tray_pid)

    def test_replace_never_stops_a_process_that_is_not_a_checkout(self, tmp_path, listeners) -> None:
        mine, stranger = tmp_path / "app-launcher-lite", tmp_path / "something-else"
        proc, port, pid = _spawn_listener(stranger, role="webapp")
        listeners.append(proc)
        out = self._restart_reclaim(tmp_path, mine, port, replace=True)
        assert "THROWN=restart refused" in out and "not a checkout of this app" in out
        assert _alive(pid)

    def test_build_check_rejects_an_answer_from_another_checkout(self, tmp_path, listeners) -> None:
        mine, other = tmp_path / "app-launcher-lite", tmp_path / "old-install" / "app-launcher-lite"
        ours, _p1, our_pid = _spawn_listener(mine / ".venv", role="webapp")
        theirs, _p2, their_pid = _spawn_listener(other / ".venv", role="webapp")
        listeners.extend([ours, theirs])

        def problem(pid: int) -> str:
            out = _run_ps(tmp_path, {
                "TL_MODE": "owner", "TL_VENV": str(mine / ".venv"), "TL_SCRIPTDIR": str(mine),
                "TL_WORKTREES": json.dumps([]), "TL_PID": str(pid),
            })
            return out.strip().split("PROBLEM=", 1)[1]

        assert problem(our_pid) == ""
        other_problem = problem(their_pid)
        assert f"PID {their_pid} runs from checkout {other}" in other_problem
        assert problem(999999).startswith("the command line of PID 999999 could not be read")


def _non_loopback_ipv4() -> Optional[str]:
    """One of this machine's own IPv4 addresses that is neither loopback nor link-local."""
    psutil = pytest.importorskip("psutil")
    for addrs in psutil.net_if_addrs().values():
        for addr in addrs:
            if addr.family == socket.AF_INET and not addr.address.startswith(("127.", "169.254.")):
                return addr.address
    return None


class TestListenerOnAnotherAddress:
    """Tailscale serves the webapp's port number on the tailnet addresses next to the webapp's
    0.0.0.0 listener; that holder never blocks a restart."""

    def test_restart_and_port_wait_leave_it_alone(self, tmp_path) -> None:
        host = _non_loopback_ipv4()
        if host is None:
            pytest.skip("no non-loopback IPv4 address on this machine")
        mine, other = tmp_path / "app-launcher-lite", tmp_path / "old-install" / "app-launcher-lite"
        proc, port, pid = _spawn_listener(other, role="webapp", host=host)
        TestAnotherCheckoutOnTheOwnedPort._other_checkout(other)
        try:
            env = {
                "TL_VENV": str(mine / ".venv"), "TL_SCRIPTDIR": str(mine), "TL_PORTS": str(port),
                "TL_WORKTREES": json.dumps([]), "TL_TRAYMATCH": r"launcher\.py\s+tray",
            }
            assert "RESOLVED" in _run_ps(tmp_path, {**env, "TL_MODE": "foreign"})
            assert "FREE" in _run_ps(tmp_path, {**env, "TL_MODE": "portsfree"})
            assert _alive(pid)
        finally:
            proc.kill()
