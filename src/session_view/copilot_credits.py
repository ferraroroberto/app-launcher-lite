"""The AI credits a Copilot session has spent, read off its terminal's footer.

Copilot CLI paints the running total in its footer, for example
``Session: 5.62 AIC used``. That number is the only source: nothing Copilot
writes to disk carries it, and this repo does no accounting of its own, so a
session that restarts starts again from whatever its screen says.

The figure is picked by its label and unit from any footer row
(:mod:`src.session_view.copilot_footer`), the rows under the input box before
the status block above it, nearest the bottom first. A footer cell wrapped over
two rows (``Session: N`` above its ``AIC`` unit) reads too. A row cut short by a
narrow terminal, a Copilot that paints no credits and the
``Plan: 459/1500 (30% used)`` allowance row all read ``None``: unknown, never a
zero.

Pure: the caller renders the screen (:mod:`src.session_view.vt_screen`).
"""

from __future__ import annotations

import re
from typing import List, Optional

from src.session_view.copilot_footer import footer

# The label, unless a word runs into it: "the footer says Session: 5 AIC" is
# prose, not a footer cell.
_LABEL = r"(?<![A-Za-z] )\bSession:\s*"
# ``Session: 5.62 AIC used``; the unit is required: a figure in any other unit
# is not credits. The number's separators are decided by ``_amount``.
_CREDITS_RE = re.compile(_LABEL + r"([\d.,]*\d)\s*AIC\b")
# ``Session: 39.74`` ending its row or followed by a cell separator: the unit
# is on the next row. Any other word after the number is some other unit.
_SESSION_FIGURE_RE = re.compile(_LABEL + r"([\d.,]*\d)\s*(?:[·|•].*)?$")
_UNIT_RE = re.compile(r"\bAIC\b")

_COMMA_GROUPS = re.compile(r"\d{1,3}(,\d{3})+")
_DOT_GROUPS = re.compile(r"\d{1,3}(\.\d{3})+")


def _amount(token: str) -> Optional[float]:
    """The number a ``Session:`` figure spells, or ``None`` when its separators
    can't be read one way only.

    A comma is a thousands separator (``1,234.5``). A dot is a decimal point
    unless it is the thousands separator of a European figure: more than one
    dot (``1.234.567``) or a comma decimal after it (``1.234,5``). A lone
    ``1.234`` is both 1.234 and 1234, so it reads ``None``, except ``0.234``
    where a thousands group cannot start with a zero.
    """
    has_comma, has_dot = "," in token, "." in token
    if not has_comma and not has_dot:
        return float(token)
    if has_comma and not has_dot:
        return float(token.replace(",", "")) if _COMMA_GROUPS.fullmatch(token) else None
    if has_dot and not has_comma:
        if token.count(".") > 1:
            return float(token.replace(".", "")) if _DOT_GROUPS.fullmatch(token) else None
        whole, fraction = token.split(".")
        if len(fraction) == 3 and whole != "0":
            return None
        return float(token)
    if token.rfind(",") < token.rfind("."):
        whole, fraction = token.rsplit(".", 1)
        return float(whole.replace(",", "") + "." + fraction) if _COMMA_GROUPS.fullmatch(whole) else None
    whole, fraction = token.rsplit(",", 1)
    return float(whole.replace(".", "") + "." + fraction) if _DOT_GROUPS.fullmatch(whole) else None


def ai_credits_used(lines: List[str]) -> Optional[float]:
    """The credits the footer says this session has used, or ``None`` when it
    shows none (or one that can't be read unambiguously)."""
    found = footer(lines)
    for rows in (found.below, found.above):
        for index in range(len(rows) - 1, -1, -1):
            match = _CREDITS_RE.search(rows[index])
            if not match and index and _UNIT_RE.search(rows[index]):
                match = _SESSION_FIGURE_RE.search(rows[index - 1])
            if match:
                return _amount(match.group(1))
    return None
