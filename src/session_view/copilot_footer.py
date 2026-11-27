"""Which rows of a Copilot CLI screen are its footer.

Copilot moves its figures between rows from one release to the next, so the
readers (:mod:`src.session_view.copilot_credits`,
:mod:`src.session_view.copilot_session`) do not ask for a figure on one given
row: they look for its label or number format in every row here. This module
only decides what is Copilot's chrome and what is not::

    ● Done.                                   conversation: never read
                                              (a blank row ends the block)
    Plan: 26.957/35.100 · Session: 1.5 AIC    ``above``: the status block
    ──────────────────────────────────────
    > (input box)                             the user's text: never read
    ──────────────────────────────────────
    <model name> · Extra High · (9%)          ``below``

A screen with no input box (a narrow cut, a dialog) offers its bottom-most row
only, so nothing in the conversation can pass for a figure.

Pure: the caller renders the screen (:mod:`src.session_view.vt_screen`).
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional

_RULE_CHARS = set("─━")
# The scrollbar column Copilot paints at the screen's right edge.
_SCROLLBAR_CHARS = "┃│║▐█"

# Non-blank rows allowed below the input box's bottom border.
_BELOW_BOX_MAX = 3
# Rows searched for the box's top border: the box grows with a long prompt.
_INPUT_ROWS_MAX = 10
# The status block sits at most this many blank rows off the top border, and is
# the unbroken run of rows from there up, no taller than this.
_STATUS_GAP_MAX = 2
_STATUS_ROWS_MAX = 3


class Footer(NamedTuple):
    """The footer's rows, each list top to bottom. ``boxed`` says an input box
    border was found, so ``below`` is Copilot's own and not just the last row."""

    above: List[str]
    below: List[str]
    boxed: bool


def _clean(row: str) -> str:
    return row.rstrip().rstrip(_SCROLLBAR_CHARS).rstrip()


def is_rule(row: str) -> bool:
    """Whether the row is one of the input box's horizontal borders."""
    text = row.strip()
    return bool(text) and set(text) <= _RULE_CHARS


def _bottom_border(rows: List[str]) -> Optional[int]:
    """The index of the input box's bottom border, within the footer's rows
    from the end of the screen."""
    below = 0
    for index in range(len(rows) - 1, -1, -1):
        if is_rule(rows[index]):
            return index
        below += bool(rows[index])
        if below > _BELOW_BOX_MAX:
            break
    return None


def _status_block(rows: List[str], bottom: int) -> List[str]:
    """The rows just above the input box whose bottom border is ``rows[bottom]``,
    or ``[]`` when its top border is not on screen."""
    top = bottom - 1
    while top >= 0 and bottom - top <= _INPUT_ROWS_MAX and not is_rule(rows[top]):
        top -= 1
    if top < 0 or not is_rule(rows[top]):
        return []
    block: List[str] = []
    gap = 0
    for row in reversed(rows[:top]):
        if not row:
            gap += 1
            if block or gap > _STATUS_GAP_MAX:
                break
            continue
        if is_rule(row) or len(block) == _STATUS_ROWS_MAX:
            break
        block.insert(0, row)
    return block


def footer(lines: List[str]) -> Footer:
    """The footer of the screen ``lines`` show; both lists empty on a blank one."""
    rows = [_clean(line) for line in lines or []]
    bottom = _bottom_border(rows)
    if bottom is None:
        last = next((row for row in reversed(rows) if row), None)
        return Footer([], [] if last is None else [last], False)
    return Footer(_status_block(rows, bottom), [row for row in rows[bottom + 1:] if row], True)
