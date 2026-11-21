"""A Copilot session's context-window use, read from its own event log.

app-launcher-lite's `/context` endpoint reads a statusline Copilot does not
print, so the lite chief reads the number where Copilot records it: the newest
`session.usage_record` in the session's `events.jsonl` carries `inputTokens`
(the prompt the model last saw) and `maxPromptTokens` (the window). The
launcher session id is joined to Copilot's own session through the
`sessions-state.json` row `hooks/session_state.py` writes
(`launcher_session_id`).

Returns the launcher endpoint's shape, `{"available": bool, "percent": int}` or
`{"available": False, "reason": "..."}`, so the ported `compact` formatting reads
it unchanged. Every step that cannot be read stays `unavailable` with its
reason, never a percentage of 0.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fleet_core.hooks_state import state_dir

SESSIONS_STATE_FILENAME = "sessions-state.json"
# Copilot keeps one folder per session here (copilot help config, "Files and directories").
COPILOT_SESSION_STATE = Path.home() / ".copilot" / "session-state"
# A long session's log is large; the newest usage record sits near its end.
TAIL_BYTES = 512 * 1024


def _unavailable(reason: str) -> Dict[str, Any]:
    return {"available": False, "reason": reason}


def session_row(launcher_sid: str, path: Optional[Path] = None) -> Tuple[Optional[str], Dict[str, Any], str]:
    """`(copilot session id, row, "")` for the newest row naming `launcher_sid`, else `(None, {}, why)`."""
    path = path or state_dir() / SESSIONS_STATE_FILENAME
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, {}, "no sessions-state file (the Copilot hooks have not run)"
    except (OSError, ValueError) as exc:
        return None, {}, f"sessions-state unreadable ({type(exc).__name__})"
    if not isinstance(rows, dict):
        return None, {}, "sessions-state is not an object"
    matches = [(str(row.get("updated_at") or ""), key, row) for key, row in rows.items()
               if isinstance(row, dict) and row.get("launcher_session_id") == launcher_sid]
    if not matches:
        return None, {}, "no Copilot session row for this launcher session"
    _stamp, key, row = max(matches, key=lambda m: m[0])
    return key, row, ""


def latest_usage(events: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """The newest `session.usage_record` usage block in the tail of `events`, or `(None, why)`."""
    try:
        with open(events, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return None, f"events.jsonl unreadable ({type(exc).__name__})"
    for line in reversed(tail.splitlines()):
        if '"session.usage_record"' not in line:
            continue
        try:
            usage = json.loads(line).get("data", {}).get("usage")
        except (ValueError, AttributeError):
            continue
        if isinstance(usage, dict):
            return usage, ""
    return None, "no session.usage_record in the event log yet"


def percent_of(usage: Dict[str, Any]) -> Tuple[Optional[int], str]:
    used, window = usage.get("inputTokens"), usage.get("maxPromptTokens")
    for value in (used, window):
        if isinstance(value, bool) or not isinstance(value, int):
            return None, "usage_record lacks inputTokens or maxPromptTokens"
    if window <= 0:
        return None, "usage_record maxPromptTokens is not positive"
    return round(100 * used / window), ""


def context_info(launcher_sid: str, state_file: Optional[Path] = None,
                 session_state: Path = COPILOT_SESSION_STATE) -> Dict[str, Any]:
    """The launcher-shaped context reading for `launcher_sid`."""
    copilot_sid, row, reason = session_row(launcher_sid, state_file)
    if copilot_sid is None:
        return _unavailable(reason)
    transcript = row.get("transcript_path")
    events = Path(transcript) if isinstance(transcript, str) and transcript else session_state / copilot_sid / "events.jsonl"
    usage, reason = latest_usage(events)
    if usage is None:
        return _unavailable(reason)
    percent, reason = percent_of(usage)
    if percent is None:
        return _unavailable(reason)
    return {"available": True, "percent": percent, "source": "usage_record",
            "input_tokens": usage["inputTokens"], "max_prompt_tokens": usage["maxPromptTokens"]}
