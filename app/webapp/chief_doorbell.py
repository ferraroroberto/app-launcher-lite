"""The chief doorbell.

Every :data:`TICK_SECONDS` the canonical webapp checks three facts about the
standing fleet chief and, when all hold, types one fixed, data-free line into
its terminal (:data:`DOORBELL_LINE`):

* the chief is **idle at its prompt**: its Board status, computed through the
  same overlay ``GET /api/board`` uses (:func:`src.board.merge_sessions`), is
  ``idle-finished``, ``awaiting-input`` or ``waiting-on-task``. The last is
  the chief's normal state while lanes run (it ends every turn with a
  background waiter armed): its turn is over and its composer is free, so a
  typed line starts a new turn. ``tool-pending`` (a permission prompt) and
  ``awaiting-decision`` (a picker) are not idle: a typed line would land in
  the dialog, not the composer. A ``working`` label whose terminal has been
  silent for :data:`STALE_WORKING_SECONDS` is also idle: the label is known to
  stick, and the measured output age is the evidence, never
  the label alone;
* its composer is **clean**: the session-host's ``composer_dirty`` is
  ``False``. ``None`` (unknown, or a session-host too old to send it) never
  rings;
* its mailbox holds **pending** events, counted by file name alone per
  fleet-config ``docs/chief-mailbox.md``: root ``*.json`` plus stale claims.

The ring is level-triggered. The condition must hold for :data:`DEBOUNCE_SECONDS`
first, so lanes that finish together become one ring. After a ring the bell
waits :data:`BACKOFF_SECONDS`; a chief still idle with pending events after
that never got the ring, so it rings again. Seeing the chief busy, or the
mailbox empty, means the ring landed, and the back-off ends early.

Off unless ``WebappConfig.chief_doorbell`` is on (default off; the owner turns
it on from Settings > Chief > Doorbell). The config is re-read every
tick, so a Settings save takes effect without a restart. Canonical instance
only, like the coverage tick: a disposable gate webapp must never type into a
real session.

The line carries no count, issue number or worker text. Everything the chief
acts on comes from ``chief_ops.py inbox``.

A ring counts only when the session-host confirms its submit
(``submit_state`` ``confirmed``). Anything else, a deferred or unconfirmed
submit or Copilot leaving the line on its input, is logged as not delivered,
never as rung, and the back-off re-ring covers it.

A missed wake must surface on its own. When the oldest pending event has
waited :data:`UNREAD_ALERT_SECONDS`, the tick logs one warning per backlog
with its current skip reason, and :func:`unread_mailbox_age` lets
``GET /api/board`` move the chief's card to Your turn with the wait shown.

Breadcrumbs go to ``webapp/chief-doorbell.log``: every ring and its delivery
verdict, and every skip reason when it changes, repeated every
:data:`SKIP_REPEAT_SECONDS` while it holds (a 3 s tick logging each skip would
be ~29k lines a day).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.webapp.routers import board_chief
from app.webapp.routers.board_spawn import read_live_sessions
from src import board
from src.board import card_status
from src.clients import session as session_client
from src.session_view.automatic_turns import (
    DOORBELL_LINE,
    ORIGIN_DOORBELL,
    record_injected,
)

logger = logging.getLogger(__name__)

TICK_SECONDS = 3.0
DEBOUNCE_SECONDS = 3.0
BACKOFF_SECONDS = 60.0

#: A ``working`` chief whose terminal has been silent this long is a stuck
#: label, not a busy chief: a real turn repaints its spinner many times a
#: second, so ten quiet minutes is two orders past any honest pause.
STALE_WORKING_SECONDS = 600.0

#: An unchanged skip reason is logged again this often, so a long hold shows
#: in ``chief-doorbell.log`` instead of vanishing after its first line.
SKIP_REPEAT_SECONDS = 600.0

#: A ``.claimed/`` claim older than this counts as pending again
#: (``chief_ops.CLAIM_STALE_SECONDS``).
CLAIM_STALE_MS = 900_000

#: A pending event this old means the chief missed its wake: the back-off
#: re-rings every minute, so fifteen minutes is many rings that did not land.
UNREAD_ALERT_SECONDS = 900.0

#: Root events and claims alike are ``<epoch-ms>-<id>.json``.
_STAMPED_NAME = re.compile(r"^(\d+)-(.+)\.json$")

#: Board statuses that mean "at its prompt, nothing pending on screen".
#: ``waiting-on-task`` is included because the chief ends every turn with a
#: background waiter armed: its turn is over and its composer is free, and a
#: typed line starts a new turn while the waiter keeps running.
IDLE_STATUSES = card_status.DOORBELL_IDLE


def doorbell_on(cfg: object) -> bool:
    """Only a real ``True`` arms it: it types into a live session, so a
    config without the key (a stub) or a non-boolean reads as off."""
    return getattr(cfg, "chief_doorbell", False) is True


def _is_counted_name(entry: Any) -> bool:
    return (
        entry.name.endswith(".json")
        and not entry.name.startswith(".")
        and entry.is_file()
    )


@dataclass(frozen=True)
class PendingScan:
    """The pending count, and the oldest stamp among the pending names
    (``None`` when none carries one)."""

    count: int
    oldest_ms: Optional[int] = None

    def oldest_age(self, now_ms: int) -> Optional[float]:
        if self.oldest_ms is None or self.count <= 0:
            return None
        return max(0.0, (now_ms - self.oldest_ms) / 1000.0)


def _stamp(name: str) -> Optional[int]:
    match = _STAMPED_NAME.match(name)
    return int(match.group(1)) if match else None


def _oldest(stamps: List[Optional[int]]) -> Optional[int]:
    known = [s for s in stamps if s is not None]
    return min(known) if known else None


def scan_pending(inbox_dir: Path, now_ms: int) -> Optional[PendingScan]:
    """Pending events in ``inbox_dir`` by the mailbox contract's counting rule.

    ``R + S``: regular, non-dot ``*.json`` files in the root, plus those in
    ``.claimed/`` whose claim is stale (or carries no readable time). Never
    opens a file. A missing directory is ``0``; one present but not listable
    is ``None`` (unknown), never ``0``. A stale claim's stamp is its claim
    time, so the oldest age it gives is a lower bound.
    """
    try:
        with os.scandir(inbox_dir) as entries:
            root = [_stamp(e.name) for e in entries if _is_counted_name(e)]
    except FileNotFoundError:
        return PendingScan(0)
    except OSError:
        return None
    claimed_dir = inbox_dir / ".claimed"
    try:
        with os.scandir(claimed_dir) as entries:
            names = [e.name for e in entries if _is_counted_name(e)]
    except FileNotFoundError:
        return PendingScan(len(root), _oldest(root))
    except OSError:
        return None
    stale = []
    for name in names:
        stamp = _stamp(name)
        if stamp is None or now_ms - stamp >= CLAIM_STALE_MS:
            stale.append(stamp)
    return PendingScan(len(root) + len(stale), _oldest(root + stale))


def count_pending(inbox_dir: Path, now_ms: int) -> Optional[int]:
    """:func:`scan_pending`'s count alone."""
    scan = scan_pending(inbox_dir, now_ms)
    return None if scan is None else scan.count


