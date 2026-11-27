"""A session's terminal, rendered from its PTY capture.

The session-host writes each session's output to a capture file. Reading its
tail and replaying it through ``pyte`` at the PTY's own size gives the screen
as the terminal shows it (Copilot's folder-trust dialog,
:mod:`src.session_view.folder_trust`) or the text of the latest reply
(:mod:`src.board.exchange`).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Tuple

import pyte

logger = logging.getLogger(__name__)

# Enough capture to hold a dialog and the reply above it. The tail is rendered
# at the PTY's own size, so only its last screenful counts; 96 KB renders in
# well under 0.1 s.
CAPTURE_TAIL_BYTES = 96 * 1024


def read_capture_tail(path: Path, n_bytes: int = CAPTURE_TAIL_BYTES) -> Optional[str]:
    """The last ``n_bytes`` of a session's PTY capture, or ``None`` when the
    file can't be read.

    The session-host writes the capture in text mode, so on Windows every
    ``\\n`` the agent printed is on disk as ``\\r\\n`` (a ``\\r\\n`` as
    ``\\r\\r\\n``). Replacing each ``\\r\\n`` with ``\\n`` undoes that exactly;
    reading in text mode would not, since it also turns a lone ``\\r`` into
    a newline. The cut starts after the first newline so the render never
    begins inside an escape sequence.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - n_bytes))
            raw = fh.read()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    if os.linesep != "\n":
        text = text.replace(os.linesep, "\n")
    if size > n_bytes:
        nl = text.find("\n")
        text = text[nl + 1:] if nl >= 0 else ""
    return text


# Copilot's TUI asks ``CSI ? Ps n`` (private-mode device status); pyte's
# ``report_device_status`` takes no ``private`` argument and raises, and a
# replay has no terminal to answer, so the query is dropped before feeding.
_PRIVATE_DSR = re.compile(r"\x1b\[\?[0-9;]*n")


def screen_lines(text: str, rows: int, cols: int) -> List[str]:
    """``text`` rendered on a ``cols`` x ``rows`` screen, one string a row."""
    screen = pyte.Screen(max(20, int(cols)), max(5, int(rows)))
    pyte.Stream(screen).feed(_PRIVATE_DSR.sub("", text))
    return [line.rstrip() for line in screen.display]


# Rendered screens by capture state. The Board polls every few seconds and a
# render is ~85 ms of GIL-bound pyte work, so an idle capture is rendered once
# and served from memory until the file's size or mtime moves. Sized for the
# live sessions of one box.
_SCREEN_CACHE_MAX = 64
_SLOW_RENDER_S = 0.2
_screens: "OrderedDict[Tuple[str, int, int], Tuple[Tuple[int, int], List[str]]]" = OrderedDict()
# One render at a time: renders are CPU-bound and hold the GIL, so running
# them side by side only slows each. The file read stays outside the lock, so
# a stalled read holds its own thread and never other sessions' renders.
_render_lock = threading.Lock()


def _cached_screen(key: Tuple[str, int, int], stamp: Tuple[int, int]) -> Optional[List[str]]:
    hit = _screens.get(key)
    if hit is None or hit[0] != stamp:
        return None
    _screens.move_to_end(key)
    return list(hit[1])


def read_screen(path: Path, rows: int, cols: int) -> Optional[List[str]]:
    """The capture's tail rendered on a ``cols`` x ``rows`` screen, or ``None``
    when it can't be read. Unchanged since the last call (same size and mtime)
    it comes from memory; the caller gets its own copy of the lines."""
    key = (str(path), rows, cols)
    try:
        st = os.stat(path)
    except OSError:
        text = read_capture_tail(path)
        return None if text is None else screen_lines(text, rows, cols)
    stamp = (st.st_size, st.st_mtime_ns)
    cached = _cached_screen(key, stamp)
    if cached is not None:
        return cached
    text = read_capture_tail(path)
    if text is None:
        return None
    with _render_lock:
        # Another poll may have rendered this state while this one read.
        cached = _cached_screen(key, stamp)
        if cached is not None:
            return cached
        started = time.perf_counter()
        lines = screen_lines(text, rows, cols)
        took = time.perf_counter() - started
        _screens[key] = (stamp, lines)
        while len(_screens) > _SCREEN_CACHE_MAX:
            _screens.popitem(last=False)
    if took >= _SLOW_RENDER_S:
        logger.info(
            "⏱️ screen render %s took %.2fs (%d bytes, %dx%d)",
            Path(path).stem[:8], took, len(text), cols, rows,
        )
    return list(lines)
