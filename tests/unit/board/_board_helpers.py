"""Builders shared by the Board unit tests."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------ merge_sessions


def _live(session_id: str, project_dir: str, started_min_ago: int, **extra):
    row = {
        "session_id": session_id,
        "kind": "pty",
        "agent": "copilot",
        "project_dir": project_dir,
        "name": Path(project_dir).name,
        "alive": True,
        "started_at": (NOW - timedelta(minutes=started_min_ago)).timestamp(),
        "live_title": "",
        "prompt_title": "",
    }
    row.update(extra)
    return row


def _state_row(cwd: str, status: str = "working", updated_min_ago: int = 5, **extra):
    row = {
        "project": Path(cwd).name,
        "status": status,
        "transcript_path": None,
        "cwd": cwd,
        "updated_at": _iso(NOW - timedelta(minutes=updated_min_ago)),
    }
    row.update(extra)
    return row


# ------------------------------- transcript activity overlay (#305 / #309)


def _msg_line(kind: str, ts: datetime) -> str:
    """One real conversation line — the shape the #309 tail probe accepts."""
    return json.dumps({
        "type": kind,
        "timestamp": _iso(ts),
        "message": {"role": kind, "content": [{"type": "text", "text": "hi"}]},
    })


def _transcript_file(tmp_path: Path, mtime: datetime, content: str = None) -> str:
    """A transcript file with its mtime pinned to ``mtime``.

    Default content is a single assistant line stamped at ``mtime``, so mtime
    and last-activity agree (the plain #305 shape). Pass ``content`` to make
    them diverge — the #309 metadata-only-appends case.
    """
    target = tmp_path / "transcript.jsonl"
    if content is None:
        content = _msg_line("assistant", mtime) + "\n"
    target.write_text(content, encoding="utf-8")
    stamp = mtime.timestamp()
    os.utime(target, (stamp, stamp))
    return str(target)
