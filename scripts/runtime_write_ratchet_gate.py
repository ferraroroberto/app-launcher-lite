r"""The gate's runtime write stage: map the vendored script's exit code to a verdict.

    & .\.venv\Scripts\python.exe -m scripts.runtime_write_ratchet_gate

`scripts/runtime_write_ratchet.py` (with the resolver it imports,
`scripts/runtime_write_resolve.py`) is vendored and never edited here, so what each of
its exit codes means for the gate is decided in this module, where a test can pin it.
`verify-before-ship.ps1` runs the module for the static stage and the script itself only
for its --snapshot/--probe pair around the test runs. Only exit 0 passes; exit 3 means a
new write target could not be resolved, git was unusable or a tracked Python file could
not be parsed: that fails as NOT CONFIRMED, not as a pass and not as a skip.
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
SCRIPT = REPO_ROOT / "scripts" / "runtime_write_ratchet.py"

_FAIL_LABELS = {
    EXIT_NEW: "NEW write target resolving under a code folder, or missing baseline",
    EXIT_CONFIG: "bad [runtime_write_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- a new write target could not be resolved, so it was not checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == EXIT_PASS:
        return True, "no write under a code folder above its baseline"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """The runtime write stage: the shared runner with this stage's verdicts."""
    return run_stage(script, "runtime write ratchet", classify_exit)


if __name__ == "__main__":
    sys.exit(run())
