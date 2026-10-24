r"""The gate's comment-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.comment_ratchet_gate

`scripts/comment_ratchet.py` is vendored and never edited here. It exits 0 when
no file carries more `#NNN` references in its comments than the baseline allows
(or when the stage is not enabled), 1 on a rise, a new file with any, or a
missing baseline, 2 on a bad table, baseline or arguments, and 3 when `git` is
unusable or a file could not be scanned. A 3 means some files were never
counted, so it is reported as NOT CONFIRMED and fails; it is not a pass and not
a skip. The exit-code mapping lives only in this module so a test can pin it, and
`verify-before-ship.ps1` invokes the module rather than the script. The
subprocess runner is the one the static-ratchet stage uses.

Exit 0 only when the script exited 0; any other outcome exits 1 and the last
line says why.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.static_ratchet_gate import (
    EXIT_CONFIG, EXIT_NEW, EXIT_NOT_CONFIRMED, EXIT_PASS, run_stage,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "comment_ratchet.py"

_FAIL_LABELS = {
    EXIT_NEW: "NEW issue reference in a comment, a baselined file that grew, or missing baseline",
    EXIT_CONFIG: "bad [comment_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- a file could not be scanned, so it was not checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == EXIT_PASS:
        return True, "no file carries more issue references in comments than its baseline"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """The comment-ratchet stage: the shared runner with this stage's verdicts."""
    return run_stage(script, "comment ratchet", classify_exit)


if __name__ == "__main__":
    sys.exit(run())
