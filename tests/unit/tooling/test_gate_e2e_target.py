"""The pre-ship gate hands pytest each routed e2e module as its own argument.

`classify_e2e.py` prints `E2E_PYTEST_TARGET` as space-joined paths when the diff
routes more than one module. Passed whole, that is one path containing spaces and
pytest collects nothing. The helper block in the gate (between the `e2e-target`
markers) is run for real, and its split target is given to a real pytest over two
modules, so a gate that stops splitting fails here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
GATE = REPO_ROOT / "scripts" / "verify-before-ship.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", "")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the gate is a Windows PowerShell script")


def _gate_text() -> str:
    return GATE.read_text(encoding="utf-8").replace("\r\n", "\n")


def _target_block() -> str:
    match = re.search(r"# e2e-target:begin\n(.*?)# e2e-target:end", _gate_text(), re.DOTALL)
    assert match, "the gate lost its e2e-target block"
    return match.group(1)


def _run(scenario: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    script = f'$ErrorActionPreference = "Stop"\n{_target_block()}\n{scenario}\n'
    return subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=120, cwd=cwd,
    )


@windows_only
def test_two_module_target_runs_both_modules(tmp_path: Path) -> None:
    for name in ("test_one.py", "test_two.py"):
        (tmp_path / name).write_text("def test_it() -> None:\n    pass\n", encoding="utf-8")
    scenario = (
        "$targets = Split-E2eTarget 'test_one.py test_two.py'\n"
        f"& '{sys.executable}' -m pytest --collect-only -q -p no:cacheprovider @targets\n"
        "exit $LASTEXITCODE"
    )
    run = _run(scenario, tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "test_one.py::test_it" in run.stdout
    assert "test_two.py::test_it" in run.stdout


@windows_only
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("tests/e2e", ["tests/e2e"]),
        ("  tests/e2e/a.py   tests/e2e/b.py ", ["tests/e2e/a.py", "tests/e2e/b.py"]),
        ("", []),
    ],
)
def test_split_yields_one_argument_per_path(tmp_path: Path, target: str, expected: list[str]) -> None:
    run = _run(f"$t = Split-E2eTarget '{target}'\nWrite-Host (\"count=\" + @($t).Count)\n@($t) | ForEach-Object {{ Write-Host \"arg=$_\" }}", tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    lines = run.stdout.split()
    assert lines[0] == f"count={len(expected)}"
    assert lines[1:] == [f"arg={path}" for path in expected]


@windows_only
@pytest.mark.parametrize(
    ("target", "recaptured"),
    [
        ("tests/e2e", True),
        ("tests/e2e/screens/test_screen_shots.py", True),
        ("tests/e2e/screens/test_screen_shots.py tests/e2e/board/test_board.py", True),
        ("tests/e2e/board/test_board.py tests/e2e/screens/test_screen_shots.py", True),
        ("tests/e2e/board/test_board.py tests/e2e/jobs/test_jobs.py", False),
        ("tests/e2e/board/test_board.py", False),
    ],
)
def test_shot_baseline_compares_only_when_the_slice_recaptured(tmp_path: Path, target: str, recaptured: bool) -> None:
    run = _run(f"$t = Split-E2eTarget '{target}'\nWrite-Host (Test-ShotsRecaptured $t)", tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip() == str(recaptured)


def test_gate_splits_the_target_before_building_pytest_args() -> None:
    """The helper is only worth having if the stage that calls pytest goes through it."""
    text = _gate_text()
    assert "Split-E2eTarget $e2eTarget" in text
    assert "@($e2eTarget," not in text, "the routed target must not be passed to pytest as one argument"
    assert "Test-ShotsRecaptured $e2eTargets" in text
