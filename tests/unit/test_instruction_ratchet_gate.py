"""The instruction-ratchet stage fails closed and is wired into the pre-ship gate.

The shared runner is pinned in test_static_ratchet_gate.py; here only this stage's
verdicts and wiring are checked. Exit 3 (an instruction file could not be read) must
read as NOT CONFIRMED, never pass or skip, and the gate must call the wrapper that
guarantees that rather than the vendored script.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from scripts.instruction_ratchet_gate import classify_exit, run

ROOT = Path(__file__).resolve().parents[2]


def test_only_exit_zero_passes() -> None:
    assert [c for c in range(-1, 5) if classify_exit(c)[0]] == [0]


@pytest.mark.parametrize("code", [1, 2, 3, 4])
def test_every_other_exit_is_a_labelled_failure(code: int) -> None:
    ok, label = classify_exit(code)
    assert not ok and label


def test_exit_three_is_not_confirmed_and_never_a_skip() -> None:
    label = classify_exit(3)[1]
    assert "NOT CONFIRMED" in label and "skip" not in label.lower()


def test_run_defaults_to_the_vendored_script() -> None:
    assert run.__defaults__ == (ROOT / "scripts" / "instruction_ratchet.py",)


def test_the_gate_calls_the_wrapper_between_the_structure_ratchet_and_e2e_routing() -> None:
    text = (ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    calls = re.findall(r"^\s*&.*instruction_ratchet.*$", text, flags=re.MULTILINE)
    assert len(calls) == 1 and "-m scripts.instruction_ratchet_gate" in calls[0]
    assert text.index("-m scripts.structure_ratchet_gate") < text.index(calls[0]) < text.index("classify_e2e.py")


def test_the_ratchet_is_enabled_with_a_baseline_and_a_vendored_script() -> None:
    fleet = tomllib.loads((ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert fleet["instruction_ratchet"]["enabled"] is True
    assert (ROOT / fleet["instruction_ratchet"]["baseline"]).is_file()
    vendored = fleet["vendored"]["instruction_ratchet"]
    assert vendored["dest"] == "scripts/instruction_ratchet.py"
    assert (ROOT / vendored["dest"]).is_file()
