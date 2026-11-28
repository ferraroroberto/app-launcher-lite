"""tray.bat must not export a generic ``APP_NAME`` to the tray it starts (#963).

cmd's ``setlocal`` only scopes a variable away from the *calling* console;
every process tray.bat launches still inherits it. The tray -> webapp ->
session-host chain then handed ``APP_NAME=AppLauncherLite`` to every Coding
session and launched app, where other projects legitimately read
``APP_NAME`` as their own config key (one app's
``os.getenv("APP_NAME", "Example App")``).

The test runs a throwaway copy of the real tray.bat beside a stub
``scripts/tray_lifecycle.ps1``. The stub
records the environment it inherited plus the arguments it was given, so
nothing real is detected, killed or started.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# ASCII only: Windows PowerShell 5.1 runs this and mis-parses non-ASCII.
_STUB_PS1 = """\
$state = if (Test-Path Env:APP_NAME) { 'present=' + $env:APP_NAME } else { 'absent' }
$lines = @(('APP_NAME_ENV=' + $state), ('ARGS=' + ($args -join '|')))
Set-Content -Path $env:TRAY_STUB_OUT -Value $lines -Encoding ASCII
exit 0
"""


@pytest.mark.skipif(sys.platform != "win32", reason="tray.bat is Windows-only")
def test_tray_bat_does_not_leak_app_name_to_the_tray(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bat = repo / "tray.bat"
    shutil.copyfile(REPO_ROOT / "tray.bat", bat)

    stub = repo / "scripts" / "tray_lifecycle.ps1"
    stub.parent.mkdir(parents=True)
    stub.write_text(_STUB_PS1, encoding="ascii")
    out = tmp_path / "stub_out.txt"

    # The usual restart route is an agent running `tray.bat --restart` from a
    # launcher-hosted session that already inherited the leak, so the caller
    # carries it: tray.bat must clear it, not merely stop setting it.
    # No venv beside the copy, so the plain -File start runs: a gate that itself runs under
    # AllSigned must not hand that policy to this stub.
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("APP_NAME", "PSEXECUTIONPOLICYPREFERENCE")}
    env["APP_NAME"] = "AppLauncherLite"
    env["TRAY_STUB_OUT"] = str(out)

    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(bat)],
        env=env,
        capture_output=True,
        timeout=60,
        creationflags=NO_WINDOW,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    lines = dict(
        line.split("=", 1)
        for line in out.read_text(encoding="ascii").splitlines()
        if "=" in line
    )
    assert lines["APP_NAME_ENV"] == "absent"
    # The tray's display name still reaches the lifecycle helper as its argument.
    assert "|-AppName|AppLauncherLite|" in lines["ARGS"]


@pytest.mark.skipif(sys.platform != "win32", reason="tray.bat is Windows-only")
@pytest.mark.windows_only
def test_tray_bat_starts_its_helper_under_allsigned(tmp_path: Path) -> None:
    """`tray.bat` / `tray.bat --restart` under a process-scope AllSigned policy (#338).

    A copy of tray.bat beside a copy of the real venv launcher (its `pyvenv.cfg` makes it
    the same interpreter) and a stub helper. `-File` would refuse the unsigned stub; the repo
    launcher loads it as a ScriptBlock. `--restart` adds the one `-Restart` switch.
    """
    venv = REPO_ROOT / ".venv"
    if not (venv / "Scripts" / "python.exe").exists():
        pytest.skip("no .venv to borrow the interpreter from")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".venv" / "Scripts").mkdir(parents=True)
    shutil.copyfile(venv / "pyvenv.cfg", repo / ".venv" / "pyvenv.cfg")
    shutil.copyfile(venv / "Scripts" / "python.exe", repo / ".venv" / "Scripts" / "python.exe")
    shutil.copyfile(REPO_ROOT / "tray.bat", repo / "tray.bat")
    (repo / "scripts" / "tray_lifecycle.ps1").write_text(
        "param([string]$Action, [string]$AppName, [switch]$Restart)\n"
        "Set-Content -Path $env:TRAY_STUB_OUT -Encoding ASCII -Value "
        "@(('ACTION=' + $Action), ('APP=' + $AppName), ('RESTART=' + $Restart.IsPresent))\n"
        "exit 0\n",
        encoding="ascii",
    )
    for flag, restart in (([], "False"), (["--restart"], "True")):
        out = tmp_path / f"out{len(flag)}.txt"
        env = {**os.environ, "PSExecutionPolicyPreference": "AllSigned", "TRAY_STUB_OUT": str(out),
               "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"}
        done = subprocess.run(
            ["cmd.exe", "/d", "/c", str(repo / "tray.bat"), *flag],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, creationflags=NO_WINDOW,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        got = dict(line.split("=", 1) for line in out.read_text(encoding="ascii").splitlines())
        assert got == {"ACTION": "launch", "APP": "AppLauncherLite", "RESTART": restart}
