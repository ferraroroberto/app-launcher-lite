"""Live sessions carry ``started_at`` as the session-host's epoch float."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import board_sessions

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _live(session_id: str, minutes_ago: int) -> dict:
    return {
        "session_id": session_id,
        "kind": "pty",
        "agent": "copilot",
        "project_dir": "E:/a/x",
        "alive": True,
        "started_at": (NOW - timedelta(minutes=minutes_ago)).timestamp(),
    }


def test_session_started_reads_epoch_float():
    assert board_sessions._session_started(_live("s", 5)) == NOW - timedelta(minutes=5)


def test_session_started_still_reads_iso_and_rejects_junk():
    iso = {"started_at": "2026-06-01T11:55:00+00:00"}
    assert board_sessions._session_started(iso) == NOW - timedelta(minutes=5)
    assert board_sessions._session_started({"started_at": True}) is None
    assert board_sessions._session_started({"started_at": "garbage"}) is None
    assert board_sessions._session_started({}) is None


def test_newest_session_claims_first():
    live = [_live("old", 120), _live("new", 5)]
    pairs, _ = board_sessions._claim_walk(live, {})
    assert [sess["session_id"] for sess, _, _ in pairs] == ["new", "old"]
