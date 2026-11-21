"""The owner-check comment block: one per state change, the newest one wins.

Pure formatting and parsing. The format is defined in `docs/owner-check.md`; a
block is a marker line, a bold title and `- key: value` lines, so it reads as
plain prose on GitHub and parses back without a markdown library.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence

MARKER = "<!-- owner-check v1 -->"

VALIDATE_AFTER = "validate-after"
BEFORE_START = "before-start"
CLEARED = "cleared"
KINDS = (VALIDATE_AFTER, BEFORE_START, CLEARED)

# The label that keeps an item on the Board, per open kind.
LABELS: Dict[str, str] = {VALIDATE_AFTER: "needs-validation", BEFORE_START: "needs-owner"}
LABEL_STYLE: Dict[str, tuple] = {
    "needs-validation": ("fbca04", "Shipped; the owner must try it as a user"),
    "needs-owner": ("d93f0b", "Cannot start until the owner does a named step"),
}
_TITLES = {
    VALIDATE_AFTER: "Owner check: validate after",
    BEFORE_START: "Owner check: before start",
    CLEARED: "Owner check: cleared",
}
FIELDS = ("kind", "reason", "try", "proof", "related", "since", "answer")
_LINE = re.compile(r"^- (" + "|".join(FIELDS) + r"): ?(.*)$")


# A before-start step must be answerable cold: what to do, where, which answer unblocks.
_MIN_ACTION_WORDS = 6
_PLACEHOLDER = re.compile(r"^\W*(tbd|todo|n/?a|none|see (the )?(issue|above|body)|decide|check|investigate|"
                          r"look into it|ask roberto|owner decides?)\W*$", re.IGNORECASE)


def vague_action(text: str) -> Optional[str]:
    """Why an owner action is too vague to answer cold, or None when it is concrete."""
    text = _one_line(text)
    if not text or _PLACEHOLDER.match(text):
        return "is empty or a placeholder"
    if len(text.split()) < _MIN_ACTION_WORDS:
        return f"names no concrete action (under {_MIN_ACTION_WORDS} words)"
    return None


def _one_line(value: str) -> str:
    return " ".join(str(value or "").split())


def format_block(
    kind: str,
    reason: str,
    try_text: str,
    proof: Sequence[str] = (),
    since: str = "",
    answer: str = "",
    related: Sequence[str] = (),
) -> str:
    """The comment body for one state change. Empty optional fields are left out."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    values = {
        "kind": kind,
        "reason": _one_line(reason),
        "try": _one_line(try_text),
        "proof": ", ".join(_one_line(p) for p in proof if _one_line(p)),
        "related": ", ".join(_one_line(r) for r in related if _one_line(r)),
        "since": _one_line(since),
        "answer": _one_line(answer),
    }
    lines = [MARKER, f"**{_TITLES[kind]}**", ""]
    lines += [f"- {key}: {values[key]}" for key in FIELDS if values[key]]
    return "\n".join(lines) + "\n"


def parse_block(body: str) -> Optional[Dict[str, object]]:
    """The block's fields, or None when `body` carries no valid v1 block.

    `proof` and `related` come back as lists; every other field as a string.
    """
    if MARKER not in (body or ""):
        return None
    fields: Dict[str, object] = {}
    for raw in body.split(MARKER, 1)[1].splitlines():
        m = _LINE.match(raw.strip())
        if m:
            fields[m.group(1)] = m.group(2).strip()
    if fields.get("kind") not in KINDS:
        return None
    for key in ("proof", "related"):
        value = str(fields.get(key, ""))
        fields[key] = [p.strip() for p in value.split(",") if p.strip()]
    return fields


def latest_block(comment_bodies: Iterable[str]) -> Optional[Dict[str, object]]:
    """The newest valid block among comments given oldest first."""
    latest = None
    for body in comment_bodies:
        parsed = parse_block(body)
        if parsed is not None:
            latest = parsed
    return latest


def open_kinds(label_names: Iterable[str]) -> List[str]:
    """The open owner-check kinds an issue's labels put it in, in KINDS order."""
    names = set(label_names)
    return [kind for kind, label in LABELS.items() if label in names]
