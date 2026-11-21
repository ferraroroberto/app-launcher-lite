"""Repair: write the missing owner-check note for a label that carries none.

A `needs-validation` or `needs-owner` label with no readable block of its own kind
renders on the Board as "Details unknown". The label can arrive without a note by
hand, from an older flow, or from a write that died between its two calls; this
builds the note from what the issue and its closing PRs already say, so the owner
sees what to try. Only unticked checklist lines are quoted, from the same issue
and its own PRs, so nothing reaches a comment that the issue did not already hold.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

from owner_check import backfill, block

REPAIR_REASON = "Labelled for an owner check with no note on record"

_UNTICKED = re.compile(r"^\s*[-*] \[ \] +(\S.*?)\s*$")
_MAX_ITEMS = 3
_MAX_ITEM_CHARS = 160


def unticked(text: str) -> List[str]:
    """The unticked `- [ ]` checklist lines of a markdown body, in order, one line each."""
    return [m.group(1) for line in (text or "").splitlines() if (m := _UNTICKED.match(line))]


def _clip(item: str) -> str:
    return item if len(item) <= _MAX_ITEM_CHARS else item[: _MAX_ITEM_CHARS - 1].rstrip() + "…"


def build_try(kind: str, bodies: Iterable[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    """`(try text, source)` for a note, or None when no honest one can be built.

    `bodies` are `(source, text)` pairs, the issue first then each closing PR; the
    first with an unticked item supplies the text. A validate-after note falls back
    to the generic "use it once" line; a before-start step must name a concrete
    owner action, so with nothing usable it has no note rather than an invented one.
    """
    for source, body in bodies:
        items = unticked(body)
        if items:
            text = "; ".join(_clip(i) for i in items[:_MAX_ITEMS])
            if kind == block.BEFORE_START and block.vague_action(text):
                continue
            return text, source
    if kind == block.VALIDATE_AFTER:
        return backfill.BACKFILL_TRY, "generic"
    return None
