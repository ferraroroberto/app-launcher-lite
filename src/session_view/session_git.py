"""Files a session changed on disk that its transcript never recorded.

The transcript fold (:mod:`src.session_view.changes`) sees only the edits an
agent made through its own edit/write tools. A file written by a shell command
(``Set-Content``, ``echo >``, a script, a formatter run) leaves no such step, so
this module reads ``git status`` of the session's folder and offers the files
the fold missed, each marked ``source: "git"``.

The baseline is the session's start: a file counts only when its modification
time is at or after the transcript's first timestamp, so work that was already
sitting in the tree when the session began is never attributed to it. A resumed
session keeps its original start, and a file already dirty before the session
that the session edited again shows its whole difference against ``HEAD``, not
only its own part; both are why the row says it came from git. A file the
shell deleted cannot be dated, so it is left out rather than guessed.

Every state that stops this from running is named (``git`` in the response),
never folded into "no files": no project folder or not a git repository,
git unreachable, a transcript with no readable start time.

Read-only: every git call goes through :func:`src.git_utils.run_git`.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.git_utils import run_git
from src.session_view.changes import _key
from src.session_view.git_changes import (
    ChangedFile,
    file_diff,
    is_git_repo,
    list_changes,
    safe_relative_path,
)

logger = logging.getLogger(__name__)

STATE_OK = "ok"
STATE_NOT_A_REPO = "not_a_repo"
STATE_BASELINE_UNKNOWN = "baseline_unknown"
STATE_UNAVAILABLE = "unavailable"

#: Lines read from the top of a transcript to find its first timestamp.
_START_SCAN_LINES = 50
#: A numeric timestamp past this is milliseconds, below it seconds.
_MILLIS_FLOOR = 100_000_000_000


def _epoch(value: Any) -> Optional[float]:
    """``value`` (ISO-8601 text, or epoch seconds/milliseconds) as epoch seconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > _MILLIS_FLOOR else float(value)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def session_started(transcript: Path) -> Optional[float]:
    """Epoch seconds of the first timestamped line of ``transcript``; None when
    none of its first lines carries one."""
    try:
        with transcript.open("rb") as fh:
            for _ in range(_START_SCAN_LINES):
                raw = fh.readline()
                if not raw:
                    break
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                stamp = _epoch(obj.get("timestamp")) if isinstance(obj, dict) else None
                if stamp is not None:
                    return stamp
    except OSError as exc:
        logger.info("ℹ️ session git: cannot read the transcript start: %s", exc)
    return None


def _session_files(project_dir: Optional[str], transcript: Path) -> Tuple[str, List[ChangedFile]]:
    """``(state, files)``: the working-tree files modified since the session
    began, or why there are none to offer."""
    if not project_dir or not Path(project_dir).is_dir() or not is_git_repo(Path(project_dir)):
        return STATE_NOT_A_REPO, []
    root = Path(project_dir)
    if run_git(root, ["rev-parse", "--is-inside-work-tree"]) is None:
        logger.info("⚠️ session git: git did not answer in the session folder")
        return STATE_UNAVAILABLE, []
    started = session_started(transcript)
    if started is None:
        logger.info("⚠️ session git: the transcript has no start time, so no file can be dated to the session")
        return STATE_BASELINE_UNKNOWN, []
    files: List[ChangedFile] = []
    for item in list_changes(root).files:
        if item.status == "D":
            continue
        try:
            modified = os.stat(root / item.path).st_mtime
        except OSError:
            continue
        if modified >= started:
            files.append(item)
    return STATE_OK, files


def _row(project_dir: str, item: ChangedFile) -> Dict[str, Any]:
    return {
        "path": item.path, "key": str(Path(project_dir) / item.path), "status": item.status,
        "additions": item.additions or 0, "deletions": item.deletions or 0, "steps": 0,
        "source": "git", "staged": item.staged, "old_path": item.old_path, "binary": item.binary,
    }


def merge_git_files(found: Dict[str, Any], project_dir: Optional[str], transcript: Path) -> Dict[str, Any]:
    """``found`` (:func:`changed_files`' answer for the newest window) plus a row
    for every file git shows the session changed that the transcript did not
    name, and a ``git`` block saying what was read."""
    state, files = _session_files(project_dir, transcript)
    rows = [{**r, "source": "transcript"} for r in found["files"]]
    seen = {r["path"].lower() for r in rows}
    added = [_row(str(project_dir), f) for f in files if f.path.lower() not in seen]
    rows = sorted(rows + added, key=lambda r: r["path"].lower())
    logger.info("ℹ️ session git: state=%s, %d changed since start, %d not in the transcript",
                state, len(files), len(added))
    return {
        **found,
        "files": rows,
        "counts": {
            "additions": sum(r["additions"] for r in rows),
            "deletions": sum(r["deletions"] for r in rows),
        },
        "git": {"state": state, "added": len(added)},
    }


def _git_item(project_dir: Optional[str], transcript: Path, requested: str) -> Optional[ChangedFile]:
    """The git-listed file ``requested`` names, or None. The jail of the git
    fallback: a path is answered only when it is in the session's own list."""
    state, files = _session_files(project_dir, transcript)
    if state != STATE_OK or not project_dir:
        return None
    root = _key(str(project_dir))
    wanted = _key(requested)
    if not wanted.startswith(root + "/"):
        return None
    rel = wanted[len(root) + 1:]
    return next((f for f in files if _key(f.path) == rel), None)


def git_changed_path(project_dir: Optional[str], transcript: Path, requested: str) -> Optional[str]:
    """The absolute path of a file only git shows the session changed, else None."""
    item = _git_item(project_dir, transcript, requested)
    if item is None or not project_dir:
        return None
    return str(safe_relative_path(Path(project_dir), item.path))


def git_diff_of(project_dir: Optional[str], transcript: Path, requested: str) -> Optional[Dict[str, Any]]:
    """The unified diff against ``HEAD`` of a file only git shows the session
    changed (capped like the Coding tab's), else None."""
    item = _git_item(project_dir, transcript, requested)
    if item is None or not project_dir:
        return None
    diff = file_diff(Path(project_dir), item.path)
    return {"diff": diff.diff, "binary": diff.binary, "truncated": diff.truncated}