def unread_mailbox_age(cfg: Any) -> Optional[float]:
    """Seconds the oldest pending event has waited, once that passes
    :data:`UNREAD_ALERT_SECONDS` with the doorbell on; else ``None``.

    Read from the inbox alone, independent of the doorbell's tick, so the
    Board shows a missed wake whatever the bell decided."""
    if cfg is None or not doorbell_on(cfg):
        return None
    now_ms = int(time.time() * 1000)
    scan = scan_pending(Path(cfg.chief_inbox_dir), now_ms)
    age = None if scan is None else scan.oldest_age(now_ms)
    if age is None or age < UNREAD_ALERT_SECONDS:
        return None
    return age


@dataclass(frozen=True)
class Observation:
    """One tick's facts. ``None`` in a field means it could not be read."""

    pending: Optional[int]
    chief_sid: Optional[str] = None
    status: Optional[str] = None
    dirty: Optional[bool] = None
    #: Seconds since the chief's terminal last produced output; ``None`` when
    #: the session-host sent no usable stamp.
    output_age: Optional[float] = None
    #: Set when a fact could not be established (host unreadable, no chief).
    blocked: Optional[str] = None
    #: Seconds the oldest pending event has waited; ``None`` when unknown.
    oldest_age: Optional[float] = None

    @property
    def idle(self) -> Optional[bool]:
        if self.status is None:
            return None
        if self.status == "working" and self.output_age is not None:
            return self.output_age >= STALE_WORKING_SECONDS
        return self.status in IDLE_STATUSES

    def summary(self) -> str:
        age = "-" if self.output_age is None else f"{self.output_age:.0f}s"
        oldest = "-" if self.oldest_age is None else f"{self.oldest_age:.0f}s"
        return (
            f"idle={self.idle} status={self.status} dirty={self.dirty} "
            f"output_age={age} pending={self.pending} oldest={oldest} "
            f"chief={(self.chief_sid or '-')[:8]}"
        )


