"""Daily totals of what shipped, per project, read once and cached on disk.

Two sources, each read and aged on its own:

* **git**: lines added and removed per local calendar day, from the local
  ``git log --numstat`` of each project folder's default branch.
* **gitlab**: merge requests merged, issues opened and issues closed per local
  day, from the project's GitLab remote through ``glab``.

A project folder with no GitLab remote is skipped for the GitLab source and
listed in ``skipped``, never counted as zero; its lines still count.

Caching contract:

* :func:`snapshot` is a pure read of ``webapp/progress-totals.json`` and never
  runs ``git`` or ``glab``, so the last result is served at once, however old.
* :func:`refresh` runs only when asked, never on a poll. A source with no
  cursor reads a backfill of :data:`BACKFILL_DAYS`; afterwards each refresh
  re-reads a window that starts :data:`OVERLAP_DAYS` before the stored cursor
  and replaces those days, so a commit pushed late is still counted and a
  repeat read never double counts.
* A failed read keeps the project's previous days for that source and reports
  ``stale``; one never read reports ``unknown``, never zero. The store is
  written after every project, so an interrupted backfill resumes where it
  stopped.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src import runtime_files
from src._json_io import atomic_write_json
from src.clients import gitlab as gitlab_client
from src.git_utils import resolve_default_ref, run_git
from src.scanner import scan_project_dirs

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Module-level so tests redirect it at a tmp_path.
TOTALS_FILE = PROJECT_ROOT / "webapp" / "progress-totals.json"

GIT_METRICS = ("lines_added", "lines_removed")
FORGE_METRICS = ("mrs_merged", "issues_opened", "issues_closed")
METRICS = GIT_METRICS + FORGE_METRICS
SOURCES = ("git", "gitlab")

# A commit keeps the date it was made, so one pushed days later lands behind
# the cursor; a week back catches the usual lag.
OVERLAP_DAYS = 7
# The sheet's longest range, so an older day could never be shown anyway; a
# first read goes back this far and no further.
BACKFILL_DAYS = 400
# A year of numstat on a large repo is slow; it still has to finish.
_GIT_LOG_TIMEOUT_SECONDS = 120.0

OK = "ok"
STALE = "stale"
UNKNOWN = "unknown"
SKIPPED = "skipped"

_STORE_VERSION = 1
NO_REMOTE = "no GitLab remote"
_REMOTE_URL = re.compile(
    r"^(?:(?:https?|ssh)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/|[^@\s]+@(?P<scp>[^:/\s]+):)"
    r"(?P<path>[^\s]+?)(?:\.git)?/?$"
)

# One refresh at a time; the flag is what a reader sees as ``in_flight``.
_run_lock = threading.Lock()
_in_flight = False

# (checkout, since) -> [(commit stamp, lines added, lines removed)]
GitReader = Callable[[Path, datetime], List[Tuple[str, int, int]]]
# (project path, since, host) -> gitlab_client.project_history's answer
ForgeReader = Callable[[str, datetime, str], Dict[str, Any]]


def _empty_store() -> Dict[str, Any]:
    return {
        "version": _STORE_VERSION, "git_refreshed_at": None, "gitlab_refreshed_at": None,
        "error": None, "repos": {}, "skipped": [],
    }


def _load(path: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    """``(store, problem)``; an unreadable file is the problem text, with an empty store."""
    if not path.is_file():
        return _empty_store(), None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ progress totals: cannot read %s: %s", path, exc)
        return _empty_store(), f"The saved totals could not be read: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        logger.warning("⚠️ progress totals: %s has an unknown shape", path)
        return _empty_store(), "The saved totals have an unknown shape."
    store = _empty_store()
    store.update({k: data[k] for k in ("git_refreshed_at", "gitlab_refreshed_at", "error") if k in data})
    store["repos"] = {n: e for n, e in data["repos"].items() if isinstance(e, dict)}
    store["skipped"] = [s for s in (data.get("skipped") or []) if isinstance(s, dict)]
    return store, None


def _state(entry: Dict[str, Any]) -> str:
    if entry.get("skipped"):
        return SKIPPED
    if not entry.get("cursor"):
        return UNKNOWN
    return STALE if entry.get("error") else OK


def _view(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "state": _state(entry),
        "read_at": entry.get("read_at"),
        "error": entry.get("error"),
        "reason": entry.get("skipped"),
        "truncated": list(entry.get("truncated") or []),
        "days": entry.get("days") or {},
    }


def snapshot(path: Optional[Path] = None) -> Dict[str, Any]:
    """The saved totals with each project's state per source; never runs git or glab."""
    store, problem = _load(path or TOTALS_FILE)
    repos = {
        name: {source: _view(entry.get(source) or {}) for source in SOURCES}
        for name, entry in sorted(store["repos"].items())
    }
    return {
        "refreshed_at": {s: store[f"{s}_refreshed_at"] for s in SOURCES},
        "in_flight": _in_flight,
        "error": problem or store["error"],
        "repos": repos,
        "skipped": list(store["skipped"]),
    }


