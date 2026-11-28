"""JSONL builders and page walkers shared by the transcript unit modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from src.session_view import transcript as st

# ------------------------------------------------------------ fixtures

TS = "2026-09-14T10:00:00.000Z"


def _write_jsonl(path: Path, lines: List[Dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    return path


def _event(kind: str, data: Dict[str, Any], ts: str = TS, **extra: Any) -> Dict[str, Any]:
    """One Copilot ``events.jsonl`` line."""
    row: Dict[str, Any] = {"type": kind, "data": data, "id": "e1", "parentId": None, "timestamp": ts}
    row.update(extra)
    return row


def _user(text: str, ts: str = TS) -> Dict[str, Any]:
    return _event("user.message", {"content": text}, ts)


def _assistant(text: str, ts: str = TS, **data: Any) -> Dict[str, Any]:
    return _event("assistant.message", {"content": text, **data}, ts)


def _thinking(text: str, ts: str = TS) -> Dict[str, Any]:
    return _event("assistant.message", {"reasoningText": text}, ts)


def _tool_use(name: str, inputs: Any, tid: str, ts: str = TS) -> Dict[str, Any]:
    return _event("tool.execution_start", {"toolCallId": tid, "toolName": name, "arguments": inputs}, ts)


def _tool_result(tid: str, content: str, ts: str = TS, *, ok: bool = True) -> Dict[str, Any]:
    return _event(
        "tool.execution_complete",
        {"toolCallId": tid, "success": ok, "result": {"content": content}}, ts,
    )


def _lines_of(path: Path) -> List[Any]:
    """Every non-blank line of ``path`` as the ``(byte offset, text)`` pairs
    the entry builders take."""
    raw = path.read_bytes()
    lines, pos = [], 0
    for chunk in raw.split(b"\n"):
        if chunk.strip():
            lines.append((pos, chunk.decode("utf-8", errors="replace")))
        pos += len(chunk) + 1
    return lines


def _parse_whole(
    path: Path, build=st.copilot_entries, *, tail: int = -1
) -> List[Dict[str, Any]]:
    """Reference: one parse of the whole file — offset stripped from the
    folded kinds, kept on turns, matching :func:`session_transcript._page`.

    ``tail`` is the live-tail offset of the page(s) being compared: from
    there on a folded entry keeps its offset too, because that region is
    what a live view re-renders and it needs a key per card. Default -1
    means "no page ended at EOF", i.e. strip them all.
    """
    entries = build(_lines_of(path))
    for e in entries:
        if not st._is_turn(e) and not (tail >= 0 and e.get("offset", -1) >= tail):
            e.pop("offset", None)
    return entries


def _tail_of(path: Path, limit: int, flavor: str = "copilot") -> int:
    """The newest page's live-tail offset — where its provisional
    region begins, and so which entries keep a folded offset."""
    return st.transcript_page(path, limit=limit, flavor=flavor)["tail"]


def _walk(path: Path, limit: int, flavor: str = "copilot") -> List[List[Dict[str, Any]]]:
    """Every page from newest to oldest, following ``next_cursor``."""
    pages, cursor, guard = [], None, 0
    while True:
        page = st.transcript_page(path, before=cursor, limit=limit, flavor=flavor)
        pages.append(page["entries"])
        cursor = page["next_cursor"]
        guard += 1
        assert guard < 10_000, "cursor never reached the file start"
        if cursor is None:
            return pages


def _kinds(entries: List[Dict[str, Any]]) -> List[str]:
    return [e["kind"] for e in entries]


# ------------------------------------------------------- transcript_page


def _conversation(turns: int, *, tool_calls_per_turn: int = 2) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    for i in range(turns):
        lines.append(_user(f"prompt {i} " + "p" * (i % 7)))
        lines.append(_thinking(f"think {i}"))
        lines.append(_assistant(f"reply {i} opener"))
        for k in range(tool_calls_per_turn):
            tid = f"t{i}-{k}"
            lines.append(_tool_use("powershell", {"command": f"cmd {i} {k}"}, tid))
            lines.append(_tool_result(tid, "out " + "o" * ((i * k) % 50)))
        lines.append(_assistant(f"reply {i} closer " + "r" * (i % 11)))
    return lines


def _live(sid: str = "s1", **extra: Any) -> Dict[str, Any]:
    row = {"session_id": sid, "kind": "pty", "agent": "copilot", "alive": True,
           "project_dir": "E:/stubfolder/proj", "started_at": 1_789_000_000}
    row.update(extra)
    return row


# ------------------------------------------------------- copilot_entries

COPILOT_CAPTURE = Path(__file__).resolve().parents[2] / "fixtures" / "copilot_events.jsonl"
