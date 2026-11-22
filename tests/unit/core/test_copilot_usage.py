"""The Copilot usage reader (``src.copilot_usage``) against a temporary log.

Synthetic rows only. Pins the month and today sums, the UTC billing month and
its pace, and that every unreadable log (missing, wrong shape, locked) reads
as ``unknown`` with a reason, never as 0 credits.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import copilot_usage

NOW = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
CREDIT = copilot_usage.NANO_PER_CREDIT


def _log(path: Path, rows: list[tuple[str, int]]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE assistant_usage_events (id INTEGER PRIMARY KEY, model TEXT,"
        " total_nano_aiu INTEGER, created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.executemany(
        "INSERT INTO assistant_usage_events (model, total_nano_aiu, created_at) VALUES ('m', ?, ?)",
        [(nano, at) for at, nano in rows],
    )
    conn.commit()
    conn.close()
    return path


def test_sums_this_month_and_today_in_both_timestamp_shapes(tmp_path: Path) -> None:
    db = _log(tmp_path / "s.db", [
        ("2026-10-31T23:59:59.999Z", 50 * CREDIT),   # last month
        ("2026-11-01T00:00:00.000Z", 10 * CREDIT),   # first instant of the month
        ("2026-11-21 08:00:00", 5 * CREDIT),          # SQLite's own default shape
        ("2026-11-22T09:30:00.000Z", CREDIT // 2),    # today
        ("2026-12-01T00:00:00.000Z", 70 * CREDIT),   # next month
    ])
    reading = copilot_usage.read(db, NOW)
    assert reading == copilot_usage.Reading("ok", used_credits=15.5, today_credits=0.5)


def test_empty_month_is_zero_not_unknown(tmp_path: Path) -> None:
    reading = copilot_usage.read(_log(tmp_path / "s.db", []), NOW)
    assert (reading.state, reading.used_credits, reading.today_credits) == ("ok", 0, 0)


def test_missing_log_is_unknown(tmp_path: Path) -> None:
    reading = copilot_usage.read(tmp_path / "absent.db", NOW)
    assert reading.state == "unknown"
    assert "no Copilot usage log" in reading.reason


@pytest.mark.parametrize("ddl, reason", [
    ("CREATE TABLE other (x INTEGER)", "no assistant_usage_events table"),
    ("CREATE TABLE assistant_usage_events (id INTEGER, created_at TEXT)", "lacks total_nano_aiu"),
])
def test_wrong_shape_is_unknown(tmp_path: Path, ddl: str, reason: str) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute(ddl)
    conn.commit()
    conn.close()
    reading = copilot_usage.read(db, NOW)
    assert reading.state == "unknown"
    assert reason in reading.reason


def test_not_a_database_is_unknown(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    db.write_bytes(b"not sqlite at all, just bytes" * 100)
    assert copilot_usage.read(db, NOW).state == "unknown"


def test_locked_log_is_unknown_within_the_lock_timeout(tmp_path: Path) -> None:
    db = _log(tmp_path / "s.db", [("2026-11-02T00:00:00Z", CREDIT)])
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        reading = copilot_usage.read(db, NOW)
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert reading.state == "unknown"
    assert elapsed < copilot_usage.LOCK_TIMEOUT_S + 2


@pytest.mark.parametrize("now, start, reset", [
    (datetime(2026, 11, 22, tzinfo=timezone.utc), "2026-11-01", "2026-12-01"),
    (datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc), "2026-12-01", "2027-01-01"),
    (datetime(2028, 2, 29, 12, tzinfo=timezone.utc), "2028-02-01", "2028-03-01"),
])
def test_billing_month_is_the_utc_calendar_month(now: datetime, start: str, reset: str) -> None:
    got = copilot_usage.billing_month(now)
    assert (got[0].date().isoformat(), got[1].date().isoformat()) == (start, reset)


def test_pace_follows_the_month_length() -> None:
    feb_start, feb_reset = copilot_usage.billing_month(datetime(2028, 2, 1, tzinfo=timezone.utc))
    # Leap February: half of 29 days is noon on the 15th.
    assert copilot_usage.pace_pct(datetime(2028, 2, 15, 12, tzinfo=timezone.utc), feb_start, feb_reset) == 50.0
    assert copilot_usage.pace_pct(feb_start, feb_start, feb_reset) == 0.0


def test_payload_applies_the_allowance_and_never_claims_zero_when_unknown() -> None:
    ok = copilot_usage.payload(copilot_usage.Reading("ok", 75.0, 3.0), 300, NOW)
    assert (ok["state"], ok["used_pct"], ok["allowance"], ok["reset_at"][:10]) == ("ok", 25.0, 300, "2026-12-01")
    assert ok["scope"] == "this machine"
    unknown = copilot_usage.payload(copilot_usage.Reading("unknown", reason="locked"), 300, NOW)
    assert (unknown["used_pct"], unknown["used_credits"], unknown["reason"]) == (None, None, "locked")
    assert copilot_usage.payload(None, 300, NOW)["state"] == "pending"