def parse_remote(url: str, host: str = "") -> Optional[str]:
    """The ``group/sub/project`` path of a remote URL on the GitLab host, else None.

    ``host`` is the configured self-hosted instance; empty means gitlab.com.
    """
    found = _REMOTE_URL.match((url or "").strip())
    if not found:
        return None
    seen = (found.group("host") or found.group("scp") or "").lower()
    wanted = re.sub(r"^https?://", "", host.strip()).strip("/").lower() or "gitlab.com"
    return found.group("path").strip("/") if seen == wanted else None


def _gitlab_path(checkout: Path, host: str) -> Optional[str]:
    """The project path of the checkout's GitLab remote; a remote named ``gitlab`` or ``origin`` wins."""
    listing = run_git(checkout, ["config", "--get-regexp", r"^remote\..+\.url$"]) or ""
    found: Dict[str, str] = {}
    for line in listing.splitlines():
        key, _, url = line.partition(" ")
        path = parse_remote(url, host)
        if path:
            found[key[len("remote."):-len(".url")]] = path
    for name in ("gitlab", "origin"):
        if name in found:
            return found[name]
    return next(iter(found.values()), None)


def resolve_projects(
    projects_dir: str, ignore: Sequence[str], host: str
) -> List[Dict[str, Any]]:
    """Every git project folder under ``projects_dir``: ``{name, checkout, gitlab}``.

    ``gitlab`` is the project's GitLab path or None. A folder whose ``.git`` is a
    file is a linked worktree of another folder's repository and is left out, or
    its lines would be counted twice. Blocking (one ``git`` per project).
    """
    projects = []
    for row in scan_project_dirs(Path(projects_dir), list(ignore)):
        if not (row.project_dir / ".git").is_dir():
            continue
        projects.append({
            "name": row.name, "checkout": row.project_dir, "gitlab": _gitlab_path(row.project_dir, host),
        })
    return projects


def _local_day(stamp: str, tz: Optional[tzinfo]) -> str:
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment.astimezone(tz).date().isoformat()


def _window(
    cursor: Optional[str], now: datetime, tz: Optional[tzinfo]
) -> Tuple[datetime, str]:
    """``(since, first day)`` of the read: the re-read window, or the backfill with no cursor."""
    anchor = datetime.fromisoformat(cursor.replace("Z", "+00:00")) if cursor else now
    days = OVERLAP_DAYS if cursor else BACKFILL_DAYS
    first = anchor.astimezone(tz).date() - timedelta(days=days)
    return gitlab_client.local_day_window(first, tz)[0], first.isoformat()


def _aggregate(
    events: Dict[str, Any], first_day: str, tz: Optional[tzinfo]
) -> Dict[str, Dict[str, int]]:
    """Group a project's raw events into per-day totals, dropping days before ``first_day``."""
    days: Dict[str, Dict[str, int]] = defaultdict(dict)
    for stamp, added, removed in events.get("commits") or []:
        row = days[_local_day(stamp, tz)]
        row["lines_added"] = row.get("lines_added", 0) + added
        row["lines_removed"] = row.get("lines_removed", 0) + removed
    for key, metric in (("merged", "mrs_merged"), ("opened", "issues_opened"), ("closed", "issues_closed")):
        for stamp in events.get(key) or []:
            row = days[_local_day(stamp, tz)]
            row[metric] = row.get(metric, 0) + 1
    return {d: dict(row) for d, row in sorted(days.items()) if d >= first_day}


def parse_numstat(text: str) -> List[Tuple[str, int, int]]:
    """``(commit stamp, added, removed)`` per commit of ``git log --numstat --format=@@%cI``.

    A binary file's ``-`` counts add nothing; a commit that changed no lines
    (a merge) still appears, with zeros.
    """
    commits: List[List[Any]] = []
    for line in text.splitlines():
        if line.startswith("@@"):
            commits.append([line[2:].strip(), 0, 0])
        elif commits and line.strip():
            added, _, rest = line.partition("\t")
            removed = rest.partition("\t")[0]
            if added.isdigit() and removed.isdigit():
                commits[-1][1] += int(added)
                commits[-1][2] += int(removed)
    return [(stamp, added, removed) for stamp, added, removed in commits]


def read_git(checkout: Path, since: datetime) -> List[Tuple[str, int, int]]:
    """The commits of the default branch since ``since``; raises when git could not answer."""
    ref = resolve_default_ref(
        checkout, fallback_refs=("origin/main", "origin/master", "main", "master")
    ) or "HEAD"
    out = run_git(
        checkout,
        ["log", ref, f"--since={since.isoformat(timespec='seconds')}", "--numstat", "--format=@@%cI"],
        timeout=_GIT_LOG_TIMEOUT_SECONDS,
    )
    if out is None:
        raise RuntimeError(f"git log {ref} failed")
    return parse_numstat(out)


