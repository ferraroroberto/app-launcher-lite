"""AI credits per live Copilot session, for the Coding tab's rows.

The figure comes off each session's screen, and reading a screen opens a socket
to the session-host, so the sessions poll never waits on one: it takes what
:mod:`src.session_view.copilot_figures` still knows and, for a session read
longer ago than the TTL, starts one read in the background. The first poll after
a session appears therefore carries no credits and a later one does, and so does
the first after a pause long enough for the figure to go unknown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Set

from src.session_view import copilot_figures
from src.session_view.vt_screen import read_vt_screen

logger = logging.getLogger(__name__)

# Credits move on the order of a turn, and the row prints one decimal: a
# reading this old is current enough, and it bounds the socket opens per poll.
_TTL_S = 15.0

# session id -> monotonic time its screen was last read. It goes with the session.
_read_at: Dict[str, float] = {}
_inflight: Set[str] = set()
_tasks: Set["asyncio.Task[None]"] = set()


def _reads_credits(session: Dict[str, Any]) -> bool:
    """Only a Copilot session with a terminal of its own has a footer to read."""
    return (
        str(session.get("agent") or "").lower() == "copilot"
        and str(session.get("kind") or "pty") != "remote"
        and bool(session.get("session_id"))
    )


async def _refresh(port: int, session: Dict[str, Any]) -> None:
    sid = str(session["session_id"])
    try:
        lines = await read_vt_screen(
            port, sid, int(session.get("rows") or 40), int(session.get("cols") or 120)
        )
        copilot_figures.observe(sid, lines)
        # A session never read yet is asked again on the next poll.
        if lines is not None or sid in _read_at:
            _read_at[sid] = time.monotonic()
    except Exception as exc:  # a failed read must never reach the poll
        logger.info("ℹ️ ai credits %s unreadable: %s", sid[:8], exc.__class__.__name__)
    finally:
        _inflight.discard(sid)


def attach_ai_credits(port: int, sessions: List[Dict[str, Any]]) -> None:
    """Set ``ai_credits`` on every session: the credits its screen shows, or
    ``None`` for a Claude session, a detached one and any whose footer shows
    none (or has not been read yet). Must run on the event loop."""
    live = {str(s.get("session_id")) for s in sessions}
    for sid in [sid for sid in _read_at if sid not in live]:
        del _read_at[sid]
        copilot_figures.forget(sid)
    now = time.monotonic()
    loop = asyncio.get_running_loop()
    for session in sessions:
        if not _reads_credits(session):
            session["ai_credits"] = None
            continue
        sid = str(session["session_id"])
        session["ai_credits"] = copilot_figures.recall(sid).ai_credits
        read_at = _read_at.get(sid)
        if sid in _inflight or (read_at is not None and now - read_at < _TTL_S):
            continue
        _inflight.add(sid)
        task = loop.create_task(_refresh(port, dict(session)))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
