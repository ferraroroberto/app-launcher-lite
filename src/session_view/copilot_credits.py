"""The AI credits a Copilot session has spent, read off its terminal's footer.

Copilot CLI paints the running total on the bottom row of its screen, for
example ``Session: 5.62 AIC used``. That number is the only source: nothing
Copilot writes to disk carries it, and this repo does no accounting of its own,
so a session that restarts starts again from whatever its screen says.

Only the bottom-most non-blank row counts, the same row
:mod:`src.session_view.copilot_session` reads the context figure from: an
older frame or a quoted line higher up says nothing about now. A row cut short
by a narrow terminal, a Copilot that paints no credits and the ``Plan: 459/1500
(30% used)`` allowance row all read ``None``: unknown, never a zero.

Pure: the caller renders the screen (:mod:`src.session_view.vt_screen`).
"""

from __future__ import annotations

import re
from typing import List, Optional

# ``Session: 5.62 AIC used``; the thousands comma and the trailing word are
# tolerated, the unit is not: a figure in any other unit is not credits.
_CREDITS_RE = re.compile(r"\bSession:\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*AIC\b")


def ai_credits_used(lines: List[str]) -> Optional[float]:
    """The credits the bottom-most footer row says this session has used, or
    ``None`` when that row shows none."""
    status = next((line for line in reversed(lines or []) if line.strip()), None)
    match = _CREDITS_RE.search(status or "")
    if not match:
        return None
    return float(match.group(1).replace(",", "") + (match.group(2) or ""))
