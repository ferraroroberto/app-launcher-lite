"""The gate's structure-ratchet stage maps the vendored script's exit codes.

The runner (timeout, unstartable script, venv interpreter) is shared with the
static-ratchet stage and pinned in test_static_ratchet_gate.py.

`scripts/structure_ratchet.py` exits 3 when it could not confirm (git unusable,
an unreadable or unparseable file, an unknown `shape`): part of the tree was not
measured. These tests pin that 3 fails as NOT CONFIRMED -- never a pass, never a
skip -- and that the pre-ship gate calls the wrapper that guarantees it rather
than the script. They run fake scripts in a temp directory.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts import structure_ratchet_gate as gate
from tests.test_static_ratchet_gate import _fake_script

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_only_exit_zero_passes_and_exit_three_is_never_a_skip() -> None:
    verdicts = {code: gate.classify_exit(code) for code in range(-1, 5)}
    assert [code for code, (ok, _) in verdicts.items() if ok] == [0]
    assert "NOT CONFIRMED" in verdicts[3][1]
    assert "skip" not in verdicts[3][1].lower()


def test_run_fails_every_nonzero_exit_and_echoes_the_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gate.run(_fake_script(tmp_path, 0, "all clear")) == 0
    assert capsys.readouterr().out.rstrip().splitlines()[-1].startswith("OK")
    for code in (1, 2, 3):
        assert gate.run(_fake_script(tmp_path, code, "size:src/x.py grew")) == 1
        out = capsys.readouterr().out
        assert "size:src/x.py grew" in out
        assert out.rstrip().splitlines()[-1].startswith("FAIL")
    assert "NOT CONFIRMED" in out  # the last code run was 3


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    lines = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(
        encoding="utf-8"
    ).splitlines()
    calls = [i for i, line in enumerate(lines) if line.lstrip().startswith("&") and "structure_ratchet" in line]
    assert len(calls) == 1
    call = lines[calls[0]]
    assert "-m scripts.structure_ratchet_gate" in call
    assert "structure_ratchet.py" not in call
    # After the docs ratchet, before the e2e routing.
    docs_at = next(i for i, line in enumerate(lines) if "-m scripts.docs_ratchet_gate" in line)
    route_at = next(i for i, line in enumerate(lines) if "classify_e2e.py" in line and "$python" in line)
    assert docs_at < calls[0] < route_at


def test_the_ratchet_is_declared_and_its_script_is_vendored() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert fleet["shape"] == "webapp-tray"
    assert fleet["structure_ratchet"]["enabled"] is True
    assert (REPO_ROOT / fleet["structure_ratchet"]["baseline"]).is_file()
    entry = fleet["vendored"]["structure_ratchet"]
    assert entry["dest"] == "scripts/structure_ratchet.py"
    assert (REPO_ROOT / entry["dest"]).is_file()
