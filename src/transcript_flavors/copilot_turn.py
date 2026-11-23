"""What a Copilot CLI ``events.jsonl`` says about the session's last turn end.

Copilot's ``agentStop`` hook only says a turn ended. Whether that end needs a
person is in the event log Copilot writes itself:

* **Background work still running.** A shell started in background mode is a
  ``tool.execution_complete`` whose ``toolTelemetry.properties.executionMode``
  is ``async``, with its ``shellId`` in ``result.structuredContent``. It ends
  with a ``system.notification`` of kind ``shell_completed`` naming the shell,
  or with a ``stop_powershell`` call naming it. A ``detached`` shell is never
  awaited (nothing notifies its end), so it is not counted. A sub-agent whose
  ``subagent.started`` names a non-``sync`` mode is pending until its
  ``subagent.completed`` / ``subagent.failed``.
* **A failed turn.** Every model round opens with ``assistant.turn_start`` and,
  when the model answers, writes an ``assistant.message`` (tool requests
  included) before its ``assistant.turn_end``. A closed round that wrote none,
  with no ``abort`` (a person pressing Esc), is a turn that failed: Copilot's
  "No response was returned" end. A round still open is a model still
  thinking, never a failure. A ``session.error`` inside the failed round is
  kept as the reason.
* **A tool still pending.** A ``tool.execution_start`` with no matching
  ``tool.execution_complete``.

The tail is bounded, so a fact whose markers fell out of it stays unknown:
``failed`` is ``None`` when the tail holds no round, and an unreadable file is
``None`` as a whole, never a clean stop.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Union

#: Enough of ``events.jsonl`` for the last turn without reading a long session whole.
TAIL_BYTES = 512 * 1024

#: Copilot's interactive question tool: pending, it waits on a person.
ASK_TOOL = "ask_user"
_STOP_SHELL_TOOL = "stop_powershell"
_ERROR_CHARS = 300


@dataclass(frozen=True)
class TurnEnd:
    """The facts a tail establishes about the newest turn."""

    background_pending: bool = False
    failed: Optional[bool] = None
    error: Optional[str] = None
    pending_tools: FrozenSet[str] = field(default_factory=frozenset)
    #: The failed round's closing ``assistant.turn_end`` stamp (ISO), set only when ``failed``.
    failed_at: Optional[str] = None


def _data(entry: Dict[str, Any]) -> Dict[str, Any]:
    data = entry.get("data")
    return data if isinstance(data, dict) else {}


def _async_shell_id(data: Dict[str, Any]) -> Optional[str]:
    """The ``shellId`` a background (non-detached) shell launch acknowledges."""
    telemetry = data.get("toolTelemetry")
    props = telemetry.get("properties") if isinstance(telemetry, dict) else None
    if not isinstance(props, dict) or props.get("executionMode") != "async":
        return None
    if str(props.get("detached")).lower() == "true":
        return None
    result = data.get("result")
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    shell = structured.get("shellId") if isinstance(structured, dict) else None
    return str(shell) if shell is not None else None


class _Scan:
    """One pass over the tail's events, oldest first."""

    def __init__(self) -> None:
        self.shells: Dict[str, None] = {}
        self.agents: Dict[str, None] = {}
        self.tools: Dict[str, str] = {}
        self.replied: Optional[bool] = None
        self.aborted = False
        self.error: Optional[str] = None
        self.closed_at: Optional[str] = None

    def _open_round(self) -> None:
        self.replied = False
        self.aborted = False
        self.error = None
        self.closed_at = None

    def feed(self, entry: Dict[str, Any]) -> None:
        kind = entry.get("type")
        data = _data(entry)
        if data.get("parentToolCallId"):
            return  # a sub-agent's own round or tool call: its parent's task call stands for it
        if kind in ("user.message", "assistant.turn_start"):
            self._open_round()
        elif kind == "assistant.message":
            self.replied = True
        elif kind == "assistant.turn_end" and self.replied is not None:
            self.closed_at = str(entry.get("timestamp") or "") or self.closed_at or "?"
        elif kind == "abort":
            self.aborted = True
        elif kind == "session.error":
            message = data.get("message")
            if isinstance(message, str) and message.strip():
                self.error = message.strip()[:_ERROR_CHARS]
        elif kind == "tool.execution_start":
            call = data.get("toolCallId")
            if isinstance(call, str):
                self.tools[call] = str(data.get("toolName") or "")
            if data.get("toolName") == _STOP_SHELL_TOOL:
                args = data.get("arguments")
                shell = args.get("shellId") if isinstance(args, dict) else None
                self.shells.pop(str(shell), None)
        elif kind == "tool.execution_complete":
            self.tools.pop(str(data.get("toolCallId")), None)
            shell = _async_shell_id(data)
            if shell is not None:
                self.shells[shell] = None
        elif kind == "system.notification":
            notice = data.get("kind")
            if isinstance(notice, dict):
                for key in ("shellId", "agentId", "toolCallId"):
                    if notice.get(key) is not None:
                        self.shells.pop(str(notice[key]), None)
                        self.agents.pop(str(notice[key]), None)
        elif kind == "subagent.started":
            if str(data.get("executionMode") or "sync") != "sync":
                self.agents[str(data.get("toolCallId") or entry.get("agentId"))] = None
        elif kind in ("subagent.completed", "subagent.failed"):
            self.agents.pop(str(data.get("toolCallId") or entry.get("agentId")), None)

    def result(self) -> TurnEnd:
        if self.replied is None or (not self.replied and self.closed_at is None):
            failed: Optional[bool] = None  # no round in the tail, or one still open
        else:
            failed = not self.replied and not self.aborted
        return TurnEnd(
            background_pending=bool(self.shells or self.agents),
            failed=failed,
            error=self.error if failed else None,
            pending_tools=frozenset(self.tools.values()),
            failed_at=self.closed_at if failed else None,
        )


def read_turn_end(path: Union[str, Path, None], tail_bytes: int = TAIL_BYTES) -> Optional[TurnEnd]:
    """The newest turn's facts from the tail of ``path``; ``None`` when it cannot be read."""
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    return turn_end_from_text(tail)


def turn_end_from_text(tail: str) -> TurnEnd:
    """The newest turn's facts from an ``events.jsonl`` tail already read."""
    scan = _Scan()
    for line in tail.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # the torn first line of a mid-file tail, or a torn last write
        if isinstance(entry, dict):
            scan.feed(entry)
    return scan.result()
