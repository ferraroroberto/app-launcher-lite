"""The chief's journal: what changed, one line each, for the Board's "since you last looked" card.

The plan file holds the current state; this file holds what happened. One JSON
object per line in `chief-journal.jsonl` beside the plan (format and bounds:
`docs/chief-journal.md`). `chief_ops.py note` appends an entry by hand, and
every `plan` transition logs itself through `log_transition`.

An entry is skipped when it repeats the newest entry for the same `kind` +
`ref`, so a chief that restates itself every turn adds nothing. Every append is
a locked read-modify-write that trims the file to its bounds and replaces it
atomically.

stdlib only.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from active_issue import _iso_z, _now, state_lock  # noqa: E402
from fleet_core import runtime_files  # noqa: E402
from fleet_core.hooks_state import atomic_write_text, state_dir  # noqa: E402

LOGGER = logging.getLogger("chief_journal")

JOURNAL_FILENAME = "chief-journal.jsonl"
KINDS = ("merged", "dispatched", "filed", "parked", "asked", "answered", "failed", "note")
# One short line, the same cap the plan gives a queue title.
MAX_TEXT = 200
# The file holds whichever bound is tighter: this many entries or this many days.
MAX_ENTRIES = 500
MAX_AGE_DAYS = 14

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_QUALIFIED_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*#[1-9][0-9]*")

APPENDED = "appended"
SKIPPED = "skipped"


def journal_file() -> Path:
    """`~/.claude/hooks/state/chief-journal.jsonl`, honouring `CLAUDE_HOOKS_STATE_DIR`."""
    return state_dir() / JOURNAL_FILENAME


def journal_for_plan(plan_path: Path) -> Path:
    """The journal that sits beside a plan file (the live one, or a test's throwaway)."""
    return plan_path.with_name(JOURNAL_FILENAME)


def make_entry(kind: str, text: str, ref: Optional[str] = None,
               now: Optional[datetime] = None) -> Dict[str, Any]:
    """One validated entry; ValueError names what is wrong with the input."""
    if kind not in KINDS:
        raise ValueError(f"--kind must be one of {', '.join(KINDS)}, got {kind!r}")
    text = " ".join(text.split())
    if not text:
        raise ValueError("the journal text must not be empty")
    if _CONTROL.search(text):
        raise ValueError("the journal text must be one line with no control characters")
    if len(text) > MAX_TEXT:
        raise ValueError(f"the journal text is longer than {MAX_TEXT} characters")
    if ref is not None and not _QUALIFIED_REF.fullmatch(ref):
        raise ValueError(f"--ref must be <repo>#<number>, got {ref!r}")
    return {"ts": _iso_z(now or _now()), "kind": kind, "ref": ref or "", "text": text}


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_entries(path: Path) -> List[Dict[str, Any]]:
    """Every readable entry, oldest first; a missing file is empty and a damaged line is dropped."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    entries: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(row, dict) and row.get("kind") in KINDS
                and isinstance(row.get("text"), str) and _parse_ts(row.get("ts"))):
            entries.append(row)
    return entries


def trim(entries: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    """Drop entries older than MAX_AGE_DAYS, then keep the newest MAX_ENTRIES."""
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    fresh = [row for row in entries if (_parse_ts(row["ts"]) or now) >= cutoff]
    return fresh[-MAX_ENTRIES:]


def _is_repeat(entries: List[Dict[str, Any]], entry: Dict[str, Any]) -> bool:
    for row in reversed(entries):
        if row["kind"] == entry["kind"] and row.get("ref", "") == entry["ref"]:
            return row["text"] == entry["text"]
    return False


def append(path: Path, kind: str, text: str, ref: Optional[str] = None,
           now: Optional[datetime] = None) -> str:
    """Append one entry unless it repeats the newest one for its kind + ref.

    Returns `APPENDED` or `SKIPPED`. Raises ValueError for a bad input and
    OSError when the file cannot be written.
    """
    moment = now or _now()
    entry = make_entry(kind, text, ref, moment)
    with state_lock(path):
        entries = read_entries(path)
        if _is_repeat(entries, entry):
            return SKIPPED
        entries = trim(entries + [entry], moment)
        runtime_files.make_dirs(path.parent, parents=True, exist_ok=True)
        payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in entries)
        atomic_write_text(path, payload, newline="\n")
    return APPENDED


def fit(text: str) -> str:
    """One line of at most MAX_TEXT characters, for text built from a plan title or question."""
    line = " ".join(_CONTROL.sub(" ", text).split())
    return line if len(line) <= MAX_TEXT else line[:MAX_TEXT - 1] + "…"


def log_transition(path: Path, kind: str, text: str, ref: Optional[str] = None) -> Optional[str]:
    """`append` for a plan transition: a journal failure is logged and never raised.

    The plan was already written when this runs, and the chief's next move
    depends on that write's exit code, so a broken journal must not turn a
    good `plan` command into a failed one.
    """
    try:
        return append(path, kind, fit(text), ref)
    except Exception as exc:  # noqa: BLE001 -- any failure here must leave the plan command's result alone
        LOGGER.warning("⚠️ journal write skipped (%s %s): %s", kind, ref or "-", exc)
        return None


def cmd_note(args: argparse.Namespace) -> int:
    """`chief_ops.py note`: append one entry, or say why nothing was written."""
    path = Path(args.journal_file) if args.journal_file else journal_file()
    try:
        outcome = append(path, args.kind, args.text, args.ref)
    except OSError as exc:
        LOGGER.warning("❌ journal write failed: %s", exc)
        print(f"NOTE=failed reason={type(exc).__name__}")
        return 1
    if outcome == SKIPPED:
        print("NOTE=skipped reason=duplicate")
    else:
        print(f"NOTE=appended kind={args.kind} ref={args.ref or '-'}")
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    """Mount `note` on the `chief_ops.py` parser."""
    note = sub.add_parser("note", help="journal one line for the Board's 'since you last looked' card")
    note.add_argument("text", help="one short line, public-safe: no secrets, no session links")
    note.add_argument("--kind", default="note", help=f"{' | '.join(KINDS)} (default note)")
    note.add_argument("--ref", default=None, help="<repo>#<N> the line is about")
    note.add_argument("--journal-file", default=None,
                      help="override the journal path (tests); default hooks/state/chief-journal.jsonl")
    note.set_defaults(func=cmd_note)
