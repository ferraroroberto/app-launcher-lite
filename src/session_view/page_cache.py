"""Parsed transcript pages, kept while their file is unchanged.

The chat's opening read and every reopen of an unchanged session ask for the
same page of the same bytes, so :func:`src.session_view.transcript.transcript_page`
parses it once per ``(path, size, mtime_ns, before, limit, flavor)`` and
answers repeats from here. Entries are copies on the way in and out, so a
caller's edits (the automatic-turn marks) never reach the cache.
"""

from __future__ import annotations

import copy
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

# The newest page and a few older ones for each of several open chats; the
# owner tunes it as ``transcript_page_cache_size`` in webapp_config.json.
DEFAULT_PAGE_CACHE_SIZE = 32

_PAGES: "OrderedDict[Tuple[Any, ...], Dict[str, Any]]" = OrderedDict()
_LOCK = threading.Lock()


def cached_page(key: Tuple[Any, ...], cache_size: int) -> Optional[Dict[str, Any]]:
    """A copy of the page stored under ``key``, or ``None`` (always ``None``
    when ``cache_size`` is 0, which turns the cache off)."""
    if cache_size <= 0:
        return None
    with _LOCK:
        hit = _PAGES.get(key)
        if hit is None:
            return None
        _PAGES.move_to_end(key)
    return copy.deepcopy(hit)


def store_page(key: Tuple[Any, ...], page: Dict[str, Any], cache_size: int) -> None:
    """Keep a copy of ``page`` under ``key``, dropping the least recently
    used pages past ``cache_size``."""
    if cache_size <= 0:
        return
    kept = copy.deepcopy(page)
    with _LOCK:
        _PAGES[key] = kept
        while len(_PAGES) > cache_size:
            _PAGES.popitem(last=False)
