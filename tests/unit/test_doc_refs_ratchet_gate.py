"""The doc-refs stage fails closed and sits after the instruction ratchet in the gate.

The shared runner is pinned in test_static_ratchet_gate.py; this file checks the stage's
own verdicts and wiring. Exit 3 must read as NOT CONFIRMED, never as a pass or a skip.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from scripts.doc_refs_ratchet_gate import classify_exit

ROOT = Path(__file__).resolve().parents[2]
GATE = (ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
FLEET = tomllib.loads((ROOT / ".fleet.toml").read_text(encoding="utf-8"))


def test_exit_zero_is_the_only_pass_and_three_is_not_confirmed() -> None:
    verdicts = {code: classify_exit(code) for code in range(-1, 6)}
    assert [code for code, (ok, _) in verdicts.items() if ok] == [0]
    assert all(label for _, label in verdicts.values())
    assert "NOT CONFIRMED" in verdicts[3][1] and "skip" not in verdicts[3][1].lower()


def test_the_gate_runs_the_wrapper_once_right_after_the_instruction_ratchet() -> None:
    lines = [ln for ln in GATE.splitlines() if ln.lstrip().startswith("&") and "doc_refs" in ln]
    assert len(lines) == 1 and "-m scripts.doc_refs_ratchet_gate" in lines[0]
    assert GATE.index("-m scripts.instruction_ratchet_gate") < GATE.index(lines[0])
    assert GATE.index(lines[0]) < GATE.index("-m scripts.comment_ratchet_gate")


def test_the_ratchet_is_enabled_with_a_baseline_and_a_vendored_script() -> None:
    table, vendored = FLEET["doc_refs_ratchet"], FLEET["vendored"]["doc_refs_ratchet"]
    assert table["enabled"] is True and (ROOT / table["baseline"]).is_file()
    assert vendored["dest"] == "scripts/doc_refs_ratchet.py" and (ROOT / vendored["dest"]).is_file()
