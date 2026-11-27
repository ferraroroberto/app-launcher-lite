"""The context percent and AI credits each live Copilot session last showed, one
memory for every reader of them (the context route and the sessions poll).

A footer caught mid-repaint reads nothing for an instant, so a figure outlives
such a read by :data:`KEEP_S`. Past that it is unknown (``None``), never an old
number and never a zero, and the log gets one line naming the footer rows that
were on screen, so a Copilot release that moves or renames a figure can be
diagnosed from it. The line is written again only after the figure has been
read in between.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, NamedTuple, Optional, Tuple

from src.session_view.copilot_credits import ai_credits_used
from src.session_view.copilot_footer import footer
from src.session_view.copilot_session import context_percent

logger = logging.getLogger(__name__)

# A few reads of the overlay's 10 s poll or the sessions poll's 15 s cache.
KEEP_S = 60.0
# How much of each footer row the log line carries.
_LOGGED_ROW_CHARS = 160


class Figures(NamedTuple):
    """``None`` is unknown."""

    percent: Optional[int]
    ai_credits: Optional[float]


@dataclass
class _Memory:
    # (value, monotonic time it was read off the screen)
    percent: Optional[Tuple[int, float]] = None
    credits: Optional[Tuple[float, float]] = None
    # The figures already reported as unrecognised.
    logged: FrozenSet[str] = field(default_factory=frozenset)


_sessions: Dict[str, _Memory] = {}


def _kept(reading: Optional[Tuple[float, float]], now: float):
    return reading[0] if reading is not None and now - reading[1] <= KEEP_S else None


def _log_unrecognised(sid: str, memory: _Memory, figures: Figures, lines: List[str]) -> None:
    unknown = frozenset(
        name for name, value in (("context", figures.percent), ("credits", figures.ai_credits))
        if value is None
    )
    if unknown - memory.logged:
        rows = footer(lines)
        seen = [" ".join(row.split())[:_LOGGED_ROW_CHARS] for row in rows.above + rows.below]
        logger.info(
            "ℹ️ copilot footer %s: no %s figure recognised; footer rows seen: %s",
            sid[:8], " or ".join(sorted(unknown)), seen,
        )
    memory.logged = unknown


def observe(sid: str, lines: Optional[List[str]]) -> Figures:
    """Take a reading of the session's screen (``None``: it could not be read)
    and answer what is known about it now."""
    now = time.monotonic()
    memory = _sessions.setdefault(sid, _Memory())
    if lines is not None:
        percent, credits = context_percent(lines), ai_credits_used(lines)
        if percent is not None:
            memory.percent = (percent, now)
        if credits is not None:
            memory.credits = (credits, now)
    figures = Figures(_kept(memory.percent, now), _kept(memory.credits, now))
    if lines is not None:
        _log_unrecognised(sid, memory, figures, lines)
    return figures


def recall(sid: str) -> Figures:
    """What is still known about the session, without a new reading."""
    memory = _sessions.get(sid) or _Memory()
    now = time.monotonic()
    return Figures(_kept(memory.percent, now), _kept(memory.credits, now))


def forget(sid: str) -> None:
    """Drop a session that is gone."""
    _sessions.pop(sid, None)


def reset() -> None:
    """Drop every session (tests)."""
    _sessions.clear()
