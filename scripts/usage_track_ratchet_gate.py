r"""The gate's usage track stage: map the vendored script's exit code to a verdict.

    & .\.venv\Scripts\python.exe -m scripts.usage_track_ratchet_gate

`scripts/usage_track_ratchet.py` (with the reader it loads, `scripts/usage_track_scan.py`)
is vendored and never edited here. Exit 3 means a file could not be read or its JS never
closed, so nothing was fully checked: NOT CONFIRMED, never a pass or a skip.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.static_ratchet_gate import (
    EXIT_CONFIG,
    EXIT_NEW,
    EXIT_NOT_CONFIRMED,
    EXIT_PASS,
    run_stage,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "usage_track_ratchet.py"

_FAIL_LABELS = {
    EXIT_NEW: "a rule's hits rose above the baseline, the usage log is not wired, or the baseline is missing",
    EXIT_CONFIG: "bad [usage_track_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- a file could not be read or closed, so it was not checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == EXIT_PASS:
        return True, "no untracked control, sheet or built id above its baseline"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """The usage track stage: the shared runner with this stage's verdicts."""
    return run_stage(script, "usage track ratchet", classify_exit)


if __name__ == "__main__":
    sys.exit(run())
