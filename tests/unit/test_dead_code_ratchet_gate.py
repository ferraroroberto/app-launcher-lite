"""The dead-code stage fails closed and sits after the doc-refs ratchet in the gate.

The shared runner is pinned in test_static_ratchet_gate.py; this file checks the stage's
own verdicts and wiring. Exit 3 must read as NOT CONFIRMED, never as a pass or a skip.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from scripts.dead_code_ratchet_gate import classify_exit

ROOT = Path(__file__).resolve().parents[2]


def _gate_lines() -> list[str]:
    return (ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8").splitlines()


def test_exit_zero_is_the_only_pass_and_three_is_not_confirmed() -> None:
    verdicts = {code: classify_exit(code) for code in range(-1, 6)}
    assert [code for code, (ok, _) in verdicts.items() if ok] == [0]
    assert all(label for _, label in verdicts.values())
    assert "NOT CONFIRMED" in verdicts[3][1] and "skip" not in verdicts[3][1].lower()


def test_the_gate_runs_the_wrapper_once_right_after_the_doc_refs_ratchet() -> None:
    calls = [i for i, ln in enumerate(_gate_lines()) if ln.lstrip().startswith("&")]
    stages = [_gate_lines()[i].split("-m ")[-1] for i in calls if "-m scripts." in _gate_lines()[i]]
    assert stages.count("scripts.dead_code_ratchet_gate") == 1
    at = stages.index("scripts.dead_code_ratchet_gate")
    assert stages[at - 1] == "scripts.doc_refs_ratchet_gate"
    assert stages[at + 1] == "scripts.conventions_ratchet_gate"


def test_the_ratchet_is_enabled_with_a_baseline_and_a_vendored_script() -> None:
    fleet = tomllib.loads((ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    baseline = fleet["dead_code_ratchet"]["baseline"]
    script = fleet["vendored"]["dead_code_ratchet"]["dest"]
    assert fleet["dead_code_ratchet"]["enabled"] is True and (ROOT / baseline).is_file()
    assert script == "scripts/dead_code_ratchet.py" and (ROOT / script).is_file()
