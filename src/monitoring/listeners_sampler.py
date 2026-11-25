"""The Apps tab's port listeners, scanned in the background and read from memory.

A psutil listener scan (socket table plus per-process name, exe, cwd and
command line) costs about a second on a fast box and several on a slow one.
Run per request, polls queued behind each other. So **a read never scans**:
:meth:`ListenersSampler.read` returns the cached snapshot with its age, and one
background tick (:meth:`ListenersSampler.tick_forever`, started from the webapp
lifespan) runs the scan on the sampler's own one-thread pool.

* **Demand-driven.** The tick scans only while a read arrived within
  :data:`DEMAND_WINDOW_SECONDS`, the same pattern as ``src/system_load.py``;
  an unwatched launcher does no scan.
* **Spaced by the finish.** The next scan is due :data:`TICK_SECONDS` after the
  last one *finished*, so a slow box spends a bounded share of its CPU here
  however long a scan takes.
* **Single-flight.** One scan at a time, however many reads or ticks arrive.
* **Unknown is its own state.** No snapshot yet (``warming``), a scan running
  past :data:`SCAN_BUDGET_SECONDS` (``overrun``), a scan that raised
  (``error``), one that could not read the socket table (``unavailable``) and
  a snapshot older than :data:`MAX_AGE_SECONDS` (``stale``) all read
  ``value=None`` with a reason, never an empty list.

Breadcrumbs go to ``webapp/slow-requests.log`` (the ``launcher.slowreq``
logger): the live webapp's own stdout is discarded by the tray.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, Optional, TypeVar

from src.diagnostics import ListenerScanUnavailable
from src.request_timing import TimedExecutor

T = TypeVar("T")

breadcrumbs = logging.getLogger("launcher.slowreq")

# Gap between the end of one scan and the start of the next.
TICK_SECONDS = 8.0
# How often the tick wakes to check demand; the work only happens on a due wake.
WAKE_SECONDS = 1.0
DEMAND_WINDOW_SECONDS = 60.0
# A scan running longer than this reads ``overrun``: a slow box's scan takes
# seconds, a wedged one never ends, and the page must tell them apart from empty.
SCAN_BUDGET_SECONDS = 10.0
# Past this a snapshot is not served as current (the tick died or stalled).
MAX_AGE_SECONDS = 3 * TICK_SECONDS

# The sampler's own thread: a slow scan stalls only this reading, never a request.
SCAN_POOL = TimedExecutor("listeners-sample", max_workers=1)


@dataclass(frozen=True)
class Reading(Generic[T]):
    """One read: the cached value, or ``None`` with why it is unknown."""

    value: Optional[T]
    reason: Optional[str]
    detail: str
    # Seconds since the last good scan started; None before the first one.
    age_seconds: Optional[float]


class ListenersSampler(Generic[T]):
    def __init__(
        self,
        scan: Callable[[], T],
        *,
        executor: Any = SCAN_POOL,
        clock: Callable[[], float] = time.monotonic,
        tick_s: float = TICK_SECONDS,
        demand_s: float = DEMAND_WINDOW_SECONDS,
        budget_s: float = SCAN_BUDGET_SECONDS,
        max_age_s: float = MAX_AGE_SECONDS,
    ) -> None:
        self.scan = scan
        self.executor = executor
        self.clock = clock
        self.tick_s = tick_s
        self.demand_s = demand_s
        self.budget_s = budget_s
        self.max_age_s = max_age_s
        self._lock = threading.Lock()
        self._last_demand: Optional[float] = None
        self._in_flight_since: Optional[float] = None
        self._last_finished: Optional[float] = None
        self._snapshot: Optional[T] = None
        self._snapshot_at: Optional[float] = None
        self._failure: Optional[tuple[str, str]] = None
        self._overrun_logged = False

    def read(self) -> Reading[T]:
        """The cached reading. Marks demand; never scans and never waits."""
        now = self.clock()
        with self._lock:
            self._last_demand = now
            started = self._in_flight_since
            failure = self._failure
            snapshot, at = self._snapshot, self._snapshot_at
            overrun = started is not None and now - started > self.budget_s
            if overrun and not self._overrun_logged:
                self._overrun_logged = True
                log_overrun = now - started
            else:
                log_overrun = None
        age = None if at is None else round(now - at, 1)
        if log_overrun is not None:
            breadcrumbs.warning(
                "⚠️ listeners scan has run %.1fs, over its %.1fs budget; the Apps tab reads unknown",
                log_overrun, self.budget_s,
            )
        if overrun:
            return Reading(None, "overrun", f"the scan has run {now - started:.1f}s, over its {self.budget_s:.0f}s budget", age)
        if failure is not None:
            return Reading(None, failure[0], failure[1], age)
        if snapshot is None or at is None:
            return Reading(None, "warming", "no scan has finished yet", age)
        if now - at > self.max_age_s:
            return Reading(None, "stale", f"the last scan is {now - at:.0f}s old", age)
        return Reading(snapshot, None, "", age)

    def maybe_scan(self) -> bool:
        """Start one scan if someone is reading, none is running and one is due."""
        now = self.clock()
        with self._lock:
            if self._in_flight_since is not None:
                return False
            if self._last_demand is None or now - self._last_demand > self.demand_s:
                return False
            if self._last_finished is not None and now - self._last_finished < self.tick_s:
                return False
            self._in_flight_since = now
        try:
            self.executor.submit(self._run, now)
        except Exception:  # noqa: BLE001 — a pool that cannot take work must not wedge single-flight
            with self._lock:
                self._in_flight_since = None
            breadcrumbs.exception("❌ listeners scan could not be queued")
            return False
        return True

    def _run(self, started: float) -> None:
        reason: Optional[str] = None
        detail = ""
        value: Optional[T] = None
        try:
            value = self.scan()
        except ListenerScanUnavailable as exc:
            reason, detail = "unavailable", str(exc)
        except Exception as exc:  # noqa: BLE001 — a failed scan must not end the sampler
            reason, detail = "error", f"{type(exc).__name__}: {exc}"
        finished = self.clock()
        with self._lock:
            was_failing = self._failure is not None
            if reason is None:
                self._snapshot, self._snapshot_at, self._failure = value, started, None
            else:
                self._failure = (reason, detail)
            self._in_flight_since = None
            self._overrun_logged = False
            self._last_finished = finished
        took = finished - started
        if reason is not None and not was_failing:
            breadcrumbs.warning("⚠️ listeners scan unknown (%s): %s", reason, detail)
        elif reason is None and was_failing:
            breadcrumbs.info("✅ listeners scan recovered")
        if took > self.budget_s:
            breadcrumbs.warning("⚠️ listeners scan took %.1fs (budget %.1fs)", took, self.budget_s)

    async def tick_forever(self) -> None:
        """Wake each WAKE_SECONDS and start a scan when one is due and wanted."""
        while True:
            await asyncio.sleep(WAKE_SECONDS)
            self.maybe_scan()
