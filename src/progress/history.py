"""``GET /api/progress``: the git and GitLab totals, one answer.

Cache first and no read waits on a recalculation: the answer is the saved
totals (:func:`src.progress.totals.snapshot`, no ``git`` or ``glab`` call).
``refresh`` starts a recalculation in the background and the answer is still
the cache, with ``refreshing`` true while it runs. The two sources fail
separately: lines (local git) and merge requests / issues (GitLab) each carry
their own state and age, and a figure never measured is ``None`` / ``unknown``,
never zero. ``refresh`` is the running or last finished recalculation's progress
(projects done of total, and one result per project: ``ok``, ``skipped`` or
``failed``), or ``None`` before the first one since the server started. A project with no GitLab remote has ``None`` for the GitLab
figures and is listed under ``gitlab.skipped``.

Days are the local calendar day for both sources.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.progress import buckets, totals

logger = logging.getLogger(__name__)

OK = totals.OK
STALE = totals.STALE
UNKNOWN = totals.UNKNOWN
UNAVAILABLE = "unavailable"

_METRICS = {"git": totals.GIT_METRICS, "gitlab": totals.FORGE_METRICS}
_NO_FORGE_REPOS = "No project has a GitLab remote."


def _age(stamp: Optional[str], now: datetime) -> Optional[float]:
    if not stamp:
        return None
    try:
        return round(max((now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds(), 0.0), 1)
    except ValueError:
        return None


def _sum(rows: Iterable[Dict[str, Any]], metrics: Sequence[str]) -> Dict[str, int]:
    out = dict.fromkeys(metrics, 0)
    for row in rows:
        for metric in metrics:
            out[metric] += int(row.get(metric, 0))
    return out


def _known(snap: Dict[str, Any], source: str) -> Dict[str, Dict[str, Any]]:
    """The projects whose ``source`` has been read at least once (ok or stale)."""
    return {
        name: repo[source] for name, repo in snap["repos"].items()
        if repo[source]["state"] in (OK, STALE)
    }


def _state(snap: Dict[str, Any], source: str, known: Dict[str, Any]) -> tuple[str, Optional[str]]:
    others = [r[source] for r in snap["repos"].values() if r[source]["state"] != totals.SKIPPED]
    if not known:
        if snap["error"]:
            return UNAVAILABLE, snap["error"]
        if source == "gitlab" and snap["repos"] and not others:
            return UNKNOWN, _NO_FORGE_REPOS
        label = "The local git history has" if source == "git" else "The GitLab totals have"
        return UNKNOWN, f"{label} not been read yet."
    if snap["error"] or any(r["state"] == STALE for r in known.values()):
        return STALE, snap["error"] or "The last read failed for some projects; showing what was kept."
    return OK, None


def _half(snap: Dict[str, Any], source: str, now: datetime, started: bool) -> Dict[str, Any]:
    known = _known(snap, source)
    state, reason = _state(snap, source, known)
    stamp = snap["refreshed_at"][source]
    half: Dict[str, Any] = {
        "state": state,
        "reason": reason,
        "refreshed_at": stamp,
        "age_seconds": _age(stamp, now),
        "refreshing": bool(snap["in_flight"]) or started,
        "unknown_repos": sorted(
            n for n, r in snap["repos"].items() if r[source]["state"] == UNKNOWN
        ),
    }
    if source == "gitlab":
        half["skipped"] = list(snap["skipped"])
        half["truncated"] = sorted(n for n, r in known.items() if r["truncated"])
    return half


def _bucket_values(
    snap: Dict[str, Any], source: str, days: Sequence[str]
) -> Optional[Dict[str, int]]:
    known = _known(snap, source)
    if not known:
        return None
    wanted = set(days)
    return _sum(
        (v for r in known.values() for d, v in r["days"].items() if d in wanted), _METRICS[source]
    )


def _rows(snap: Dict[str, Any], first: str, last: str) -> List[Dict[str, Any]]:
    """One row per project; a metric of a source that was not read is ``None``."""
    rows = []
    for name, repo in sorted(snap["repos"].items()):
        row: Dict[str, Any] = {
            "repo": name,
            "git_state": repo["git"]["state"],
            "gitlab_state": repo["gitlab"]["state"],
        }
        for source in totals.SOURCES:
            view = repo[source]
            if view["state"] in (OK, STALE):
                inside = (v for d, v in view["days"].items() if first <= d <= last)
                row.update(_sum(inside, _METRICS[source]))
            else:
                row.update(dict.fromkeys(_METRICS[source]))
        rows.append(row)
    return rows


def _unreadable(exc: Exception) -> Dict[str, Any]:
    return {
        "error": f"{type(exc).__name__}: {exc}", "repos": {}, "skipped": [], "in_flight": False,
        "refreshed_at": dict.fromkeys(totals.SOURCES),
    }


def read(
    *,
    bucket: str,
    first: date,
    last: date,
    projects_dir: str,
    ignore: Sequence[str] = (),
    host: str = "",
    refresh: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The combined answer for one range. Never raises; runs no ``git`` or ``glab`` unless ``refresh``."""
    now = now or datetime.now(timezone.utc)
    start, stop = first.isoformat(), last.isoformat()
    plan = buckets.bucket_plan(first, last, bucket)
    started = False
    if refresh:
        try:
            started = totals.start_refresh(projects_dir, ignore, host)
        except Exception:  # the cached answer must still be given
            logger.exception("⚠️ progress: could not start the refresh")
    try:
        snap = totals.snapshot()
    except Exception as exc:  # an unreadable store must still answer, as unavailable
        logger.exception("⚠️ progress: totals unreadable")
        snap = _unreadable(exc)
    git, gitlab = (_half(snap, s, now, started) for s in totals.SOURCES)
    return {
        "bucket": bucket,
        "from": start,
        "to": stop,
        "generated_at": now.isoformat(timespec="seconds"),
        "refreshing": git["refreshing"],
        "refresh": snap.get("refresh"),
        "git": git,
        "gitlab": gitlab,
        "buckets": [
            {
                "start": b["start"],
                "end": b["end"],
                "git": _bucket_values(snap, "git", b["days"]),
                "gitlab": _bucket_values(snap, "gitlab", b["days"]),
            }
            for b in plan
        ],
        "repos": _rows(snap, start, stop),
    }
