"""The chief's automatic self-compaction gate: the context reading, the
threshold from Settings, and the refusals that keep it from compacting on a
guess or over a stale handover log or plan.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Fallback when app-launcher's Settings value can't be read.
DEFAULT_COMPACT_THRESHOLD = 30
# The pre-compact refresh must have just run: handover log and plan both
# written within this window, or the compaction would drop state they lack.
COMPACT_FRESH_SECONDS = 600
# The Stop hook (hooks/chief_waiter_stop.py) nudges at most once per window,
# and a queued self-compact silences it for the same window: the compaction
# runs when the turn ends, while the percentage is still over the threshold.
COMPACT_NUDGE_SECONDS = 600
COMPACT_QUEUED_MARKER = "chief-compact-queued"
DEFAULT_COMPACT_FOCUS = (
    "Keep the in-flight lanes (sid, repo#N, state), steers still awaiting "
    "verification, the waiting-on-Roberto items, and anything parked with its "
    "reason. The handover log and the plan hold the rest."
)


def format_context_line(sid: str, info: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    """`(line, percent)`; a null or unavailable reading is `unknown`, never 0."""
    percent = info.get("percent") if info.get("available") else None
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        reason = info.get("reason") or ("percent not reported" if info.get("available") else "unavailable")
        return f"CONTEXT sid={sid[:8]} percent=unknown reason={reason}", None
    return f"CONTEXT sid={sid[:8]} percent={int(percent)}", int(percent)


def compact_status_line(percent: Optional[int], threshold: Optional[int], reason: str = "percent not reported") -> str:
    """The one-line verdict the inbox and the Stop hook read.

    `COMPACT_DUE=unknown` is its own state: an unreadable percentage is never
    reported as "not due".
    """
    if threshold is None:
        return "COMPACT_OFF"
    if percent is None:
        return f"COMPACT_DUE=unknown reason={reason}"
    verdict = "COMPACT_DUE" if percent >= threshold else "COMPACT_NOT_DUE"
    return f"{verdict} percent={percent} threshold={threshold}"


def resolve_compact_threshold(
    settings: Optional[Dict[str, Any]], override: Optional[int], unread_reason: str = "launcher unreachable",
) -> Tuple[Optional[int], str]:
    """`(threshold, note)` -- `None` means auto-compact is off (Settings value 0).

    An explicit `--threshold` wins. Otherwise the launcher's
    `auto_compact_threshold`; an unreadable or missing value falls back to the
    default and the note says why, never passing it off as configured.
    """
    if override is not None:
        return override, f"threshold {override} (--threshold)"
    if settings is None:
        return DEFAULT_COMPACT_THRESHOLD, f"threshold {DEFAULT_COMPACT_THRESHOLD}, default: {unread_reason}"
    value = settings.get("auto_compact_threshold")
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_COMPACT_THRESHOLD, f"threshold {DEFAULT_COMPACT_THRESHOLD}, default: setting missing"
    if value == 0:
        return None, "auto-compact is off in Settings (0)"
    return value, f"threshold {value} (Settings)"


def file_age_seconds(path: Path, now: Optional[float] = None) -> Optional[float]:
    try:
        return (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return None


def self_compact_refusal(
    percent: Optional[int], threshold: int, handover_age: Optional[float],
    plan_age: Optional[float], fresh_seconds: float = COMPACT_FRESH_SECONDS,
) -> Optional[str]:
    """Why the *automatic* trigger must not compact now, or `None` to proceed.

    Applies only to `self-compact`. An operator-typed `/compact` never passes
    through here and is never gated.
    """
    if percent is None:
        return "context percentage unknown; not compacting on a guess"
    if percent < threshold:
        return f"below threshold ({percent}% < {threshold}%)"
    for name, age in (("handover log", handover_age), ("plan", plan_age)):
        if age is None:
            return f"{name} missing; refresh it before compacting"
        if age > fresh_seconds:
            return f"{name} is stale (written {int(age // 60)}m ago, limit {int(fresh_seconds // 60)}m); refresh it first"
    return None


# ---- chief-dispatched lanes (hooks/lane_compact holds the turn end) ---------

# Both folders are read by hooks/lane_compact under its own spelling (tree boundary).
LANE_HANDOFF_DIRNAME = "lane-handoffs"
LANE_QUEUED_DIRNAME = "lane-compact-queued"


def lane_compact_focus(handoff: str) -> str:
    return (f"Keep the issue, branch and worktree, and the next step. The lane handoff at {handoff} "
            "holds the rest and is handed back after this compaction.")


def lane_compact_refusal(
    handoff_age: Optional[float], gate_running: Optional[bool], fresh_seconds: float = COMPACT_FRESH_SECONDS,
) -> Optional[str]:
    """Why a lane must not compact now, or `None` to proceed. An unknown gate state is a running gate."""
    if gate_running is None:
        return "gate state unknown; not compacting on a guess"
    if gate_running:
        return "a gate is running in this tree; finish it before compacting"
    if handoff_age is None:
        return "handoff missing; write it before compacting"
    if handoff_age > fresh_seconds:
        return (f"handoff is stale (written {int(handoff_age // 60)}m ago, limit "
                f"{int(fresh_seconds // 60)}m); refresh it first")
    return None
