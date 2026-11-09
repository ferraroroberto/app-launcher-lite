"""The pre-ship gate ends every run with a `GATE-RESULT: PASS|FAIL` stdout line.

A pipeline reports its last command's status, so `verify-before-ship.ps1 | tail`
hands the caller `tail`'s 0 and a red gate reads green. The verdict line is the
part a pipe cannot rewrite. The helper block in the gate (between the
`gate-verdict` markers) is run for real over a captured stdout (the same
anonymous pipe `|` creates) for a pass and an early terminating error; a copy of
the real script is run, with a stand-in interpreter, for a failed stage and for
the paths that need no stage to execute.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
GATE = REPO_ROOT / "scripts" / "verify-before-ship.ps1"
BOX_LOCK = REPO_ROOT / "scripts" / "box_lock_gate.ps1"
SYSTEM32 = Path(os.environ.get("SystemRoot", "")) / "System32"
POWERSHELL = SYSTEM32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the gate is a Windows PowerShell script")


def _verdict_block() -> str:
    text = GATE.read_text(encoding="utf-8")
    match = re.search(r"# gate-verdict:begin\n(.*?)# gate-verdict:end", text.replace("\r\n", "\n"), re.DOTALL)
    assert match, "the gate lost its gate-verdict block"
    return match.group(1)


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=120, env=env,
    )


def _run_scenario(scenario: str) -> subprocess.CompletedProcess[str]:
    script = f'$ErrorActionPreference = "Stop"\n{_verdict_block()}\n{scenario}\n'
    return _run(["-Command", script])


def _last_line(run: subprocess.CompletedProcess[str]) -> str:
    lines = [line.rstrip() for line in run.stdout.splitlines() if line.strip()]
    assert lines, f"nothing on stdout; stderr={run.stderr!r}"
    return lines[-1]


def _gate_copy(root: Path) -> Path:
    scripts = root / "scripts"
    scripts.mkdir()
    shutil.copy2(GATE, scripts / GATE.name)
    shutil.copy2(BOX_LOCK, scripts / BOX_LOCK.name)
    return scripts / GATE.name


def _isolated_env(root: Path) -> dict[str, str]:
    # An empty profile has no box_lock.py, so the gate runs unlocked and never reaches the real helper.
    env = {**os.environ, "USERPROFILE": str(root)}
    # The gate runs this suite under its own lock, which exports VERIFY_BOX_LOCK_HELD; the copy under test must not see it.
    env.pop("VERIFY_BOX_LOCK_HELD", None)
    return env


@windows_only
def test_pass_ends_with_pass_and_exits_zero() -> None:
    run = _run_scenario("Complete-Gate 0")
    assert run.returncode == 0, run.stdout
    assert _last_line(run) == "GATE-RESULT: PASS"


@windows_only
def test_failed_stage_ends_with_fail_and_exits_one(tmp_path: Path) -> None:
    """The first stage runs the interpreter; one that exits non-zero must end the run on a FAIL verdict."""
    gate = _gate_copy(tmp_path)
    interpreter = tmp_path / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    shutil.copy2(SYSTEM32 / "whoami.exe", interpreter)  # exits non-zero on the unknown `-m` switch
    run = _run(["-File", str(gate)], env=_isolated_env(tmp_path))
    assert run.returncode == 1, run.stdout
    assert _last_line(run) == "GATE-RESULT: FAIL"
    assert "byte-compile failed" in run.stdout


@windows_only
def test_early_terminating_error_reports_on_stdout_then_fails() -> None:
    run = _run_scenario('throw "boom before any stage"')
    assert run.returncode == 1, run.stdout
    assert _last_line(run) == "GATE-RESULT: FAIL"
    assert "boom before any stage" in run.stdout, "the error must reach stdout, not only stderr"


@windows_only
def test_missing_venv_ends_with_fail_and_exits_one(tmp_path: Path) -> None:
    run = _run(["-File", str(_gate_copy(tmp_path))], env=_isolated_env(tmp_path))
    assert run.returncode == 1, run.stdout
    assert _last_line(run) == "GATE-RESULT: FAIL"
    assert ".venv missing" in run.stdout


@windows_only
def test_unrunnable_interpreter_is_reported_as_fail_not_a_silent_run(tmp_path: Path) -> None:
    """A terminating error in the real script: the interpreter path exists but is not an executable."""
    gate = _gate_copy(tmp_path)
    interpreter = tmp_path / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("not an executable", encoding="utf-8")
    run = _run(["-File", str(gate)], env=_isolated_env(tmp_path))
    assert run.returncode == 1, run.stdout
    assert _last_line(run) == "GATE-RESULT: FAIL"


def test_every_exit_goes_through_the_verdict() -> None:
    """A new `exit` that skips `Complete-Gate` would end a run with no verdict."""
    text = GATE.read_text(encoding="utf-8").replace("\r\n", "\n")
    outside = text.replace(_verdict_block(), "")
    assert not re.search(r"^\s*exit\s+\d", outside, re.MULTILINE), "exit through Complete-Gate, not directly"
    assert text.rstrip().endswith("Complete-Gate 0"), "the pass path must end on its verdict"


def test_verdict_block_is_ascii_for_windows_powershell_5_1() -> None:
    """Windows PowerShell 5.1 reads a BOM-less file as ANSI, so the block stays ASCII."""
    _verdict_block().encode("ascii")


@windows_only
def test_lock_timeout_ends_with_fail_and_still_exits_three(tmp_path: Path) -> None:
    """The inner gate never ran, so only the wrapper can print the verdict for exit 3."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(BOX_LOCK, scripts / BOX_LOCK.name)
    gate = scripts / "gate.ps1"
    gate.write_text(
        'Set-Location (Join-Path $PSScriptRoot "..")\n'
        '. (Join-Path $PSScriptRoot "box_lock_gate.ps1"); Enter-BoxLockGate\n'
        'Write-Host "the gate ran"\n'
        'exit 0\n',
        encoding="ascii",
    )
    helper = tmp_path / ".claude" / "skills" / "_lib" / "box_lock.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("import sys\nsys.exit(3)\n", encoding="ascii")
    python_dir = str(Path(sys.executable).parent)
    env = {**_isolated_env(tmp_path), "PATH": python_dir + os.pathsep + os.environ.get("PATH", "")}
    run = _run(["-File", str(gate)], env=env)
    assert run.returncode == 3, run.stdout
    assert "the gate ran" not in run.stdout
    assert "box load lock not taken in time" in run.stdout
    assert _last_line(run) == "GATE-RESULT: FAIL"
