"""What the running (or last finished) Sync all has done, in memory only.

The shape is the History refresh's (``done`` of ``total``, the ``current``
project, ``state`` running or done) so both screens count the same way. A
restart forgets it; the report the run returned stays with the page that asked.
Replaced whole under a lock, never edited in place, so a reader on another
thread sees one consistent run. Sync all runs repos on several worker threads,
so ``current`` is the project that started last, not the only one in flight.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

IDLE = "idle"
RUNNING = "running"
DONE = "done"

_lock = threading.Lock()
_progress: Optional[Dict[str, Any]] = None
_run_seq = 0


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def begin(total: int) -> None:
    """Open a run over ``total`` repos, replacing the previous run's progress."""
    global _progress, _run_seq
    with _lock:
        _run_seq += 1
        _progress = {
            "run": _run_seq, "state": RUNNING, "done": 0, "total": total, "current": None,
            "results": [], "error": None, "started_at": _stamp(), "finished_at": None,
        }


def started(name: str) -> None:
    """A repo's sync began; it is what the page names as the current project."""
    global _progress
    with _lock:
        if _progress is not None:
            _progress = {**_progress, "current": name}


def finished(result: Dict[str, Any]) -> None:
    """A repo's sync ended with ``result`` (the row Sync all reports for it)."""
    global _progress
    with _lock:
        if _progress is not None:
            results = [*_progress["results"], result]
            _progress = {**_progress, "results": results, "done": len(results)}


def end(error: Optional[str] = None) -> None:
    """Close the run; ``error`` is set only when the run itself stopped."""
    global _progress
    with _lock:
        if _progress is not None:
            _progress = {**_progress, "state": DONE, "current": None, "error": error, "finished_at": _stamp()}


def snapshot() -> Dict[str, Any]:
    """The run's progress, or an idle placeholder before the first one since the server started."""
    with _lock:
        if _progress is None:
            return {
                "run": 0, "state": IDLE, "done": 0, "total": None, "current": None,
                "results": [], "error": None, "started_at": None, "finished_at": None,
            }
        return {**_progress, "results": list(_progress["results"])}
