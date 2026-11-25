"""What a live Copilot CLI session runs and shows, for the Chat view's model
chip, context ring and Switch model sheet.

Probed live on Copilot CLI 1.0.114:

* The model is in the session's own ``events.jsonl``: ``session.start`` (and
  ``session.resume``) carry ``selectedModel``, and an in-session ``/model <id>``
  writes ``session.model_change`` with ``newModel`` the moment it applies. The
  newest of them is the model the session runs now. ``auto`` is Copilot Fusion
  picking per task, and is shown as such. ``session.start`` keeps an id it was
  launched with even when that id does not exist, so the newest record wins
  rather than the first. Copilot writes the file at the session's first
  prompt, with every record since launch, so a session nobody has prompted
  has no log: its model is its launch flag, and a switch there could not be
  confirmed, so it is refused.
* The status line under the prompt names the model, the effort and the
  context-window use, as Copilot computes it::

      ← open sidebar · Interactive · Manual Approval · / commands  · space hold to record GPT-6 Luna · Medium · (10%)

  ``(10%)`` matches ``/context``'s ``28k/272k tokens (10%)``. A narrow PTY
  cuts that line short (``MAI Code 2 Flash · M…`` at 46 columns), and then
  there is no figure to show: nothing else Copilot writes states it, so the
  ring stays hidden rather than showing a computed guess.
* While a turn runs the status line starts with ``Working``, and a ``/model``
  typed then is queued (``Queued (1)``) until the turn ends, so the switch is
  refused until Copilot is back at an empty prompt.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

from src.session_view import transcript_locate

logger = logging.getLogger(__name__)

# Event type -> the field naming the model it puts the session on.
_MODEL_EVENTS = {
    "session.start": "selectedModel",
    "session.resume": "selectedModel",
    "session.model_change": "newModel",
}
_PREFILTER = tuple(f'"{name}"' for name in _MODEL_EVENTS)

# The status line's closing figure: Copilot's own context-window percentage.
_CONTEXT_RE = re.compile(r"·\s*\((\d{1,3})%\)\s*$")
_WORKING_RE = re.compile(r"^\s*\S?\s*Working\b")
# A footer wrapped over two rows leaves the percent alone on the bottom row,
# without the ``·`` that led it. Trusted only under the input box.
_WRAPPED_CONTEXT_RE = re.compile(r"(?:^|\s)\((\d{1,3})%\)\s*$")
# Non-blank rows the footer may take under the input box's bottom border.
_FOOTER_ROWS_MAX = 2
_RULE_CHARS = set("─━")

# Event logs read so far, by path: (bytes consumed, model). A poll reads only
# what was appended since, so a long session's log is read once in full.
_MODELS_MAX = 128
_models: "OrderedDict[str, Tuple[int, Optional[str]]]" = OrderedDict()
_models_lock = threading.Lock()

# Event-log lookups by launcher session id. A found log never moves; a miss is
# retried after the TTL (Copilot creates the log at the first prompt), and the
# lookup reads every session folder's sidecar, so it is not run on every poll.
_MISS_TTL_S = 10.0
_LOGS_MAX = 128
_logs: "OrderedDict[str, Tuple[Optional[Path], float]]" = OrderedDict()
_logs_lock = threading.Lock()


def event_log(session: Mapping[str, Any], fresh: bool = False) -> Optional[Path]:
    """The live Copilot session's ``events.jsonl``, or ``None`` when no single
    log matches it yet
    (:func:`~src.session_view.transcript_locate.find_copilot_transcript`).
    ``fresh`` looks again past a cached miss. Blocking file IO."""
    sid = str(session.get("session_id") or "")
    now = time.monotonic()
    with _logs_lock:
        hit = _logs.get(sid)
    if sid and hit is not None and (
        hit[0] is not None or (not fresh and now - hit[1] < _MISS_TTL_S)
    ):
        return hit[0]
    path = transcript_locate.find_copilot_transcript(dict(session))
    if sid:
        with _logs_lock:
            _logs[sid] = (path, now)
            _logs.move_to_end(sid)
            while len(_logs) > _LOGS_MAX:
                _logs.popitem(last=False)
    return path


def _model_in(line: bytes) -> Optional[str]:
    text = line.decode("utf-8", errors="replace")
    if not any(marker in text for marker in _PREFILTER):
        return None
    try:
        event = json.loads(text)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    field = _MODEL_EVENTS.get(str(event.get("type")))
    data = event.get("data")
    if not field or not isinstance(data, dict):
        return None
    model = data.get(field)
    return model.strip() if isinstance(model, str) and model.strip() else None


def current_model(path: Any) -> Optional[str]:
    """The model id the event log says the session runs now, or ``None``
    when no record names one or the file can't be read. Blocking file IO;
    only bytes appended since the last call are read."""
    if not path:
        return None
    key = str(path)
    with _models_lock:
        offset, model = _models.get(key, (0, None))
    try:
        with open(key, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size < offset:
                # A shorter file is a rewritten one: read it again from the top.
                offset, model = 0, None
            fh.seek(offset)
            chunk = fh.read(size - offset)
    except OSError as exc:
        logger.info("ℹ️ copilot event log %s unreadable: %s", Path(key).parent.name[:8], exc)
        return None
    # A torn last line is left for the next read to finish.
    complete = chunk[: chunk.rfind(b"\n") + 1]
    for line in complete.splitlines():
        found = _model_in(line)
        if found:
            model = found
    with _models_lock:
        _models[key] = (offset + len(complete), model)
        _models.move_to_end(key)
        while len(_models) > _MODELS_MAX:
            _models.popitem(last=False)
    return model


def _status_line(lines: List[str]) -> Optional[str]:
    """The bottom-most non-blank row: Copilot's status line."""
    return next((line for line in reversed(lines or []) if line.strip()), None)


def _under_input_box(lines: List[str]) -> bool:
    """Whether the bottom-most row sits within the footer's rows below the
    input box's bottom border."""
    below = 0
    for line in reversed(lines or []):
        text = line.strip()
        if text and set(text) <= _RULE_CHARS:
            return True
        below += bool(text)
        if below > _FOOTER_ROWS_MAX:
            return False
    return False


def context_percent(lines: List[str]) -> Optional[int]:
    """The context-window percentage Copilot's status line shows, or ``None``
    when the line is not on screen or is cut short before the figure."""
    status = _status_line(lines)
    match = _CONTEXT_RE.search(status or "")
    if not match and _under_input_box(lines):
        match = _WRAPPED_CONTEXT_RE.search(status or "")
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 100 else None


def switch_refusal(lines: List[str]) -> Optional[str]:
    """Why ``/model`` can't be typed into this screen now, or ``None`` when
    Copilot sits at an empty prompt. The prompt is the ``❯`` row between the
    two rules above the status line; text on it would be sent with the
    command, and a running turn would queue it."""
    status = _status_line(lines)
    if status is None:
        return "Chat can't see the terminal's screen, so nothing was sent"
    if _WORKING_RE.match(status):
        return "Copilot is working: switch the model once it is back at the prompt"
    rows = [line for line in lines if line.strip()]
    for index in range(len(rows) - 2, 0, -1):
        above, row, below = rows[index - 1].strip(), rows[index], rows[index + 1].strip()
        if row.lstrip().startswith("❯") and set(above) <= _RULE_CHARS and set(below) <= _RULE_CHARS:
            if row.strip()[1:].strip():
                # Also the search row of an open menu, drawn the same way.
                return "Copilot's prompt holds text or a menu is open: clear it on the terminal first"
            return None
    return "Copilot isn't at its prompt: answer what the terminal shows first"
