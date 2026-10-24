r"""The gate's structure-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.structure_ratchet_gate

`scripts/structure_ratchet.py` (vendored, never edited here) exits 0 on a pass,
1 on a finding above its baseline or a root folder outside the declared shape,
2 on a bad table, baseline or arguments and 3 when it could not confirm (git
unusable, a file it could not read, a Python file that does not parse, an
unknown `shape`). Exit 3 means part of the tree was not measured, so it fails
the gate as its own state and never reads as a pass or a skip. This wrapper is
the one place that mapping lives, so a test can pin it; `verify-before-ship.ps1`
calls the wrapper, never the script. The runner is shared with the static-ratchet
stage.

Exit 0 only when the script exited 0; every other outcome exits 1 with its
reason on the last line.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.static_ratchet_gate import (
    EXIT_CONFIG, EXIT_NEW, EXIT_NOT_CONFIRMED, EXIT_PASS, run_stage,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "structure_ratchet.py"

_FAIL_LABELS = {
    EXIT_NEW: "NEW offender, a baselined one that grew, or a root folder outside the shape",
    EXIT_CONFIG: "bad [structure_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- part of the tree could not be measured, so it was not checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == EXIT_PASS:
        return True, "no file, function or directory grew and the root layout matches the shape"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """The structure-ratchet stage: the shared runner with this stage's verdicts."""
    return run_stage(script, "structure ratchet", classify_exit)


if __name__ == "__main__":
    sys.exit(run())
