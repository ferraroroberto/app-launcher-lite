"""Hook-state-file IO for the Board tab (issue #408 split of ``board.py``).

Reads the two files the hook/workflow writers maintain:

* the **sessions-state file** (``session_state`` hook —
  ``~/.copilot/hooks/state/sessions-state.json``): ``working`` / ``needs-you``
  / ``idle`` rows per recent coding session.
* the **active-issues file** (the shared issue workflows —
  ``~/.copilot/hooks/state/active-issues.json``): issue branches currently in
  flight across the fleet.

Both share the same degradation contract (#164 acceptance): a
missing/corrupt/stale file must never error — callers get an
``available: False`` shape with empty/``None`` fields instead of an
exception, so a board render never breaks on a cold or torn state file.

Also home to the small time/parse helpers (:func:`_now`, :func:`_parse_iso`,
:func:`_iso_z`, :func:`_age_seconds`) shared by :mod:`src.board_sessions` and
:mod:`src.board_transcript` — every board-side timestamp flows through these
same two tolerant parsers.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# A state row (or the whole file) older than this is treated as gone-cold —
# matches the writer's own prune horizon in the session_state hook.
STATE_STALE_AFTER = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(raw: Any) -> Optional[datetime]:
    """Tolerant ISO-8601 parse; naive stamps are assumed local (job records)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _age_seconds(anchor: Optional[datetime], now: datetime) -> Optional[int]:
    if anchor is None:
        return None
    return max(0, int((now - anchor).total_seconds()))


def read_sessions_state(path: Path, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read the hook-written state file with full staleness tolerance.

    Absent / unreadable / corrupt → ``{"available": False, ...}`` with empty
    rows (precedent: ``team_os.recap_status`` and ``jobs._read_queue_file``).
    ``stale`` is true when the newest row is older than
    :data:`STATE_STALE_AFTER` — the hooks have stopped writing.
    """
    now = now or _now()
    empty: Dict[str, Any] = {"available": False, "stale": False, "updated_at": None, "rows": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty

    rows = {
        str(sid): row for sid, row in data.items() if isinstance(row, dict)
    }
    stamps = [
        stamp
        for stamp in (_parse_iso(row.get("updated_at")) for row in rows.values())
        if stamp is not None
    ]
    newest = max(stamps) if stamps else None
    return {
        "available": True,
        "stale": bool(newest is not None and now - newest > STATE_STALE_AFTER),
        "updated_at": _iso_z(newest) if newest else None,
        "rows": rows,
    }


def read_active_issues(path: Path, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read fresh cross-repo issue markers with tolerant degradation.

    Fleet-config's issue workflows key rows by ``<repo>#<number>`` and stamp
    ``started_at`` once the branch is ready. Missing/corrupt input is
    unavailable; malformed and older-than-24h rows are omitted individually so
    an abandoned workflow can never disable a backlog card forever.
    """
    now = now or _now()
    empty: Dict[str, Any] = {"available": False, "updated_at": None, "rows": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty

    cutoff = now - STATE_STALE_AFTER
    rows: Dict[str, Any] = {}
    stamps = []
    for row in data.values():
        if not isinstance(row, dict):
            continue
        repo = row.get("repo")
        number = row.get("number")
        started_at = _parse_iso(row.get("started_at"))
        if (
            not isinstance(repo, str)
            or not repo.strip()
            or not isinstance(number, int)
            or isinstance(number, bool)
            or number <= 0
            or started_at is None
            or started_at < cutoff
        ):
            continue
        rows[f"{repo.strip().lower()}#{number}"] = row
        stamps.append(started_at)

    newest = max(stamps) if stamps else None
    return {
        "available": True,
        "updated_at": _iso_z(newest) if newest else None,
        "rows": rows,
    }
