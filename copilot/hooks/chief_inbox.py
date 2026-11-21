"""Chief inbox: a chief-managed worker's turn end or exit becomes a mailbox event.

The lite counterpart of fleet-config's ``hooks/chief_inbox.py``. A worker the
chief dispatched (its launcher session id is a key in ``chief-managed.json``)
gets one event file per ``agentStop`` (``kind: turn-end``) and ``sessionEnd``
(``kind: session-end``) in ``<state dir>/chief-inbox/``. The chief claims them
with ``chief_ops.py inbox`` and retires them with ``ack``. The file format is
fleet-config's ``docs/chief-mailbox.md`` v2, unchanged, so the chief's reader
and app-launcher-lite's pending count parse it as they would upstream.

Copilot's ``agentStop`` payload carries no reply text, only ``transcriptPath``
(the session's ``events.jsonl``), so the excerpt is the newest
``assistant.message`` content in a bounded tail of that file
(``excerpt_source: transcript``).

Called from ``session_state.main`` on the same hook invocation, so installing it
needs no new hook registration. Cheap and fail-open: a session without
``APP_LAUNCHER_SESSION_ID`` returns before any I/O; an unreadable registry is
logged as undetermined and writes nothing, never folded into "not managed".
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
INBOX_DIRNAME = "chief-inbox"
REGISTRY_FILENAME = "chief-managed.json"
LAUNCHER_SESSION_ENV = "APP_LAUNCHER_SESSION_ID"
# Copilot hook event -> (v1-compatible `event` name, v2 `kind`), per the contract.
EVENTS = {"agentStop": ("Stop", "turn-end"), "sessionEnd": ("SessionEnd", "session-end")}
EXCERPT_CHARS = 1500
# Enough of events.jsonl for the last reply without reading a long session whole.
TRANSCRIPT_TAIL_BYTES = 256 * 1024
# A second turn-end from one session inside this window is not written (contract rule).
TURN_END_DEBOUNCE_SECONDS = 45

NOT_LAUNCHER = "not-launcher"
NOT_DISPATCHED = "not-dispatched"
UNDETERMINED = "undetermined"
MANAGED = "managed"


def state_dir() -> Path:
    root = os.environ.get("COPILOT_HOOKS_STATE_DIR")
    return Path(root) if root else Path.home() / ".copilot" / "hooks" / "state"


def managed_row(sid: str, path: Optional[Path] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """This launcher session's registry row and why; ``row`` is set only when managed."""
    if not sid:
        return None, NOT_LAUNCHER
    path = path or state_dir() / REGISTRY_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None, NOT_DISPATCHED
    except (OSError, ValueError):
        return None, UNDETERMINED
    if not isinstance(data, dict):
        return None, UNDETERMINED
    if sid not in data:
        return None, NOT_DISPATCHED
    row = data[sid]
    return (row if isinstance(row, dict) else {}), MANAGED


def transcript_excerpt(path: str) -> Tuple[Optional[str], str]:
    """``(excerpt, unavailable reason)`` from the newest ``assistant.message`` in the tail."""
    if not path:
        return None, "no transcriptPath in the payload"
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return None, f"transcript unreadable ({type(exc).__name__})"
    for line in reversed(tail.splitlines()):
        if '"assistant.message"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get("data") or {}).get("content") if isinstance(entry, dict) else None
        if isinstance(content, str) and content.strip():
            return content.strip()[-EXCERPT_CHARS:], ""
    return None, "no assistant reply in the transcript tail"


def _debounced(directory: Path, sid: str, now: float) -> bool:
    """True when this sid wrote a turn-end inside the window; otherwise stamps now."""
    stamp = directory / f".turn-end-{sid}.stamp"
    try:
        if now - stamp.stat().st_mtime < TURN_END_DEBOUNCE_SECONDS:
            return True
    except OSError:
        pass
    try:
        # runtime-write-exempt: the inbox under the user state dir (state_dir()), never this repo
        stamp.write_text("", encoding="utf-8")
    except OSError:
        pass
    return False


def _publish(directory: Path, entry: Dict[str, Any], now: datetime) -> Path:
    """Temp file named ``.event.<random>.tmp`` in the inbox, then ``os.replace``."""
    target = directory / f"{int(now.timestamp() * 1000)}-{entry['id']}.json"
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".event.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False))
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def record(payload: Dict[str, Any], hook_event: str) -> Optional[Path]:
    """Write one inbox event for a chief-managed session's ``hook_event``; the file or None."""
    if hook_event not in EVENTS:
        return None
    sid = os.environ.get(LAUNCHER_SESSION_ENV, "").strip()
    if not sid:
        return None
    row, reason = managed_row(sid)
    if reason == UNDETERMINED:
        logger.info("ℹ️ chief_inbox: chief-managed state undetermined for %s -- no %s event written",
                    sid, hook_event)
        return None
    if row is None:
        return None
    event_name, kind = EVENTS[hook_event]
    now = datetime.now(timezone.utc)
    entry: Dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "id": uuid.uuid4().hex,
        "kind": kind,
        "event": event_name,
        "launcher_sid": sid,
        "repo": row.get("repo"),
        "number": row.get("number"),
        "agent": os.environ.get("APP_LAUNCHER_AGENT", "").strip().lower() or "copilot",
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dispatch_id": row.get("dispatch_id"),
    }
    if kind == "turn-end":
        excerpt, unavailable = transcript_excerpt(str(payload.get("transcript_path") or ""))
    else:
        excerpt, unavailable = None, "session ended; no reply on this event"
    entry["excerpt"] = excerpt
    entry["excerpt_source"] = "transcript" if excerpt else None
    if excerpt is None:
        entry["excerpt_unavailable"] = unavailable
    directory = state_dir() / INBOX_DIRNAME
    # runtime-write-exempt: the inbox under the user state dir (state_dir()), never this repo
    directory.mkdir(parents=True, exist_ok=True)
    if kind == "turn-end" and _debounced(directory, sid, time.time()):
        logger.info("ℹ️ chief_inbox: turn-end for %s within %ss of the last -- skipped",
                    sid, TURN_END_DEBOUNCE_SECONDS)
        return None
    return _publish(directory, entry, now)


def record_safely(payload: Dict[str, Any], hook_event: str) -> None:
    """``record`` for the hook path: never raises, never blocks the session."""
    try:
        record(payload, hook_event)
    except Exception as exc:  # noqa: BLE001 -- advisory; a worker's turn end must never fail
        logger.info("ℹ️ chief_inbox: %s event not written (%s)", hook_event, exc)
