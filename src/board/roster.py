"""The chief's lane roster: each live session's state from the session-host's own record.

The Board and the sessions poll join transcripts, the state file and PTY
captures, and those reads slow down under load. The roster reads none of
them: one session-host list, mapped row by row, so it still answers while
transcript scans are stalled. What the host's record cannot tell is
``unknown``, never a guess.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SPAWNED = "spawned"
WORKING = "working"
AWAITING_INPUT = "awaiting-input"
UNKNOWN = "unknown"

# A working Copilot TUI repaints its spinner every second or so, so PTY output
# this recent means a turn is running; quieter than this, it waits for input.
WORKING_WINDOW_S = 5.0


def lane_state(info: Dict[str, Any], now: float) -> str:
    """``spawned`` before the agent's TUI has set its title, ``working`` while
    its PTY painted within :data:`WORKING_WINDOW_S`, ``awaiting-input`` when
    quieter, ``unknown`` for a detached session or a record with no paint time."""
    if info.get("kind", "pty") != "pty":
        return UNKNOWN
    if not info.get("live_title"):
        return SPAWNED
    last_output_at = info.get("last_output_at")
    if not isinstance(last_output_at, (int, float)):
        return UNKNOWN
    return WORKING if now - last_output_at < WORKING_WINDOW_S else AWAITING_INPUT


def _quiet_seconds(info: Dict[str, Any], now: float) -> Optional[float]:
    last_output_at = info.get("last_output_at")
    return round(now - last_output_at, 1) if isinstance(last_output_at, (int, float)) else None


def roster_rows(sessions: List[Dict[str, Any]], now: Optional[float] = None) -> List[Dict[str, Any]]:
    """One row per live session in the host's list; an exited session holds no repo."""
    now = time.time() if now is None else now
    rows = []
    for info in sessions:
        if not isinstance(info, dict) or not info.get("alive"):
            continue
        rows.append({
            "session_id": info.get("session_id"),
            "project": Path(str(info.get("project_dir") or "")).name or None,
            "agent": info.get("agent"),
            "kind": info.get("kind", "pty"),
            "label": info.get("label") or "",
            "state": lane_state(info, now),
            "quiet_seconds": _quiet_seconds(info, now),
        })
    return rows
