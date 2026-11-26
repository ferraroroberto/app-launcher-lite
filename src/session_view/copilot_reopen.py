"""Which session folder a launch's Copilot process has on screen.

Every interactive launch creates a folder of its own. A conversation picked
afterwards (the ``--resume`` picker, ``/resume``) stays in its original
folder, which gets no line until the next prompt, so the launch's folder is
left without a single turn. What ties the two is ``inuse.<pid>.lock``: a
Copilot process keeps one in every session folder it has opened, stamped when
it opened it, and drops them when it exits (probed on CLI 1.0.116).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import psutil

_LOCK_RE = re.compile(r"^inuse\.(\d+)\.lock$")
# A cheap byte filter; the parsed record's own ``type`` decides.
_USER_TURN_MARK = b'"user.message"'
# A log bigger than this is no empty launch folder, whatever it holds.
_EMPTY_LOG_MAX_BYTES = 512 * 1024
# Logs seen to hold a turn: a log only grows, so that never changes.
_HAS_TURN: Set[Path] = set()
# ``(state dir, launch folder) -> (scan instant, folder found)``. Opening a
# conversation moves no mtime a ``stat`` of the listing would see.
_SHOWN_CACHE: Dict[Tuple[Path, Path], Tuple[float, Optional[Path]]] = {}
_CACHE_MAX = 256


def holds_no_turn(log: Path) -> bool:
    """True when ``log`` is readable and nobody has said anything in it."""
    if log in _HAS_TURN:
        return False
    try:
        with log.open("rb") as fh:
            head = fh.read(_EMPTY_LOG_MAX_BYTES + 1)
    except OSError:
        return False
    if len(head) <= _EMPTY_LOG_MAX_BYTES and not any(
        _is_user_turn(line) for line in head.split(b"\n") if _USER_TURN_MARK in line
    ):
        return True
    if len(_HAS_TURN) >= _CACHE_MAX:
        _HAS_TURN.clear()
    _HAS_TURN.add(log)
    return False


def _is_user_turn(line: bytes) -> bool:
    try:
        return json.loads(line).get("type") == "user.message"
    except (ValueError, AttributeError):
        return False


def lock_holders(folder: Path, since: float) -> Dict[int, float]:
    """``{pid: opened at}`` for the live processes that opened ``folder`` at or
    after ``since``. A lock a crashed process left behind names nobody."""
    holders: Dict[int, float] = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return holders
    for name in names:
        match = _LOCK_RE.match(name)
        if match is None:
            continue
        pid = int(match.group(1))
        try:
            opened = (folder / name).stat().st_mtime
        except OSError:
            continue
        if opened >= since and psutil.pid_exists(pid):
            holders[pid] = opened
    return holders


def shown_folder(
    state_dir: Path, launch: Path, since: float, until: float, ttl: float
) -> Optional[Path]:
    """The folder the process that created ``launch`` opened last, when that is
    another one; else None.

    The launching process is the one holding ``launch`` since between
    ``since`` and ``until``; with none or several, nothing is followed. A
    result is kept for ``ttl`` seconds.
    """
    key = (state_dir, launch)
    now = time.time()
    hit = _SHOWN_CACHE.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    found = _scan(state_dir, launch, since, until)
    if len(_SHOWN_CACHE) >= _CACHE_MAX:
        _SHOWN_CACHE.clear()
    _SHOWN_CACHE[key] = (now, found)
    return found


def _scan(state_dir: Path, launch: Path, since: float, until: float) -> Optional[Path]:
    owners = [(pid, at) for pid, at in lock_holders(launch, since).items() if at <= until]
    if len(owners) != 1:
        return None
    pid, newest = owners[0]
    found: Optional[Path] = None
    try:
        for lock in state_dir.glob(f"*/inuse.{pid}.lock"):
            opened = lock.stat().st_mtime
            if lock.parent != launch and opened > newest:
                newest, found = opened, lock.parent
    except OSError:
        return None
    return found
