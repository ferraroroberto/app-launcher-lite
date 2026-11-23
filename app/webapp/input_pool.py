"""The thread pool that delivers messages to sessions, and the send's stage breadcrumb.

``POST /api/claude-code/sessions/{sid}/input`` holds a thread for as long as
the session takes the text: the readiness wait for a just-spawned agent, then
the session-host's own write, ingest and submit-confirm windows, a few seconds
at worst. On the loop's default executor that thread was shared with every
route that has no pool of its own, so with a phone polling ten things at once
a send queued behind reads before any of that started. Here the send's
blocking steps (the delivery call, a refused send's 500 classification and the
audit write) run on :data:`INPUT_POOL`, which nothing else uses.

Each send writes one INFO line to ``webapp/slow-requests.log`` naming where
its time went: the phone's terminal-token step (reported by the page in two
headers), the wait for an input thread, the readiness read, the session-host
call and, inside that call, the ingest and submit-confirm windows read off the
host's own verdict. A stage that could not be measured reads ``unknown``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Mapping, Optional

from src.request_timing import TimedExecutor
from src.webapp_settings import performance

slow_logger = logging.getLogger("launcher.slowreq")

INPUT_POOL = TimedExecutor(
    "input", max_workers=performance.default("pool_input"), ceiling=performance.maximum("pool_input")
)

# The page's report of its terminal-token step: how long it took and where the
# token came from (``cached``, ``passkey`` for a fresh ceremony, ``none`` when
# the gate does not apply).
TOKEN_MS_HEADER = "x-send-token-ms"
TOKEN_SOURCE_HEADER = "x-send-token-source"
_TOKEN_SOURCES = frozenset({"cached", "passkey", "none"})


def resize(limit: int) -> None:
    """Apply the Settings > Performance size, live."""
    INPUT_POOL.resize(limit)


async def run_input(stages: Dict[str, Any], fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """``fn(*args, **kwargs)`` on :data:`INPUT_POOL`, adding its thread wait to ``stages["queue_ms"]``."""
    loop = asyncio.get_running_loop()
    queued = time.monotonic()

    def call() -> Any:
        stages["queue_ms"] = stages.get("queue_ms", 0.0) + (time.monotonic() - queued) * 1000
        return fn(*args, **kwargs)

    return await loop.run_in_executor(INPUT_POOL, call)


def client_token(headers: Mapping[str, str]) -> Dict[str, Any]:
    """The page's token step from its headers; a missing or malformed value is left out."""
    out: Dict[str, Any] = {}
    source = headers.get(TOKEN_SOURCE_HEADER, "")
    if source in _TOKEN_SOURCES:
        out["token_source"] = source
    raw = headers.get(TOKEN_MS_HEADER, "")
    try:
        ms = float(raw)
    except ValueError:
        return out
    if 0 <= ms < 24 * 3600 * 1000:
        out["token_ms"] = ms
    return out


def host_windows(verdict: Mapping[str, Any]) -> Dict[str, Optional[float]]:
    """The ingest and submit-confirm windows inside the session-host call.

    ``waited_ms`` is the host's wait from the write until the payload echoed
    and settled (0 for a short payload, which is not verified); the confirm
    window runs from there to the verdict, ``submitted_at - written_at``
    minus that wait. Either is ``None`` when the verdict lacks the fields.
    """
    waited = verdict.get("waited_ms")
    ingest = float(waited) if isinstance(waited, (int, float)) else None
    written, submitted = verdict.get("written_at"), verdict.get("submitted_at")
    confirm: Optional[float] = None
    if ingest is not None and isinstance(written, (int, float)) and isinstance(submitted, (int, float)):
        confirm = max(0.0, (submitted - written) * 1000 - ingest)
    return {"ingest_ms": ingest, "confirm_ms": confirm}


def _ms(value: Any) -> str:
    return f"{value:.0f}ms" if isinstance(value, (int, float)) else "unknown"


def log_send(sid: str, stages: Mapping[str, Any], total_s: float, outcome: str) -> None:
    """One INFO breadcrumb naming each stage of a send and its outcome."""
    slow_logger.info(
        "📨 send %s: %.1fs, token %s %s, queue %s, ready %s, host %s (ingest %s, confirm %s); %s",
        sid[:8], total_s,
        stages.get("token_source", "unknown"), _ms(stages.get("token_ms")),
        _ms(stages.get("queue_ms")), _ms(stages.get("ready_ms")), _ms(stages.get("host_ms")),
        _ms(stages.get("ingest_ms")), _ms(stages.get("confirm_ms")), outcome,
    )
