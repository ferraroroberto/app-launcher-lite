"""Resumable Coding launches keyed by a caller's request id.

A launch waits on the session-host's create call, which can outlive the
caller's own HTTP budget while the spawn still succeeds server-side. A caller
that gives up and retries would then open a second session. With a request id
the first call claims a slot, the spawn finishes into that slot whether or not
the caller is still connected, and a repeat with the same id answers from the
slot instead of spawning again: the session when it is done, ``pending`` while
it is still being created, the original refusal when it failed.

The slots live in the webapp process only: a tray restart forgets them, and a
caller that cannot find its id is told ``unknown``, never "nothing launched".
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

PENDING = "pending"
DONE = "done"
FAILED = "failed"

# A slot outlives any caller's poll of it; an hour also bounds the memory a
# stream of distinct ids can hold.
SLOT_TTL_SECONDS = 3600.0

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def valid_request_id(value: Any) -> bool:
    """True for a request id this registry accepts: 1-64 URL-safe characters."""
    return isinstance(value, str) and bool(_REQUEST_ID.match(value))


@dataclass
class LaunchSlot:
    """One request id's launch: its state, the launch response once done, or
    the HTTP status and detail it failed with."""

    state: str = PENDING
    response: Optional[Dict[str, Any]] = None
    status: Optional[int] = None
    detail: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    # The task finishing the spawn, held so it is never garbage-collected
    # while the caller that started it has gone.
    task: Any = None

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"state": self.state}
        if self.state == DONE:
            out["session"] = (self.response or {}).get("session")
        if self.state == FAILED:
            out.update(status=self.status, detail=self.detail)
        return out


class LaunchRequests:
    """The slots, keyed by ``(app_id, request_id)``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: Dict[Tuple[str, str], LaunchSlot] = {}

    def claim(self, app_id: str, request_id: str) -> Tuple[LaunchSlot, bool]:
        """``(slot, True)`` for a new id, now pending; ``(slot, False)`` for one already seen."""
        key = (app_id, request_id)
        with self._lock:
            self._prune()
            slot = self._slots.get(key)
            if slot is not None:
                return slot, False
            slot = self._slots[key] = LaunchSlot()
            return slot, True

    def get(self, app_id: str, request_id: str) -> Optional[LaunchSlot]:
        with self._lock:
            self._prune()
            return self._slots.get((app_id, request_id))

    def _prune(self) -> None:
        cutoff = time.monotonic() - SLOT_TTL_SECONDS
        for key in [k for k, s in self._slots.items() if s.state != PENDING and s.created_at < cutoff]:
            del self._slots[key]
