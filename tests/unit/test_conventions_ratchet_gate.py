"""The conventions stage fails closed and sits after the dead-code ratchet in the gate.

The shared runner is pinned in test_static_ratchet_gate.py; this file checks the stage's
own verdicts and wiring. Exit 3 must read as NOT CONFIRMED, never as a pass or a skip.
"""

from __future__ import annotations

import tomllib

from scripts.conventions_ratchet_gate import classify_exit
from tests.unit._gate_wiring import ROOT, assert_fails_closed, gate_stages


def test_exit_zero_is_the_only_pass_and_three_is_not_confirmed() -> None:
    assert_fails_closed(classify_exit)


def test_the_gate_runs_the_wrapper_once_right_after_the_dead_code_ratchet() -> None:
    stages = gate_stages()
    assert stages.count("scripts.conventions_ratchet_gate") == 1
    at = stages.index("scripts.conventions_ratchet_gate")
    assert stages[at - 1] == "scripts.dead_code_ratchet_gate"
    assert stages[at + 1] == "scripts.comment_ratchet_gate"


def test_the_ratchet_is_enabled_with_a_baseline_and_a_vendored_script() -> None:
    fleet = tomllib.loads((ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    baseline = fleet["conventions_ratchet"]["baseline"]
    script = fleet["vendored"]["conventions_ratchet"]["dest"]
    assert fleet["conventions_ratchet"]["enabled"] is True and (ROOT / baseline).is_file()
    assert script == "scripts/conventions_ratchet.py" and (ROOT / script).is_file()
