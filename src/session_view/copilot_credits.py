"""The AI credits a Copilot session has spent, read off its terminal's footer.

Copilot CLI paints the running total on a status row of its screen, for
example ``Session: 5.62 AIC used``. That number is the only source: nothing
Copilot writes to disk carries it, and this repo does no accounting of its own,
so a session that restarts starts again from whatever its screen says.

Two layouts are read. Older Copilots paint the figure on the bottom-most
non-blank row. Current ones paint it on the status row above the input box and
put the model, effort and context percent on the bottom row::

    Plan: 26.957/35.100 (76% used) · Session: 15.99 AIC used · YOLO
    ──────────────────────────────────────────
    > (input box)
    ──────────────────────────────────────────
    <model name> · Extra High · (9%)

The bottom row is tried first. Failing that, only the status row directly
above the input box counts, found through the box's two border rules: nothing
higher up is read, so a line quoted in the conversation is never credits. A
row cut short by a narrow terminal, a Copilot that paints no credits and the
``Plan: 459/1500 (30% used)`` allowance row all read ``None``: unknown, never a
zero.

Pure: the caller renders the screen (:mod:`src.session_view.vt_screen`).
"""

from __future__ import annotations

import re
from typing import List, Optional

# ``Session: 5.62 AIC used``; the unit is required: a figure in any other unit
# is not credits. The number's separators are decided by ``_amount``.
_CREDITS_RE = re.compile(r"\bSession:\s*([\d.,]*\d)\s*AIC\b")
# ``Session: 39.74`` ending its row or followed by a cell separator: the unit
# is on the next row. Any other word after the number is some other unit.
_SESSION_FIGURE_RE = re.compile(r"\bSession:\s*([\d.,]*\d)\s*(?:[·|•].*)?$")
_UNIT_RE = re.compile(r"\bAIC\b")
_RULE_CHARS = set("─━")

# Rows searched between the input box's borders, and above its top border, for
# the status row: an input box grows with a long prompt, the status row sits
# at most a blank row or two off the border.
_INPUT_ROWS_MAX = 10
_STATUS_GAP_MAX = 3
# Non-blank rows allowed below the box's bottom border (the model row).
_BELOW_BOX_MAX = 2

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


def _is_rule(row: str) -> bool:
    text = row.strip()
    return bool(text) and set(text) <= _RULE_CHARS


def _row_credits(row: Optional[str]) -> Optional[float]:
    match = _CREDITS_RE.search(row or "")
    return _amount(match.group(1)) if match else None


def _status_rows(rows: List[str], bottom: int) -> List[str]:
    """The non-blank rows just above the input box that ends at ``rows[bottom]``'s
    footer, top to bottom and at most two, or ``[]`` when the screen shows no
    such box."""
    index, below = bottom, 0
    while index >= 0 and not _is_rule(rows[index]):
        below += rows[index].strip() != ""
        if below > _BELOW_BOX_MAX:
            return []
        index -= 1
    top = index - 1
    while top >= 0 and index - top <= _INPUT_ROWS_MAX and not _is_rule(rows[top]):
        top -= 1
    if top < 0 or not _is_rule(rows[top]):
        return []
    above = [row for row in rows[max(0, top - _STATUS_GAP_MAX):top] if row.strip()]
    return above[-2:]


def _wrapped_credits(upper: str, lower: str) -> Optional[float]:
    """The figure when ``Session: N`` ends ``upper`` and the unit starts ``lower``."""
    match = _SESSION_FIGURE_RE.search(upper)
    if match and _UNIT_RE.search(lower):
        return _amount(match.group(1))
    return None


def _status_credits(rows: List[str], bottom: int) -> Optional[float]:
    status = _status_rows(rows, bottom)
    if not status:
        return None
    found = _row_credits(status[-1])
    if found is None and len(status) == 2:
        found = _wrapped_credits(status[0], status[1])
    return found


def ai_credits_used(lines: List[str]) -> Optional[float]:
    """The credits the footer says this session has used, or ``None`` when it
    shows none (or one that can't be read unambiguously)."""
    rows = list(lines or [])
    bottom = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].strip()), None)
    if bottom is None:
        return None
    if _CREDITS_RE.search(rows[bottom]):
        return _row_credits(rows[bottom])
    return _status_credits(rows, bottom)
