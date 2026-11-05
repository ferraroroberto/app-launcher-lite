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


# ------------------------------------------------------- transcript files


def _transcript_file(tmp_path: Path, mtime: datetime) -> str:
    """A transcript file with its mtime pinned to ``mtime`` — the only thing
    the Board reads of it (external-row liveness)."""
    target = tmp_path / "transcript.jsonl"
    target.write_text(json.dumps({"type": "session.start"}) + "\n", encoding="utf-8")
    stamp = mtime.timestamp()
    os.utime(target, (stamp, stamp))
    return str(target)