def _fold(
    entry: Dict[str, Any], events: Dict[str, Any], first_day: str, started: str, tz: Optional[tzinfo]
) -> None:
    """Replace ``entry``'s days from ``first_day`` on with the fresh read; touches nothing before it."""
    kept = {d: row for d, row in (entry.get("days") or {}).items() if d < first_day}
    entry.update(
        days={**kept, **_aggregate(events, first_day, tz)}, cursor=started, read_at=started,
        error=None, skipped=None, truncated=list(events.get("truncated") or []),
    )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def refresh(
    projects_dir: str,
    ignore: Sequence[str] = (),
    host: str = "",
    *,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
    tz: Optional[tzinfo] = None,
    git_reader: Optional[GitReader] = None,
    forge_reader: Optional[ForgeReader] = None,
) -> Dict[str, Any]:
    """Read every project now and replace the saved totals; returns :func:`snapshot`.

    Blocking and slow: call it on the owner's demand only, off the event loop.
    A refresh already running makes this a plain :func:`snapshot`.
    """
    global _in_flight
    target = path or TOTALS_FILE
    if not _run_lock.acquire(blocking=False):
        return snapshot(target)
    _in_flight = True
    try:
        _refresh_locked(
            projects_dir, ignore, host, target, now or datetime.now(timezone.utc), tz,
            git_reader or read_git, forge_reader or _read_forge,
        )
    finally:
        _in_flight = False
        _run_lock.release()
    return snapshot(target)


def _read_forge(path: str, since: datetime, host: str) -> Dict[str, Any]:
    return gitlab_client.project_history(path, since, host)


def _read_source(
    entry: Dict[str, Any], source: str, read: Callable[[datetime], Dict[str, Any]],
    now: datetime, started: str, tz: Optional[tzinfo], name: str,
) -> bool:
    """One project's one source into ``entry``; False (the old days kept, ``error`` set) when it failed."""
    since, first_day = _window(entry.get("cursor"), now, tz)
    try:
        events = read(since)
    except Exception as exc:  # one project's failure must not lose the others
        level = logger.warning if isinstance(exc, RuntimeError) else logger.exception
        level("⚠️ progress totals: %s %s failed: %s", name, source, exc)
        entry["error"] = f"{type(exc).__name__}: {exc}"
        return False
    _fold(entry, events, first_day, started, tz)
    return True


def _refresh_locked(
    projects_dir: str, ignore: Sequence[str], host: str, path: Path, now: datetime,
    tz: Optional[tzinfo], git_reader: GitReader, forge_reader: ForgeReader,
) -> None:
    started = _stamp(now)
    store, _ = _load(path)
    projects = resolve_projects(projects_dir, ignore, host)
    store["error"] = None
    store["skipped"] = [{"repo": p["name"], "reason": NO_REMOTE} for p in projects if not p["gitlab"]]
    store["repos"] = {n: e for n, e in store["repos"].items() if n in {p["name"] for p in projects}}
    ok = dict.fromkeys(SOURCES, 0)
    failed = dict.fromkeys(SOURCES, 0)
    for project in projects:
        name = project["name"]
        entry = store["repos"].setdefault(name, {})
        git_entry = entry.setdefault("git", {"days": {}})
        if _read_source(
            git_entry, "git", lambda since, p=project: {"commits": git_reader(p["checkout"], since)},
            now, started, tz, name,
        ):
            ok["git"] += 1
        else:
            failed["git"] += 1
        forge_entry = entry.setdefault("gitlab", {"days": {}})
        if not project["gitlab"]:
            forge_entry.clear()
            forge_entry.update(skipped=NO_REMOTE, days={})
        elif _read_source(
            forge_entry, "gitlab", lambda since, p=project: forge_reader(p["gitlab"], since, host),
            now, started, tz, name,
        ):
            ok["gitlab"] += 1
        else:
            failed["gitlab"] += 1
        _save(path, store)
    for source in SOURCES:
        if ok[source] or not failed[source]:
            store[f"{source}_refreshed_at"] = started
    _save(path, store)
    logger.info(
        "✅ progress totals: git %d read %d failed, gitlab %d read %d failed, %d without a GitLab remote",
        ok["git"], failed["git"], ok["gitlab"], failed["gitlab"], len(store["skipped"]),
    )


def _save(path: Path, store: Dict[str, Any]) -> None:
    runtime_files.make_dirs(path.parent)
    atomic_write_json(path, store)


def start_refresh(projects_dir: str, ignore: Sequence[str], host: str, **kwargs: Any) -> bool:
    """Refresh in a background thread unless one is running; True when it started."""
    if _in_flight:
        return False
    threading.Thread(
        target=refresh, args=(projects_dir, ignore, host), kwargs=kwargs,
        name="progress-totals-refresh", daemon=True,
    ).start()
    return True
