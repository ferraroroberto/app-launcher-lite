"""Transcript tails, read once per change of the file.

The session list and the Board both poll every few seconds, and each build
asks for every live session's transcript tail several times over: the
activity anchor, the model, the waiting-status scans, the provider link. A
file system call on a transcript the session-host is still appending to can
stall for seconds under on-access antivirus scanning, so every repeat open is
another chance at that stall, on both endpoints.

So :func:`read_tail` keeps the newest tail per path, keyed by the file's
``(size, mtime_ns)``. A file that has not changed answers from memory after
one ``stat``; a smaller window of a cached tail is sliced from it, so a
session costs one bounded read per change however many readers ask. Reads of
the same path are serialised, so polls that land together share the one read
instead of each opening the file. The cache holds at most
``transcript_tail_cache_mb`` of tails (Settings > Performance, applied by
:func:`set_budget_mb`); 0 turns it off and every call reads the file.

A read slower than :data:`SLOW_READ_S`, or a wait that long behind another
tail read (locks are striped by path), leaves a breadcrumb in ``webapp/slow-requests.log``
naming the file, so a stall reads apart from a busy pool.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional, Tuple

slow_logger = logging.getLogger("launcher.slowreq")

# Matches the shipped default of ``transcript_tail_cache_mb``; the app sets
# the owner's value at start and after every Settings save.
DEFAULT_BUDGET_MB = 32
# Well under a poll's 1.5 s deadline, well over a normal tail read (a few ms).
SLOW_READ_S = 1.0
# The Board's exchange window. A re-read widens to cover every reader up to
# this; a larger window (an event log's 4 MB) is read only for its own caller.
_WIDEN_MAX_BYTES = 256 * 1024
# Locks are striped by path so the set never grows with the session count;
# two paths sharing a stripe only queue behind each other's read.
_STRIPES = 64

# path -> (size, mtime_ns, window asked for, tail bytes)
_Entry = Tuple[int, int, int, bytes]
_TAILS: "OrderedDict[str, _Entry]" = OrderedDict()
_TAILS_LOCK = threading.Lock()
_STRIPE_LOCKS = tuple(threading.Lock() for _ in range(_STRIPES))
_budget = {"bytes": DEFAULT_BUDGET_MB * 1024 * 1024, "held": 0}


def set_budget_mb(mb: int) -> None:
    """Cap the cache at ``mb`` megabytes of tails; 0 empties and disables it."""
    with _TAILS_LOCK:
        _budget["bytes"] = max(0, int(mb)) * 1024 * 1024
        _evict()


def clear() -> None:
    """Drop every cached tail."""
    with _TAILS_LOCK:
        _TAILS.clear()
        _budget["held"] = 0


def read_tail(path: Any, n_bytes: int) -> bytes:
    """The last ``n_bytes`` of ``path``; ``b""`` on any OSError, as before."""
    key = str(path)
    if _budget["bytes"] <= 0:
        return _read(key, n_bytes)
    asked = time.monotonic()
    with _STRIPE_LOCKS[hash(key) % _STRIPES]:
        waited = time.monotonic() - asked
        if waited >= SLOW_READ_S:
            slow_logger.info(
                "⏱️ transcript tail %s: waited %.1fs behind another tail read",
                _name(key), waited,
            )
        try:
            st = os.stat(key)
        except OSError:
            return b""
        hit, window = _cached(key, st.st_size, st.st_mtime_ns, n_bytes)
        if hit is not None:
            return hit
        raw = _read(key, window)
        _store(key, (st.st_size, st.st_mtime_ns, window, raw))
        return raw[-n_bytes:] if n_bytes < len(raw) else raw


def _cached(key: str, size: int, mtime_ns: int, n_bytes: int) -> Tuple[Optional[bytes], int]:
    """The tail from memory, or ``None`` and the window a fresh read takes."""
    with _TAILS_LOCK:
        entry = _TAILS.get(key)
        if entry is None:
            return None, n_bytes
        window, raw = entry[2], entry[3]
        # A re-read takes the widest window asked of this file (up to the
        # Board's), so the wider reader slices it instead of opening it again.
        wider = max(window, n_bytes)
        wider = wider if wider <= _WIDEN_MAX_BYTES else n_bytes
        if entry[0] != size or entry[1] != mtime_ns:
            return None, wider
        # A smaller window is a slice of a larger one; a whole small file
        # answers any window.
        if n_bytes > window and len(raw) < size:
            return None, wider
        _TAILS.move_to_end(key)
    return (raw[-n_bytes:] if n_bytes < len(raw) else raw), window


def _store(key: str, entry: _Entry) -> None:
    with _TAILS_LOCK:
        old = _TAILS.pop(key, None)
        if old is not None:
            _budget["held"] -= len(old[3])
        if len(entry[3]) > _budget["bytes"]:
            return
        _TAILS[key] = entry
        _budget["held"] += len(entry[3])
        _evict()


def _evict() -> None:
    while _TAILS and _budget["held"] > _budget["bytes"]:
        _key, dropped = _TAILS.popitem(last=False)
        _budget["held"] -= len(dropped[3])


def _read(key: str, n_bytes: int) -> bytes:
    started = time.monotonic()
    try:
        with Path(key).open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - n_bytes))
            return fh.read()
    except OSError:
        return b""
    finally:
        took = time.monotonic() - started
        if took >= SLOW_READ_S:
            slow_logger.info(
                "⏱️ transcript tail %s: read %d KB in %.1fs", _name(key), n_bytes // 1024, took
            )


def _name(key: str) -> str:
    """The file's name only: a breadcrumb never carries a full local path."""
    return Path(key).name
