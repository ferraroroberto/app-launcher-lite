r"""The gate's instruction-ratchet stage: map the vendored script's exit code.

    & .\.venv\Scripts\python.exe -m scripts.instruction_ratchet_gate

`scripts/instruction_ratchet.py` is vendored and never edited here, so the verdict
for each of its exit codes is decided in this module, where a test can pin it.
`verify-before-ship.ps1` runs the module, never the script. Only exit 0 passes;
exit 3 means an instruction file could not be read or parsed, so it was never
checked: it fails as NOT CONFIRMED, not as a pass and not as a skip.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.static_ratchet_gate import (
    EXIT_CONFIG, EXIT_NEW, EXIT_NOT_CONFIRMED, EXIT_PASS, run_stage,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "instruction_ratchet.py"

_FAIL_LABELS = {
    EXIT_NEW: "NEW instruction-file rule hit, a file past its size cap, or missing baseline",
    EXIT_CONFIG: "bad [instruction_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- an instruction file could not be read, so it was not checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == EXIT_PASS:
        return True, "no instruction file has more rule hits than its baseline"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """The instruction-ratchet stage: the shared runner with this stage's verdicts."""
    return run_stage(script, "instruction ratchet", classify_exit)


if __name__ == "__main__":
    sys.exit(run())
