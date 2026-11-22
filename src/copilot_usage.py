"""Copilot's AI credits billed this month on this machine, from the CLI's own usage log.

The Copilot CLI records every model call it bills in the
``assistant_usage_events`` table of ``~/.copilot/session-store.db``, sub-agents
included, with the cost in ``total_nano_aiu`` (one AI credit is 1e9 nano) and a
UTC ``created_at``. That covers this machine's CLI only: VS Code chat and other
machines spend from the same allowance without appearing here, which is why
the payload names its ``scope``.

The billing month starts on the 1st at 00:00 UTC. A file that is missing,
locked or shaped differently reads as ``unknown`` with the reason, never as 0
credits, because a card showing 0% would claim a fact nobody established.

Reads are synchronous and bounded (a 1 s lock timeout); the webapp runs them
in a worker thread, off the request path (``app/webapp/routers/copilot_usage.py``).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

TABLE = "assistant_usage_events"
REQUIRED_COLUMNS = frozenset({"total_nano_aiu", "created_at"})
NANO_PER_CREDIT = 1_000_000_000
# A Copilot write holds its lock for milliseconds; a lock held past this is
# reported as unknown rather than waited on.
LOCK_TIMEOUT_S = 1.0
SOURCE = "Copilot CLI usage log (~/.copilot/session-store.db)"
SCOPE = "this machine"


def default_db_path() -> Path:
    return Path.home() / ".copilot" / "session-store.db"


@dataclass(frozen=True)
class Reading:
    """What the log says, before the allowance and the clock are applied."""

    state: str  # "ok" | "unknown"
    used_credits: Optional[float] = None
    today_credits: Optional[float] = None
    reason: str = ""


def billing_month(now: datetime) -> Tuple[datetime, datetime]:
    """The UTC month holding ``now``: its start and the next month's start (the reset)."""
    now = now.astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    reset = (start + timedelta(days=32)).replace(day=1)
    return start, reset


def pace_pct(now: datetime, start: datetime, reset: datetime) -> float:
    """The share of the billing month gone, 0 to 100."""
    span = (reset - start).total_seconds()
    gone = (now.astimezone(timezone.utc) - start).total_seconds()
    return round(max(0.0, min(100.0, 100.0 * gone / span)), 1)


def _day_key(moment: datetime) -> str:
    # ``created_at`` is ISO (``2026-11-21T15:18:17.971Z``) or SQLite's own
    # ``2026-11-21 15:18:17``; both sort after a bare date prefix of their day.
    return moment.strftime("%Y-%m-%d")


def read(db_path: Path, now: datetime) -> Reading:
    """Sum this month's and today's credits from the log; never raises."""
    if not db_path.is_file():
        return Reading("unknown", reason="no Copilot usage log on this machine")
    start, reset = billing_month(now)
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        # must-use-exempt: a read-only (mode=ro) open of Copilot's own log; connect_sqlite opens read-write and takes no URI
        # runtime-write-exempt: mode=ro, so nothing is written; the file is Copilot's, under the user's home
        conn = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=LOCK_TIMEOUT_S,
        )
    except sqlite3.Error as exc:
        logger.warning("⚠️ Copilot usage log %s did not open: %s", db_path, exc)
        return Reading("unknown", reason=f"the usage log did not open: {exc}")
    try:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
        if not columns:
            return Reading("unknown", reason=f"the usage log has no {TABLE} table")
        missing = REQUIRED_COLUMNS - columns
        if missing:
            return Reading(
                "unknown", reason=f"the usage log lacks {', '.join(sorted(missing))}",
            )
        month_nano, today_nano = conn.execute(
            f"SELECT COALESCE(SUM(total_nano_aiu), 0),"
            f" COALESCE(SUM(CASE WHEN created_at >= ? THEN total_nano_aiu END), 0)"
            f" FROM {TABLE} WHERE created_at >= ? AND created_at < ?",
            (_day_key(today), _day_key(start), _day_key(reset)),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("⚠️ Copilot usage log %s unreadable: %s", db_path, exc)
        return Reading("unknown", reason=f"the usage log could not be read: {exc}")
    finally:
        conn.close()
    return Reading(
        "ok",
        used_credits=round(month_nano / NANO_PER_CREDIT, 2),
        today_credits=round(today_nano / NANO_PER_CREDIT, 2),
    )


def payload(reading: Optional[Reading], allowance: int, now: datetime) -> Dict[str, Any]:
    """The ``/api/copilot-usage`` body: the reading against the allowance and the month.

    ``reading`` is None while the first read is still running (``state: pending``).
    """
    start, reset = billing_month(now)
    body: Dict[str, Any] = {
        "state": "pending" if reading is None else reading.state,
        "used_credits": None,
        "today_credits": None,
        "used_pct": None,
        "allowance": allowance,
        "pace_pct": pace_pct(now, start, reset),
        "cycle_start": start.isoformat(),
        "reset_at": reset.isoformat(),
        "source": SOURCE,
        "scope": SCOPE,
        "reason": "" if reading is None else reading.reason,
    }
    if reading is not None and reading.state == "ok" and reading.used_credits is not None:
        body["used_credits"] = reading.used_credits
        body["today_credits"] = reading.today_credits
        body["used_pct"] = round(100.0 * reading.used_credits / allowance, 1)
    return body
