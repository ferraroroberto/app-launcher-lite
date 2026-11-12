"""Board tab — a card's status is the hook row's own status; no transcript is read."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from src import board
from tests.unit.board._board_helpers import (
    NOW,
    _live,
    _state_row,
    _transcript_file,
)


@pytest.mark.parametrize("raw, expected", [
    ("working", "working"),
    ("idle", "idle"),
    ("needs-you", "awaiting-input"),
    ("something-new", "unknown"),
    (None, "unknown"),
])
def test_live_card_status_maps_the_hook_status(raw, expected):
    rows = {} if raw is None else {"t": _state_row("E:/x/y", status=raw)}
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], rows, now=NOW)
    assert cards[0]["status"] == expected


def test_needs_you_never_reaches_a_card():
    row = _state_row("E:/x/y", status="needs-you")
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-input"


def test_transcript_activity_does_not_change_the_hook_status(tmp_path: Path):
    """A transcript written well after the row's stamp used to flip a waiting
    card to ``working``; the transcript is no longer read, so the hook's own
    status stands whatever the file says."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-input"
    # The age anchors to the hook stamp.
    assert cards[0]["age_seconds"] == 600


def test_missing_transcript_keeps_the_hook_status(tmp_path: Path):
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = str(tmp_path / "gone.jsonl")
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle"


def test_busy_live_title_does_not_change_the_hook_status():
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    sess = _live("aaa", "E:/x/y", 30, live_title="⠋ working", last_output_at=NOW.timestamp())
    cards = board.merge_sessions([sess], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-input"


def test_external_card_status_is_the_hook_status(tmp_path: Path):
    row = _state_row("E:/automation/reporting", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards[0]["kind"] == "external"
    assert cards[0]["status"] == "awaiting-input"
    assert cards[0]["age_seconds"] == 600


def test_parse_iso_reads_a_naive_stamp_as_local_unless_told_utc():
    from datetime import timezone

    from src.board_state import parse_iso

    local = parse_iso("2026-01-02T03:04:05")
    utc = parse_iso("2026-01-02T03:04:05", naive_utc=True)
    assert local is not None and utc is not None
    assert utc.tzinfo is timezone.utc and utc.hour == 3
    assert local == local.astimezone() and local.utcoffset() is not None
    # An explicit offset or "Z" wins over the naive rule.
    assert parse_iso("2026-01-02T03:04:05Z", naive_utc=False) == utc
    assert parse_iso("") is None and parse_iso(None) is None and parse_iso("nope") is None
