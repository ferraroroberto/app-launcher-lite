"""Where a card's exchange comes from: the row's declared transcript, the Copilot event log, and the state row the merge renders."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from src import board
from src.board import exchange as board_exchange
from tests.api.board._support import (
    NOW,
    _assistant_line,
    _iso,
    _live_sess,
    _user_line,
    _write_jsonl,
)


def test_declared_transcript_wins_and_reports_native(tmp_path: Path):
    declared = tmp_path / "declared.jsonl"
    _write_jsonl(declared, [
        _user_line("declared?"),
        _assistant_line([{"type": "text", "text": "Declared conversation."}]),
    ])

    result = board_exchange.resolve_exchange(
        {"session_id": "s1", "agent": "copilot"}, str(declared), tmp_path / "absent.transcript",
    )

    assert result["source"] == "native"
    assert result["assistant"]["text"] == "Declared conversation."


def test_an_agent_with_no_structured_history_degrades_to_the_launcher_capture(tmp_path: Path):
    capture = tmp_path / "exact.transcript"
    capture.write_text("● Exact session reply.\r\n", encoding="utf-8")

    result = board_exchange.resolve_exchange(
        {"agent": "claude", "project_dir": "E:/proj/app", "started_at": NOW.timestamp(),
         "prompt_title": "question"},
        None, capture,
    )

    assert result["source"] == "launcher"
    assert result["assistant"]["text"] == "Exact session reply."


def test_state_row_for_session_matches_render_claim():
    """#537: an ambiguous single row (2 live sessions sharing one cwd)
    matches neither — state_row_for_session must stay consistent with what
    merge_sessions renders rather than resolving the ambiguity differently."""
    live = [_live_sess("old", "E:/a/x", 120), _live_sess("new", "E:/a/x", 5)]
    rows = {
        "t1": {"cwd": "E:/a/x", "status": "needs-you",
               "updated_at": _iso(NOW - timedelta(minutes=1)),
               "transcript_path": "p1"},
    }
    assert board.state_row_for_session(live, rows, "new") is None
    assert board.state_row_for_session(live, rows, "old") is None
    assert board.state_row_for_session(live, rows, "ghost") is None