def _output_age(stamp: Any, now_epoch: float) -> Optional[float]:
    """Seconds since the host's ``last_output_at`` epoch stamp, or ``None``
    when it is absent or not a positive number (an older host, ``0`` for
    "never recorded"). A stamp ahead of the clock reads as age ``0``."""
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
        return None
    return max(0.0, now_epoch - float(stamp))


async def observe(cfg: Any) -> Observation:
    """Read the chief's facts, the file and session reads off the loop.

    The pending count runs first and an empty mailbox stops there, so the
    steady state costs two directory listings and no session reads. The label
    reconcile stays on the loop, as in ``GET /api/board``: it schedules the
    chief-pointer write with ``create_task``.
    """
    now_ms = int(time.time() * 1000)
    scan = await asyncio.to_thread(scan_pending, Path(cfg.chief_inbox_dir), now_ms)
    pending = None if scan is None else scan.count
    oldest_age = None if scan is None else scan.oldest_age(now_ms)
    if not pending:
        return Observation(pending=pending)
    (live, host_error), state = await asyncio.gather(
        asyncio.to_thread(read_live_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    if host_error is not None:
        return Observation(
            pending=pending,
            oldest_age=oldest_age,
            blocked=f"session-host unreadable: {host_error}",
        )
    live = board_chief._reconcile_chief_labels(
        live, state["rows"], cfg.fleet_config_dir
    )
    chief = board_chief._find_chief(live)
    if not chief:
        return Observation(pending=pending, oldest_age=oldest_age, blocked="no live chief")
    sid = str(chief.get("session_id") or "")
    # A session-host that predates the composer field sends none: unknown.
    dirty = chief.get("composer_dirty")
    if not isinstance(dirty, bool):
        dirty = None
    status = None
    cards = await asyncio.to_thread(board.merge_sessions, live, state["rows"])
    for card in cards:
        if str(card.get("session_id") or "") == sid:
            status = str(card.get("status") or "") or None
            break
    return Observation(
        pending=pending,
        chief_sid=sid,
        status=status,
        dirty=dirty,
        output_age=_output_age(chief.get("last_output_at"), time.time()),
        oldest_age=oldest_age,
    )


class Doorbell:
    """The debounce + back-off state machine. Pure: time comes in as an
    argument, so tests drive it without sleeping."""

    def __init__(self) -> None:
        self._eligible_since: Optional[float] = None
        self._last_ring: Optional[float] = None
        self._alarmed = False

    def unread_alarm(self, obs: Observation) -> bool:
        """``True`` once per backlog, on the first tick its oldest event has
        waited :data:`UNREAD_ALERT_SECONDS`; an emptied mailbox re-arms it."""
        if obs.pending is not None and obs.pending <= 0:
            self._alarmed = False
            return False
        if obs.oldest_age is None or obs.oldest_age < UNREAD_ALERT_SECONDS:
            return False
        if self._alarmed:
            return False
        self._alarmed = True
        return True

    def decide(self, obs: Observation, now: float) -> str:
        """``"ring"``, or the reason not to."""
        if obs.blocked:
            self._eligible_since = None
            return obs.blocked
        if obs.pending is None:
            self._eligible_since = None
            return "pending unknown"
        if obs.pending <= 0:
            # The chief drained its mailbox: the last ring (if any) landed.
            self._eligible_since = None
            self._last_ring = None
            return "mailbox empty"
        if obs.idle is not True:
            self._eligible_since = None
            if obs.idle is False:
                # Busy: the chief took the ring, or is working anyway. A
                # later idle with pending events is a new condition.
                self._last_ring = None
                return f"chief not idle ({obs.status})"
            return "chief status unknown"
        if obs.dirty is not False:
            self._eligible_since = None
            return "composer dirty" if obs.dirty else "composer unknown"
        if self._last_ring is not None and now - self._last_ring < BACKOFF_SECONDS:
            return "backing off after ring"
        if self._eligible_since is None:
            self._eligible_since = now
        if now - self._eligible_since < DEBOUNCE_SECONDS:
            return "debouncing"
        self._eligible_since = None
        self._last_ring = now
        return "ring"


class _SkipLog:
    """Log a skip reason when it changes, and again every
    :data:`SKIP_REPEAT_SECONDS` while it holds."""

    def __init__(self) -> None:
        self._last: Optional[str] = None
        self._logged_at: float = 0.0

    def note(self, reason: str, obs: Optional[Observation], now: float) -> None:
        if reason == self._last and now - self._logged_at < SKIP_REPEAT_SECONDS:
            return
        self._last = reason
        self._logged_at = now
        detail = f" ({obs.summary()})" if obs is not None else ""
        logger.info("🔕 chief doorbell skip: %s%s", reason, detail)

    def reset(self) -> None:
        self._last = None


async def tick_once(
    cfg: Any,
    bell: Doorbell,
    skips: _SkipLog,
    *,
    now: Callable[[], float] = time.monotonic,
    observe_fn: Callable[[Any], Awaitable[Observation]] = observe,
    send_fn: Callable[..., Dict[str, Any]] = session_client.send_input,
    record_fn: Callable[..., bool] = record_injected,
) -> str:
    """One tick. Returns the decision (``"ring"``, a skip reason, or ``"off"``)."""
    tick_now = now()
    if cfg is None or not doorbell_on(cfg):
        skips.note("off", None, tick_now)
        return "off"
    try:
        obs = await observe_fn(cfg)
    except Exception as exc:  # noqa: BLE001 — a failed read is a skip, never a crash
        reason = f"observe failed: {exc}"
        skips.note(reason, None, tick_now)
        return reason
    decision = bell.decide(obs, tick_now)
    if bell.unread_alarm(obs):
        logger.warning(
            "⚠️ chief has not read its mailbox for %d min, doorbell decision: %s (%s)",
            int((obs.oldest_age or 0) // 60), decision, obs.summary(),
        )
    if decision != "ring":
        skips.note(decision, obs, tick_now)
        return decision
    skips.reset()
    # Taken before the send: the transcript line can land before send_input
    # returns, and the ledger row must not be later than the line it matches.
    sent_at = time.time()
    try:
        result = await asyncio.to_thread(
            send_fn, cfg.session_host_port, obs.chief_sid, DOORBELL_LINE, True
        )
    except session_client.SessionHostError as exc:
        # The back-off already started: a host that refuses the write is
        # retried in BACKOFF_SECONDS, not hammered every tick.
        logger.warning("⚠️ chief doorbell ring failed: %s (%s)", exc, obs.summary())
        return "ring failed"
    # The Chat pane folds this turn from the row; a failed write is logged by
    # the ledger and only leaves the turn to the fixed-line match.
    await asyncio.to_thread(
        record_fn, obs.chief_sid, DOORBELL_LINE, ORIGIN_DOORBELL, now=sent_at
    )
    body = result if isinstance(result, dict) else {}
    verdict = ring_verdict(body)
    if verdict == "ring":
        logger.info(
            "🔔 chief doorbell rang, submit confirmed (%s) reason=%s",
            obs.summary(), body.get("reason"),
        )
    else:
        logger.warning(
            "⚠️ chief doorbell ring not delivered: submit_state=%s delivered=%s "
            "reason=%s (%s)",
            body.get("submit_state"), body.get("delivered"), body.get("reason"),
            obs.summary(),
        )
    return verdict


def ring_verdict(body: Dict[str, Any]) -> str:
    """``"ring"`` only for a delivered, confirmed submit; anything the host
    did not confirm (deferred, unconfirmed, untaken, a field missing) is
    ``"ring not delivered"``."""
    if body.get("delivered") is True and body.get("submit_state") == "confirmed":
        return "ring"
    return "ring not delivered"


async def tick_forever(app: Any) -> None:
    """The lifespan loop. Reads ``app.state.webapp_config`` every tick."""
    bell = Doorbell()
    skips = _SkipLog()
    while True:
        try:
            await tick_once(getattr(app.state, "webapp_config", None), bell, skips)
        except Exception:  # noqa: BLE001 — never take the lifespan down
            logger.exception("❌ chief doorbell tick failed")
        await asyncio.sleep(TICK_SECONDS)
