"""``GET /api/copilot-usage``: the Code tab's Usage card, never on the request path.

The reads (``src.copilot_usage.read`` and ``read_plan``) touch files another
process writes, the usage log through SQLite, so they can stall on a lock or a
slow disk. A request therefore
never waits for it: it gets the last reading at once (``state: pending``
before the first one lands) and, when that reading is older than
:data:`REFRESH_S`, starts one refresh in a worker thread. Refreshes are
single-flight, so a stalled read is never stacked, and one whose log file and
UTC day are unchanged is skipped without opening the database (the plan cache,
one small JSON file, is read on every refresh). Nothing reads
the log while nobody asks, and page load never asks: the card's module loads
after the session list has drawn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import APIRouter, Request

from src import copilot_usage
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

router = APIRouter()

# The card polls every 60 s; a reading younger than this is served as is.
REFRESH_S = 30.0

_Signature = Tuple[Tuple[float, int], ...]


def _signature(db_path: Path, now: datetime) -> Optional[Tuple[str, _Signature]]:
    """The log's mtime and size (plus its WAL's) and the UTC day, or None if unstattable."""
    parts = []
    for path in (db_path, db_path.with_name(db_path.name + "-wal")):
        try:
            stat = path.stat()
        except OSError:
            if path == db_path:
                return None
            continue
        parts.append((stat.st_mtime, stat.st_size))
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d"), tuple(parts)


class UsageSnapshot:
    """The last reading, refreshed in the background while it is asked for."""

    def __init__(
        self,
        db_path: Callable[[], Path] = copilot_usage.default_db_path,
        reader: Callable[[Path, datetime], copilot_usage.Reading] = copilot_usage.read,
        clock: Callable[[], float] = time.monotonic,
        plan_path: Callable[[], Path] = copilot_usage.default_plan_cache_path,
        plan_reader: Callable[[Path, datetime], copilot_usage.PlanReading] = copilot_usage.read_plan,
    ) -> None:
        self._db_path = db_path
        self._reader = reader
        self._clock = clock
        self._plan_path = plan_path
        self._plan_reader = plan_reader
        self._reading: Optional[copilot_usage.Reading] = None
        self._plan: Optional[copilot_usage.PlanReading] = None
        self._read_at: Optional[float] = None
        self._signature: Optional[Tuple[str, _Signature]] = None
        self._task: Optional["asyncio.Future[None]"] = None

    def current(
        self,
    ) -> Tuple[Optional[copilot_usage.Reading], Optional[copilot_usage.PlanReading]]:
        """The last machine and plan readings at once; starts a refresh when due."""
        due = self._read_at is None or self._clock() - self._read_at >= REFRESH_S
        if due and (self._task is None or self._task.done()):
            self._task = asyncio.ensure_future(asyncio.to_thread(self._refresh))
            # A failed refresh is logged in _refresh; nobody awaits the task.
            self._task.add_done_callback(lambda done: done.cancelled() or done.exception())
        return self._reading, self._plan

    def _refresh(self) -> None:
        started = self._clock()
        now = datetime.now(timezone.utc)
        path = self._db_path()
        try:
            signature = _signature(path, now)
            # Only a good reading is kept on an unchanged file: an unknown one
            # (a lock, say) is retried.
            stale = self._reading is None or self._reading.state != "ok"
            if stale or signature is None or signature != self._signature:
                self._reading = self._reader(path, now)
                self._signature = signature
        except Exception as exc:  # noqa: BLE001 — a reader bug must read as unknown, not crash the card
            logger.exception("❌ Copilot usage refresh failed")
            self._reading = copilot_usage.Reading("unknown", reason=f"the usage read failed: {exc}")
            self._signature = None
        try:
            self._plan = self._plan_reader(self._plan_path(), now)
        except Exception as exc:  # noqa: BLE001 — a reader bug must read as unknown, not crash the card
            logger.exception("❌ Copilot plan refresh failed")
            self._plan = copilot_usage.PlanReading("unknown", reason=f"the plan read failed: {exc}")
        finally:
            self._read_at = started


def usage_snapshot(app: Any) -> UsageSnapshot:
    snapshot = getattr(app.state, "copilot_usage", None)
    if snapshot is None:
        snapshot = app.state.copilot_usage = UsageSnapshot()
    return snapshot


@router.get("/api/copilot-usage")
async def get_copilot_usage(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    reading, plan = usage_snapshot(request.app).current()
    return copilot_usage.payload(reading, plan, cfg.copilot_monthly_credits, datetime.now(timezone.utc))
