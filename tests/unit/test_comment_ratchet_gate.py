"""The gate's comment-ratchet stage maps the vendored script's exit codes.

Exit 3 from `scripts/comment_ratchet.py` means a file could not be scanned, so it
was not counted. It must fail as NOT CONFIRMED (never pass, never skip), and the
pre-ship gate must call the wrapper that guarantees that rather than the script.
The shared runner is pinned in test_static_ratchet_gate.py; these tests use fake
scripts in a temp directory.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from scripts import comment_ratchet_gate as gate
from tests.test_static_ratchet_gate import _fake_script

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PS1 = REPO_ROOT / "scripts" / "verify-before-ship.ps1"


def test_only_exit_zero_passes_and_exit_three_is_not_confirmed() -> None:
    passing = [code for code in range(-1, 5) if gate.classify_exit(code)[0]]
    assert passing == [0]
    label = gate.classify_exit(3)[1]
    assert "NOT CONFIRMED" in label
    assert "skip" not in label.lower()


@pytest.mark.parametrize("code", [1, 2, 3])
def test_a_nonzero_exit_fails_the_stage_and_echoes_the_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    assert gate.run(_fake_script(tmp_path, code, "src/x.py has 3 references")) == 1
    out = capsys.readouterr().out
    assert "src/x.py has 3 references" in out
    assert out.rstrip().splitlines()[-1].startswith("FAIL")


def test_exit_zero_passes_the_stage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.run(_fake_script(tmp_path, 0, "all clear")) == 0
    assert capsys.readouterr().out.rstrip().splitlines()[-1].startswith("OK")


def test_the_pre_ship_gate_runs_the_wrapper_between_the_structure_ratchet_and_e2e_routing() -> None:
    text = GATE_PS1.read_text(encoding="utf-8")
    calls = re.findall(r"^\s*&.*comment_ratchet.*$", text, flags=re.MULTILINE)
    assert len(calls) == 1
    assert "-m scripts.comment_ratchet_gate" in calls[0]
    assert "comment_ratchet.py" not in calls[0]
    structure_at = text.index("-m scripts.structure_ratchet_gate")
    route_at = text.index("classify_e2e.py")
    assert structure_at < text.index("-m scripts.comment_ratchet_gate") < route_at


def test_the_ratchet_is_enabled_with_a_baseline_and_a_vendored_script() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert fleet["comment_ratchet"]["enabled"] is True
    assert (REPO_ROOT / fleet["comment_ratchet"]["baseline"]).is_file()
    vendored = fleet["vendored"]["comment_ratchet"]
    assert vendored["dest"] == "scripts/comment_ratchet.py"
    assert (REPO_ROOT / vendored["dest"]).is_file()
