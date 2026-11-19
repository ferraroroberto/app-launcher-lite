"""A Copilot session's last exchange comes from its ``events.jsonl``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from src.board import exchange as board_exchange
from src.session_view import transcript_locate


def _event(kind: str, data: Dict[str, Any], ts: str) -> str:
    return json.dumps({"type": kind, "data": data, "timestamp": ts})


def _write_events(path: Path, events: List[str]) -> Path:
    path.write_text("\n".join(events) + "\n", encoding="utf-8")
    return path


def _session() -> Dict[str, Any]:
    return {"agent": "copilot", "project_dir": "E:/proj/app", "started_at": 1_789_600_000.0}


def test_copilot_exchange_reads_the_last_reply_from_events(tmp_path: Path):
    events = _write_events(tmp_path / "events.jsonl", [
        _event("user.message", {"content": "first question"}, "t1"),
        _event("assistant.message", {"content": "first answer"}, "t2"),
        _event("user.message", {"content": "second question"}, "t3"),
        _event("assistant.message", {"content": "second answer"}, "t4"),
        _event("model.response", {"content": "bookkeeping, never a reply"}, "t5"),
    ])
    result = board_exchange.resolve_exchange(_session(), str(events), tmp_path / "none")
    assert result["available"] is True
    assert result["source"] == "native"
    assert result["user"] == {"text": "second question", "timestamp": "t3"}
    assert result["assistant"] == {"text": "second answer", "timestamp": "t4"}


def test_copilot_exchange_uses_the_task_complete_summary_as_the_reply(tmp_path: Path):
    events = _write_events(tmp_path / "events.jsonl", [
        _event("user.message", {"content": "how many sessions?"}, "t1"),
        _event("tool.execution_start", {
            "toolCallId": "c1", "toolName": "powershell", "arguments": {"command": "ls"},
        }, "t2"),
        _event("tool.execution_start", {
            "toolCallId": "c2", "toolName": "task_complete",
            "arguments": {"summary": "Two sessions are running."},
        }, "t3"),
    ])
    result = board_exchange.resolve_exchange(_session(), str(events), tmp_path / "none")
    assert result["available"] is True
    assert result["assistant"]["text"] == "Two sessions are running."
    assert result["user"]["text"] == "how many sessions?"


def test_copilot_exchange_locates_the_events_when_no_row_names_them(
    tmp_path: Path, monkeypatch,
):
    events = _write_events(tmp_path / "events.jsonl", [
        _event("user.message", {"content": "status?"}, "t1"),
        _event("assistant.message", {"content": "All green."}, "t2"),
    ])
    monkeypatch.setattr(transcript_locate, "find_copilot_transcript", lambda session: events)
    result = board_exchange.resolve_exchange(_session(), None, tmp_path / "none")
    assert result["source"] == "native"
    assert result["assistant"]["text"] == "All green."


def test_copilot_exchange_with_no_events_file_stays_unavailable(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(transcript_locate, "find_copilot_transcript", lambda session: None)
    result = board_exchange.resolve_exchange(_session(), None, tmp_path / "none")
    assert result["available"] is False
    assert result["reason"] == "no_exchange"
