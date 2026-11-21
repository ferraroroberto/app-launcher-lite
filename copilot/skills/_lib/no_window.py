"""Windows console-suppression flag for every `subprocess` spawn in the skill tier.

The global `CLAUDE.md` convention ("Subprocess spawns must suppress the console
window (Windows)"): any spawn of an external executable must
pass `creationflags=subprocess.CREATE_NO_WINDOW` on Windows, because a parent
with no console of its own — pythonw, a tray app, a **scheduled task**, a daemon
— otherwise gets a console window flashed on screen for every call. That case is
this repo's normal case: every `run-weekly.bat` invokes `claude_progress.py`
under an app-launcher job with no console, and each unsuppressed `git`/`gh`/
`chrome` spawn beneath it flashes a window at whoever happens to be at the
machine.

The convention also says a repo with 3+ call sites factors the ternary into one
helper instead of repeating it. This module is that helper for the skill tier —
`skills/_lib/*` (via the tree's usual
`sys.path.insert(0, str(Path(__file__).resolve().parent))` idiom) and
`.claude/skills/*/*` (via its `parents[3] / "skills" / "_lib"` idiom).

The hook tier has its own copy in `hooks/_lib.py` and deliberately does not
import this one: `hooks/` and `skills/` are junctioned into the agent homes as
two independent units, and a hook must stay importable on a bare system Python
with nothing but its own directory on `sys.path`. `tests/run_acceptance.py`
asserts the two definitions agree, so the duplication cannot drift.

**Do not combine with `DETACHED_PROCESS`** — the two flags are mutually
exclusive. A long-lived child that later needs
`CTRL_BREAK_EVENT` combines this with `CREATE_NEW_PROCESS_GROUP` instead.

stdlib only.
"""

from __future__ import annotations

import subprocess
import sys

# `subprocess.CREATE_NO_WINDOW` is Windows-only; the conditional expression
# evaluates the platform test first, so the attribute is never touched on POSIX.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
