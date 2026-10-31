"""Shared atomic-JSON-write and interprocess-lock helpers.

Six call sites across ``src/`` each hand-rolled the same
tempfile-then-``os.replace`` dance with their own suffix bikeshed. This
module is the single place that owns it. It also owns the msvcrt-based
file-lock-with-retry pattern (issue #520) that both ``src.jobs.queue`` and
``src.jobs.history`` need for their own read-modify-write critical sections.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # Windows-only interprocess file lock (this repo runs Windows-only).
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# How many times ``file_lock`` calls ``msvcrt.locking(LK_LOCK)`` before it
# gives up and lets the caller write unlocked. ``LK_LOCK`` already retries
# internally (10 attempts, 1 s apart, ~10 s) before raising ``OSError``, so
# the true worst case is ``_LOCK_ATTEMPTS`` x ~10 s = ~30 s of blocking: long
# enough to ride out a slow sibling process finishing its write, short enough
# that a wedged holder never hangs the webapp's kill and queue paths for good.
_LOCK_ATTEMPTS = 3


def atomic_write_json(target: Path, payload: Any, *, indent: int = 2) -> None:
    """Write ``payload`` as JSON to ``target`` atomically.

    Serializes to a sibling ``<name><suffix>.tmp`` file, then ``os.replace``s
    it over ``target`` — the swap is all-or-nothing, so a crash mid-write or
    a concurrent reader never observes a partially-written file. Caller is
    responsible for ensuring ``target.parent`` exists.
    """
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=indent), encoding="utf-8")
    os.replace(tmp, target)


@contextmanager
def file_lock(lock_path: Path, *, label: str) -> Iterator[None]:
    """Hold an exclusive interprocess lock for a read-modify-write section.

    Shared by :mod:`src.jobs.queue` (``_queue_file_lock``) and
    :mod:`src.jobs.history` (``_run_json_lock``) — both need to serialize a
    read-modify-``os.replace`` across genuinely separate OS processes (the
    webapp process and a spawned executor process). Uses a Windows
    ``msvcrt.locking`` byte-range lock on a dedicated ``lock_path`` sidecar
    file, never on the JSON document itself (which ``os.replace`` swaps out
    from under any held handle). Retries a few times before giving up and
    proceeding unlocked — logged via ``label`` — since losing serialization
    is far better than wedging the caller's operation forever. The worst-case
    wait is about 30 s (see ``_LOCK_ATTEMPTS``).

    Callers resolve ``lock_path`` themselves (rather than this helper
    caching it) so tests that monkeypatch the underlying JSON path redirect
    the lock file too, instead of touching a stale production path.
    """
    if msvcrt is None:  # pragma: no cover - non-Windows fallback
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    locked = False
    try:
        for attempt in range(_LOCK_ATTEMPTS):
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
                break
            except OSError:
                if attempt == _LOCK_ATTEMPTS - 1:
                    logger.warning(
                        "⚠️  %s lock contended — writing unlocked", label
                    )
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()
