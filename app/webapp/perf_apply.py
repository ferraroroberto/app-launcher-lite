"""Applies the Settings > Performance limits that a running process holds.

Poll spacings, the caches and the request budgets are read off the config on
each use, so a save reaches them by itself. The thread pools are objects built
at import time, so they are resized here: once when the app starts and again
after every config save, with no restart of the webapp or the session-host.
"""

from __future__ import annotations

import logging
from typing import Any

from app.webapp import hot_poll
from app.webapp.routers import claude_code
from src import request_timing

logger = logging.getLogger(__name__)


def apply(cfg: Any) -> None:
    """Resize each pool to ``cfg``'s limits (``pool_sends`` 0 = automatic)."""
    hot_poll.resize_pools(cfg.pool_hot_poll, cfg.pool_session_reads)
    claude_code.resize_git_status_pool(cfg.pool_git_status)
    request_timing.resize_default_executor(cfg.pool_sends)
    logger.info(
        "ℹ️ performance limits applied: hot-poll=%s session-reads=%s git-status=%s sends=%s",
        cfg.pool_hot_poll, cfg.pool_session_reads, cfg.pool_git_status,
        cfg.pool_sends or "auto",
    )
