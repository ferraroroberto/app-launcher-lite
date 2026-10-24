r"""The gate's dead-code stage: map the vendored script's exit code to a verdict.

    & .\.venv\Scripts\python.exe -m scripts.dead_code_ratchet_gate

`scripts/dead_code_ratchet.py` is vendored and never edited here, so what each of its
exit codes means for the gate is decided in this module, where a test can pin it.
`verify-before-ship.ps1` runs the module, never the script. Only exit 0 passes; exit 3
means git was unusable or a Python file could not be read or parsed, so the references
were never resolved: that fails as NOT CONFIRMED, not as a pass and not as a skip.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.static_ratchet_gate import (
    EXIT_CONFIG, EXIT_NEW, EXIT_NOT_CONFIRMED, EXIT_PASS, run_stage,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dead_code_ratchet.py"

_FAIL_LABELS = {
    EXIT_NEW: "NEW function, class or method nothing references, or missing baseline",
    EXIT_CONFIG: "bad [dead_code_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- git or a Python file could not be read or parsed, so references were not resolved",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == EXIT_PASS:
        return True, "no function, class or method nothing references beyond its baseline"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """The dead-code stage: the shared runner with this stage's verdicts."""
    return run_stage(script, "dead code ratchet", classify_exit)


if __name__ == "__main__":
    sys.exit(run())
