"""The read-only health line under Settings > Performance.

It answers "is this box coping?" next to the limits that tune it: CPU and free
memory (the header reading's cached sample), requests in flight (the timing
middleware's counters), coding-agent processes, and the live lane count against
the chief's worker cap. Asking starts no scan and signals no process.

**Unknown is its own state.** A figure that could not be established is
``None`` with the reason under ``unknown``, never ``0``: a box with no sample
yet or an unreadable lane file must not read as an idle one.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

import psutil

from src import request_timing, system_load
from src.board import state as board_state
from src.chief_session_end import read_registry
from src.health_snapshot import AGENT_PROCESS_NAMES

logger = logging.getLogger(__name__)

#: The window the in-flight peak covers, in seconds.
WINDOW_S = 300.0

#: A process scan is the one costly read here, so it is reused for this long.
AGENT_COUNT_TTL_S = 5.0

_agent_cache: Optional[Tuple[float, Optional[int]]] = None


def agent_processes(now: Optional[float] = None) -> Optional[int]:
    """How many coding-agent processes run, or ``None`` when the process list cannot be read."""
    global _agent_cache
    moment = time.monotonic() if now is None else now
    if _agent_cache is not None and moment - _agent_cache[0] < AGENT_COUNT_TTL_S:
        return _agent_cache[1]
    try:
        count: Optional[int] = sum(
            1 for proc in psutil.process_iter(["name"])
            if (proc.info.get("name") or "").lower() in AGENT_PROCESS_NAMES
        )
    except (psutil.Error, OSError) as exc:
        logger.warning(f"⚠️ agent process count unknown: {type(exc).__name__}")
        count = None
    _agent_cache = (moment, count)
    return count


def lane_count(managed_file: Path, sessions_file: Path) -> Tuple[Optional[int], str]:
    """Chief-managed lanes whose session still has a live state row, and why it is unknown."""
    managed = read_registry(managed_file)
    if managed is None:
        return None, "the lane file could not be read"
    state = board_state.read_sessions_state(sessions_file)
    if not state["available"]:
        return None, "the session state file could not be read"
    live: Set[str] = {
        str(row.get("launcher_session_id")) for row in state["rows"].values()
        if row.get("launcher_session_id")
    }
    return sum(1 for sid in managed if sid in live), ""


def read(cfg: Any) -> Dict[str, Any]:
    """The health figures; each unknown one is ``None`` and named under ``unknown``."""
    unknown: Dict[str, str] = {}
    load = system_load.read()
    cpu = load.get("cpu_percent")
    memory = load.get("memory")
    if cpu is None or not memory or not memory.get("total_bytes"):
        reason = load.get("reason") or "unknown"
        unknown["cpu"] = unknown["memory"] = f"the PC load sample is not ready ({reason})"
        cpu, free_gb, total_gb = None, None, None
    else:
        free_gb = round((memory["total_bytes"] - memory["used_bytes"]) / 2**30, 1)
        total_gb = round(memory["total_bytes"] / 2**30, 1)
    stats = request_timing.STATS.snapshot(WINDOW_S)
    agents = agent_processes()
    if agents is None:
        unknown["agents"] = "the process list could not be read"
    lanes, why = lane_count(Path(cfg.chief_managed_file), Path(cfg.sessions_state_file))
    if lanes is None:
        unknown["lanes"] = why
    return {
        "cpu_percent": cpu,
        "memory_free_gb": free_gb,
        "memory_total_gb": total_gb,
        "inflight": stats["inflight"],
        "max_inflight": stats["max_inflight"],
        "window_s": WINDOW_S,
        "agent_processes": agents,
        "lanes": lanes,
        "worker_cap": cfg.chief_worker_cap,
        "unknown": unknown,
    }
