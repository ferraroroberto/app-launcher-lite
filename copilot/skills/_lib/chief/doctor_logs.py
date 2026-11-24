"""The log part of `chief_ops.py doctor`: recent errors, grouped by message.

Two sources are read, never written:

* Lite's own logs, every ``*.log`` in the checkout's ``webapp/`` folder (the
  webapp's warnings, the session-host, the doorbell, the chief events). An
  ``ERROR``/``CRITICAL`` line or a traceback in the window is a failure.
* Copilot CLI's process logs under the Copilot home's ``logs/``. A hook line
  at ``ERROR`` is a failure; Copilot's other errors and its hook warnings are
  reported as notes, because they come from Copilot itself, not from Lite's
  install.

Messages are grouped once ids, hex runs and numbers are masked, so one fault
repeated a hundred times reads as one group with its count.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

WINDOW = timedelta(hours=24)
# A busy warnings log grows a few MB a day; this tail covers far more than the window.
MAX_TAIL_BYTES = 2 * 1024 * 1024
# Logs the pre-ship gate writes for its disposable instances, not this machine's Lite.
SKIPPED_PREFIXES = ("e2e-", "verify-progress")
MESSAGE_CHARS = 160

FAIL = "fail"
NOTE = "note"

_LITE_TS = re.compile(r"^\[?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
_LITE_ERROR = re.compile(r"\[(?:ERROR|CRITICAL)\]|^\S+ \S+ (?:ERROR|CRITICAL) ")
_TRACEBACK = "Traceback (most recent call last)"
_COPILOT_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z \[(\w+)\] (.*)$")
# A date-time or a bare time-of-day (the session-host's format), then an optional level word.
_LEADING_STAMP = re.compile(r"^\[?(?:\d{4}-\d{2}-\d{2}[ T])?\d{2}:\d{2}:\d{2}[\d,.]*\]?\s*(?:\[\w+\]\s*|[A-Z]+\s+)?")
_VOLATILE = (
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<id>"),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<id>"),
    (re.compile(r"\d+(?:[.,]\d+)?"), "N"),
)


@dataclass
class Group:
    """One repeated message: where it came from, how bad, how often, and when last."""

    source: str
    kind: str
    severity: str
    message: str
    count: int
    last: datetime


def normalise(message: str) -> str:
    text = _LEADING_STAMP.sub("", message.strip())
    for pattern, mask in _VOLATILE:
        text = pattern.sub(mask, text)
    return text[:MESSAGE_CHARS]


def _tail_lines(path: Path) -> List[str]:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - MAX_TAIL_BYTES))
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > MAX_TAIL_BYTES else lines


def _log_files(folder: Path) -> List[Path]:
    """``*.log`` files directly in ``folder``; [] when it does not exist, OSError when unreadable."""
    try:
        entries = list(os.scandir(folder))
    except FileNotFoundError:
        return []
    return sorted(Path(e.path) for e in entries if e.name.endswith(".log") and e.is_file())


class _Groups:
    def __init__(self) -> None:
        self.rows: Dict[Tuple[str, str, str], Group] = {}

    def add(self, source: str, kind: str, severity: str, message: str, moment: datetime) -> None:
        key = (source, kind, normalise(message))
        row = self.rows.get(key)
        if row is None:
            self.rows[key] = Group(source, kind, severity, key[2], 1, moment)
            return
        row.count += 1
        row.last = max(row.last, moment)


def _lite_events(lines: Iterable[str], fallback: datetime) -> Iterable[Tuple[str, str, datetime]]:
    """``(kind, message, local time)`` per error line or traceback; an untimed line takes the line above's time."""
    moment: Optional[datetime] = None
    in_traceback = False
    for line in lines:
        stamp = _LITE_TS.match(line)
        if stamp:
            try:
                moment = datetime.strptime(f"{stamp.group(1)} {stamp.group(2)}", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        if line.startswith(_TRACEBACK):
            in_traceback = True
            continue
        if in_traceback:
            # The traceback's own exception line is the first unindented line after the frames.
            if line and not line[0].isspace() and not line.startswith("During handling"):
                in_traceback = False
                yield "traceback", line, moment or fallback
            continue
        if _LITE_ERROR.search(line):
            yield "error", line, moment or fallback


def scan_lite_logs(log_dir: Path, now: datetime) -> List[Group]:
    """Errors and tracebacks in Lite's ``webapp/*.log`` within the window (``now`` is local time)."""
    groups = _Groups()
    for path in _log_files(log_dir):
        if path.name.startswith(SKIPPED_PREFIXES):
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        if now - modified > WINDOW:
            continue
        for kind, message, moment in _lite_events(_tail_lines(path), modified):
            if now - moment <= WINDOW:
                groups.add(f"launcher:{path.name}", kind, FAIL, message, moment)
    return list(groups.rows.values())


def scan_copilot_logs(log_dir: Path, now_utc: datetime) -> List[Group]:
    """Hook failures (fail), and Copilot's other errors and hook warnings (note), within the window."""
    groups = _Groups()
    for path in _log_files(log_dir):
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if now_utc - modified > WINDOW:
            continue
        for line in _tail_lines(path):
            match = _COPILOT_LINE.match(line)
            if not match:
                continue
            moment = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            if now_utc - moment > WINDOW:
                continue
            level, message = match.group(2), match.group(3)
            hook = "hook" in message.lower()
            if level == "ERROR":
                groups.add("copilot", "hook-failure" if hook else "error", FAIL if hook else NOTE, message, moment)
            elif level == "WARNING" and hook:
                groups.add("copilot", "hook-warning", NOTE, message, moment)
    return list(groups.rows.values())
