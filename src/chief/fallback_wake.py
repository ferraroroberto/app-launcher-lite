"""The launcher's fallback wake for the chief.

The doorbell (:mod:`app.webapp.chief_doorbell`) rings an idle chief as soon as
its mailbox fills. When it misses or holds off (the switch is off, or the
chief's input line reads as unknown), nothing else wakes a Copilot chief: it
cannot run a background waiter of its own. This is the backup. With
``chief_fallback_minutes`` at N (0 = off) it rings the chief once, through the
doorbell's line and its ring path, when either holds:

* ``mailbox``: the oldest pending mailbox event has waited N minutes;
* ``slots``: worker slots have been free beside ``queued`` plan rows for N
  minutes (the plan has work and the chief is not starting it).

The doorbell's safety holds: never while the chief is not idle, never while
its composer is dirty. A composer that reads unknown holds the ring until it
has read unknown for N minutes, then the ring goes through once, because an
unknown composer would otherwise hold it forever. At most one ring per N
minutes.

Pure: time and every fact come in as arguments, so tests drive it with a fake
clock. The launcher glue (observation, the ring, the log) is in
:mod:`app.webapp.chief_doorbell`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Dict, List, Mapping, Optional

from src.chief.identity import CHIEF_LABEL
from src.chief.queue_status import QueueStatus, queue_status

#: How often the launcher evaluates the fallback. A minute is far below any
#: sensible N and keeps the session-host read off the doorbell's 3 s tick.
CHECK_SECONDS = 60.0

#: The cap scope that counts only sessions the chief dispatched.
_SCOPE_CHIEF = "chief"

#: Standing Telegram channel sessions carry this label prefix and are never
#: workers (the same rule as the chief's own worker count).
_CHANNEL_LABEL_PREFIX = "telegram:"

REASON_MAILBOX = "mailbox"
REASON_SLOTS = "slots"


def queued_rows(plan_index: Mapping[str, Any]) -> Optional[int]:
    """How many plan queue rows wait as ``queued``; ``None`` for an unreadable
    plan (:func:`src.chief.plan.read_plan_index`'s ``state``)."""
    if plan_index.get("state") != "ok":
        return None
    return sum(
        1 for row in plan_index.get("queue") or [] if queue_status(row) == QueueStatus.QUEUED
    )


def free_slots(
    live: List[Dict[str, Any]],
    chief_sid: str,
    cap: int,
    scope: str,
    managed: Optional[Collection[str]],
) -> Optional[int]:
    """Worker slots the chief could still fill.

    Counts alive sessions other than the chief and Telegram channel sessions;
    under the ``chief`` scope only those in the chief-managed registry. A
    ``chief`` scope whose registry could not be read (``managed`` ``None``)
    is unknown, never a guess.
    """
    if scope == _SCOPE_CHIEF and managed is None:
        return None
    alive = 0
    for sess in live:
        sid = str(sess.get("session_id") or "")
        label = str(sess.get("label") or "")
        if not sess.get("alive") or sid == chief_sid or label == CHIEF_LABEL:
            continue
        if label.startswith(_CHANNEL_LABEL_PREFIX):
            continue
        if scope == _SCOPE_CHIEF and sid not in (managed or ()):
            continue
        alive += 1
    return max(cap - alive, 0)


@dataclass(frozen=True)
class Facts:
    """One check's facts. ``None`` means the fact could not be read."""

    #: Seconds the oldest pending mailbox event has waited.
    oldest_age: Optional[float]
    free_slots: Optional[int]
    queued: Optional[int]
    #: The chief is at its prompt (the doorbell's own idle test).
    idle: Optional[bool]
    #: The session-host's ``composer_dirty``.
    dirty: Optional[bool]
    #: Set when the chief cannot be rung at all (no live chief, host down).
    blocked: Optional[str] = None

    @property
    def slots_idle(self) -> bool:
        return bool(self.free_slots and self.queued)


@dataclass(frozen=True)
class Decision:
    ring: bool
    #: Why it rings (``mailbox``, ``slots`` or both, plus the detail), or why not.
    reason: str


class FallbackWake:
    """The fallback's state: when it last rang, since when slots have sat
    idle and since when the composer has read unknown."""

    def __init__(self) -> None:
        self._last_check: Optional[float] = None
        self._last_ring: Optional[float] = None
        self._slots_since: Optional[float] = None
        self._unknown_since: Optional[float] = None

    def due(self, now: float) -> bool:
        """``True`` once per :data:`CHECK_SECONDS`; marks the check taken."""
        if self._last_check is not None and now - self._last_check < CHECK_SECONDS:
            return False
        self._last_check = now
        return True

    def reset(self) -> None:
        self._last_ring = self._slots_since = self._unknown_since = None

    def _reasons(self, facts: Facts, window: float, now: float) -> List[str]:
        reasons = []
        if facts.oldest_age is not None and facts.oldest_age >= window:
            reasons.append(f"{REASON_MAILBOX} (oldest event waited {int(facts.oldest_age // 60)} min)")
        if facts.slots_idle:
            if self._slots_since is None:
                self._slots_since = now
            if now - self._slots_since >= window:
                reasons.append(
                    f"{REASON_SLOTS} ({facts.free_slots} free, {facts.queued} queued "
                    f"for {int((now - self._slots_since) // 60)} min)"
                )
        else:
            self._slots_since = None
        return reasons

    def _track_composer(self, facts: Facts, now: float) -> None:
        if facts.dirty is None and facts.blocked is None:
            if self._unknown_since is None:
                self._unknown_since = now
        else:
            self._unknown_since = None

    def decide(self, minutes: int, facts: Facts, now: float) -> Decision:
        """Whether to ring now, and why (or why not)."""
        if minutes <= 0:
            self.reset()
            return Decision(False, "off")
        window = minutes * 60.0
        self._track_composer(facts, now)
        if facts.blocked:
            return Decision(False, facts.blocked)
        reasons = self._reasons(facts, window, now)
        if not reasons:
            return Decision(False, "nothing waiting")
        if self._last_ring is not None and now - self._last_ring < window:
            return Decision(False, "rang within the last window")
        if facts.idle is not True:
            return Decision(False, "chief not idle" if facts.idle is False else "chief status unknown")
        if facts.dirty is True:
            return Decision(False, "composer dirty")
        forced = ""
        if facts.dirty is None:
            since = now if self._unknown_since is None else self._unknown_since
            if now - since < window:
                return Decision(False, "composer unknown")
            forced = "; composer unknown past the window, ringing once anyway"
        self._last_ring = now
        self._unknown_since = None
        self._slots_since = None
        return Decision(True, "; ".join(reasons) + forced)
