"""Shared resolution for the hooks-state base directory.

Lite: the one file of the ported `skills/_lib` closure that is not byte-identical
to fleet-config's. Copilot keeps its state under `~/.copilot/hooks/state/`, with
`COPILOT_HOOKS_STATE_DIR` as the override `hooks/session_state.py` already honours.

Every `skills/_lib` module that persists advisory state under
`~/.claude/hooks/state/` re-derived this exact `CLAUDE_HOOKS_STATE_DIR`
override lookup independently (`active_issue.py`, `chief_managed.py`,
`quota_snapshot.py`, `rate_gate.py` -- plus four more hook-tree copies fixed
alongside this one, `hooks/_lib.py`'s own `state_dir()`). This is a
correctness contract, not a style point: hermetic acceptance runs depend on
the env var being honoured everywhere, so a ninth caller that re-derives it
by hand risks getting it wrong and writing into live fleet state during a
test run.

Also owns the atomic-write temp naming + age sweep and the
shared write body around them for writers into that
directory, mirroring `hooks/_lib.py`'s copy -- the tree
boundary forbids either tier importing the other's.

stdlib only.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

STATE_DIR_ENV_VAR = "COPILOT_HOOKS_STATE_DIR"

# An abrupt death between mkstemp and os.replace runs no Python handler, so a
# writer's own except-branch unlink never fires and the temp is stranded.
# Nothing inside the dead process can clean up, so temps
# are named after their target and expired by age by the next writer instead.
# An hour is orders of magnitude beyond a real write's duration, so the sweep
# cannot race a live writer's temp.
ATOMIC_TMP_SWEEP_AFTER_SECONDS = 3600.0


def state_dir() -> Path:
    """The shared hooks-state base directory, honoring `COPILOT_HOOKS_STATE_DIR`."""
    root = os.environ.get(STATE_DIR_ENV_VAR)
    return Path(root) if root else Path.home() / ".copilot" / "hooks" / "state"


# The atomic-write helpers below mirror hooks/_lib.py's copy on purpose: the tree
# boundary forbids either tier importing the other, so the duplication check skips them.
# jscpd:ignore-start
def atomic_tmp_prefix(path: Path) -> str:
    """Writer-identifying `mkstemp` prefix for `path`'s write temporaries.

    An orphan is then `.<target>.<random>.tmp` -- it names the file whose
    writer stranded it, instead of the anonymous `tmp<8>.tmp` that left six
    accumulated temps in `hooks/state/` unattributable.
    Pair with `suffix=".tmp"`.
    """
    return f".{path.name}."


def sweep_stale_atomic_temps(path: Path) -> None:
    """Unlink `path`'s abandoned write temporaries, by age.

    The cleanup a hard-killed writer could never do itself: see
    `ATOMIC_TMP_SWEEP_AFTER_SECONDS`. `hooks/state/` is shared by several
    writers, so a name only matches when it is exactly this target's prefix
    plus one `mkstemp` random segment (no dots) plus `.tmp` -- a sibling
    target whose name merely *starts* with this one is never swept. Advisory:
    any failure leaves the orphan for the next writer rather than disturbing
    the write this sweep precedes.
    """
    pattern = re.compile(re.escape(atomic_tmp_prefix(path)) + r"[a-z0-9_]+\.tmp")
    cutoff = time.time() - ATOMIC_TMP_SWEEP_AFTER_SECONDS
    try:
        candidates = [entry for entry in path.parent.iterdir() if pattern.fullmatch(entry.name)]
    except OSError:
        return
    for stale in candidates:
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            continue


def atomic_write_text(
    path: Path, text: str, *, newline: Optional[str] = None, temp_stem: Optional[Path] = None,
    fsync: bool = False, replace_retries: int = 0,
) -> None:
    """Replace `path` with `text` (UTF-8) so a reader never sees half a file.

    Mirror of `hooks/_lib.py`'s copy: sweep the stem's
    stale orphans, `mkstemp` a sibling named by `atomic_tmp_prefix`, write,
    `os.replace`, and unlink the temp when any of that raises `OSError` --
    then re-raise, so each caller keeps its own failure policy. `newline` goes
    to `open()` unchanged (`None` = platform line endings).
    `temp_stem` (a sibling of `path`) names and sweeps the temps instead of
    `path` itself, for a writer whose every target is unique.

    `fsync=True` flushes and `os.fsync`s the temp file before the replace, for
    a writer whose target a sibling process reads expecting durability across
    a crash. `replace_retries` retries `os.replace` on `PermissionError` with
    capped exponential backoff (a Windows sharing violation from a concurrent
    reader), up to `replace_retries` attempts beyond the first, before
    letting the final `PermissionError` propagate. Both were `quota_snapshot
    .publish`'s own reasons for hand-rolling a second copy of this writer
    rather than calling this one; it now passes both.
    """
    stem = temp_stem or path
    sweep_stale_atomic_temps(stem)
    temporary: Optional[str] = None
    try:
        fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=atomic_tmp_prefix(stem), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(text)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        for attempt in range(replace_retries + 1):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == replace_retries:
                    raise
                time.sleep(0.01 * (2 ** attempt))
    except OSError:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise
# jscpd:ignore-end
