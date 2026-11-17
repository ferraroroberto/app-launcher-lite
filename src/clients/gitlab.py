"""Group-wide GitLab reads and writes for the Board tab, via the ``glab`` CLI.

The GitLab counterpart of the full app's ``src/clients/github.py``, with the
same public names so the Board's callers read the same either way: open
issues, open merge requests and today's closed issues across every project
under the configured group, behind a module-level cache. **``glab`` is a
subprocess per call and is never invoked on a poll**: :func:`refresh` runs the
reads only when the user asks (or once on a stale Board activation), while
:func:`snapshot` is a pure memory read the 5 s board poll can hit for free.

Every read and write goes through ``glab api`` against the REST v4 API, so
the shapes pinned in the unit fixtures are GitLab's documented JSON rather
than any one ``glab`` subcommand's text output. A non-empty ``host`` rides
``GITLAB_HOST`` (glab's own self-hosted selector).

Errors degrade, never break the board: a failed refresh keeps the previous
snapshot's data and surfaces ``error`` so the UI can badge it. The cache is
process memory, so a restart starts it at ``fetched_at: None`` ("never
fetched"), which is not "fetched, nothing open".
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

_GLAB_TIMEOUT_SECONDS = 20.0
# The command a throwaway instance runs instead of the real ``glab``: a JSON
# list of argv. Unset everywhere real.
GLAB_CMD_ENV = "LAUNCHER_GLAB_CMD"
# GitLab's largest ``per_page``. The reads do not paginate: a full page means
# the group may hold more, and ``refresh`` records that as ``truncated``.
_PAGE = 100


class GlabError(RuntimeError):
    """``glab`` missing, timed out, unauthenticated, or non-zero exit."""


def _glab_argv() -> List[str]:
    """``["glab"]``, or the argv :data:`GLAB_CMD_ENV` names.

    A malformed value is a :class:`GlabError`, never a quiet fall-back to the
    real ``glab``: the instance that set it meant to stay off real GitLab.
    """
    raw = os.environ.get(GLAB_CMD_ENV, "").strip()
    if not raw:
        return ["glab"]
    try:
        argv = json.loads(raw)
    except ValueError:
        argv = None
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
        raise GlabError(f"{GLAB_CMD_ENV} must be a JSON list of strings")
    return argv


def _run_glab(
    args: Sequence[str],
    *,
    host: str = "",
    timeout: float = _GLAB_TIMEOUT_SECONDS,
    stdin_text: Optional[str] = None,
) -> str:
    """Run ``glab <args>`` and return stdout; :class:`GlabError` on any failure."""
    env = {**os.environ, "GITLAB_HOST": host} if host else None
    try:
        proc = subprocess.run(
            [*_glab_argv(), *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GlabError(f"glab {' '.join(args[:2])}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        first = detail[0] if detail else "no output"
        raise GlabError(f"glab {' '.join(args[:2])} exited {proc.returncode}: {first}")
    return proc.stdout


def _api_json(path: str, host: str = "", **kwargs: Any) -> Any:
    out = _run_glab(["api", path], host=host, **kwargs)
    try:
        return json.loads(out or "null")
    except ValueError as exc:
        raise GlabError(f"glab returned unparseable JSON: {exc}") from exc


def _api_list(path: str, host: str = "") -> List[Dict[str, Any]]:
    data = _api_json(path, host)
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _send(method: str, path: str, payload: Dict[str, Any], host: str = "") -> Dict[str, Any]:
    """One write: the JSON body travels on stdin, never the command line."""
    out = _run_glab(
        ["api", path, "--method", method, "--header", "Content-Type: application/json",
         "--input", "-"],
        host=host,
        stdin_text=json.dumps(payload),
    )
    try:
        data = json.loads(out or "{}")
    except ValueError as exc:
        raise GlabError(f"glab returned unparseable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GlabError(f"glab {method} {path} printed no object")
    return data


def _enc(path: str) -> str:
    """A ``group/sub/project`` path as the one URL-encoded segment the API wants."""
    return quote(path, safe="")


def _project(group: str, repo: str) -> str:
    return _enc(f"{group.strip('/')}/{repo}")


def _repo_name(row: Dict[str, Any]) -> str:
    """Short project name from ``references.full`` (``grp/sub/project#N`` or ``!N``)."""
    full = str((row.get("references") or {}).get("full") or "")
    head = full.split("#", 1)[0].split("!", 1)[0]
    return head.split("/")[-1]


def _labels(row: Dict[str, Any]) -> List[str]:
    # GitLab labels are plain strings (gh gave [{name}]).
    return [str(lab) for lab in (row.get("labels") or []) if lab]


def _norm_issue(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "issue",
        "repo": _repo_name(row),
        "number": row.get("iid"),
        "title": row.get("title"),
        "url": row.get("web_url"),
        "updated_at": row.get("updated_at"),
        "closed_at": row.get("closed_at"),
        "labels": _labels(row),
    }


def _norm_pr(row: Dict[str, Any]) -> Dict[str, Any]:
    """A merge request in the Board's PR row shape (``kind`` stays ``pr``)."""
    return {
        "kind": "pr",
        "repo": _repo_name(row),
        "number": row.get("iid"),
        "title": row.get("title"),
        "url": row.get("web_url"),
        "updated_at": row.get("updated_at"),
        "is_draft": bool(row.get("draft") or row.get("work_in_progress")),
        "labels": _labels(row),
    }


# The ``/codebase-audit`` ledger label: bookkeeping, not dispatchable work.
_NON_ACTIONABLE_LABELS = {"audit-meta"}


def _is_actionable(labels: Sequence[str]) -> bool:
    return not any(str(lab).lower() in _NON_ACTIONABLE_LABELS for lab in labels)


def _group_issues(group: str, query: str, host: str) -> List[Dict[str, Any]]:
    return _api_list(f"groups/{_enc(group)}/issues?{query}&per_page={_PAGE}", host)


def search_open_prs(group: str, host: str = "") -> List[Dict[str, Any]]:
    """Open merge requests across the group, most recently updated first."""
    rows = _api_list(
        f"groups/{_enc(group)}/merge_requests?state=opened&order_by=updated_at"
        f"&sort=desc&per_page={_PAGE}",
        host,
    )
    prs = [_norm_pr(r) for r in rows]
    return [p for p in prs if _is_actionable(p["labels"])]


def local_day_window(day: date, tz: Optional[tzinfo] = None) -> Tuple[datetime, datetime]:
    """The first and last second of the local calendar day ``day``, in UTC.

    GitLab stamps in UTC while the owner reads days on this host's clock, so
    the window opens at local midnight and closes one second before the next
    (23 h or 25 h across a DST change). ``tz`` stands in for the host's zone.
    """
    def midnight_utc(d: date) -> datetime:
        return datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc)

    start = midnight_utc(day)
    return start, midnight_utc(day + timedelta(days=1)) - timedelta(seconds=1)


def _stamp(moment: datetime) -> str:
    return quote(moment.isoformat(timespec="seconds").replace("+00:00", "Z"), safe="")


def _parse(raw: Any) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _within(raw: Any, start: datetime, end: datetime) -> bool:
    moment = _parse(raw)
    return moment is not None and start <= moment <= end


# GitLab's default closing pattern ("Closes #N", "Fixes #N", "Resolves #N");
# "Part of #N" does not close.
_CLOSES = re.compile(
    r"\b(?:close[sd]?|closing|fix(?:e[sd]|ing)?|resolv(?:e[sd]?|ing)|implement(?:s|ed|ing)?)\b:?\s+#(\d+)",
    re.IGNORECASE,
)
_VISUAL_PROOF = re.compile(r"\bVISUAL_PROOF=([A-Za-z/]+)")


def search_merged_on(
    group: str, day: date, tz: Optional[tzinfo] = None, host: str = ""
) -> Dict[Tuple[str, int], Dict[str, Any]]:
    """The MR that closed each issue, for MRs merged on the local day ``day``.

    Keyed ``(repo, issue iid)``; the value carries ``pr`` (the MR iid),
    ``pr_url`` and the MR description's ``VISUAL_PROOF=`` state (``None`` when
    it has none). The group endpoint filters on update time, so merge time is
    checked here. Where two MRs close one issue the later one wins.
    """
    start, end = local_day_window(day, tz)
    rows = _api_list(
        f"groups/{_enc(group)}/merge_requests?state=merged&updated_after={_stamp(start)}"
        f"&per_page={_PAGE}",
        host,
    )
    closers: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for row in rows:
        number = row.get("iid")
        if not isinstance(number, int) or not _within(row.get("merged_at"), start, end):
            continue
        body = str(row.get("description") or "")
        state = _VISUAL_PROOF.search(body)
        for issue in _CLOSES.findall(body):
            key = (_repo_name(row), int(issue))
            if key not in closers or number > closers[key]["pr"]:
                closers[key] = {
                    "pr": number, "pr_url": str(row.get("web_url") or ""),
                    "visual_proof": state.group(1).lower() if state else None,
                }
    return closers


def search_done_on(
    group: str, day: date, tz: Optional[tzinfo] = None, host: str = ""
) -> List[Dict[str, Any]]:
    """Issues closed on the local day ``day``, latest close first.

    Group issues have no ``closed_after`` filter, so the read asks for issues
    updated since the day began and keeps those whose ``closed_at`` falls in
    the day. Each row carries the MR that closed it, from one more read over
    the same window; a failed MR read leaves the cards without one. GitLab
    records no close reason, so ``not_planned`` is always ``False``.
    """
    start, end = local_day_window(day, tz)
    closed = _group_issues(group, f"state=closed&updated_after={_stamp(start)}", host)
    try:
        closers = search_merged_on(group, day, tz, host)
    except GlabError as exc:
        logger.warning("⚠️ glab merged-MR read for %s failed: %s", day.isoformat(), exc)
        closers = {}
    done: List[Dict[str, Any]] = []
    for issue in map(_norm_issue, closed):
        if not (_is_actionable(issue["labels"]) and _within(issue["closed_at"], start, end)):
            continue
        closer = closers.get((issue["repo"], int(issue["number"] or 0))) or {}
        done.append({
            **issue, "state": "closed", "labels": [],
            "pr": closer.get("pr"), "pr_url": closer.get("pr_url", ""),
            "visual_proof": closer.get("visual_proof"), "not_planned": False,
        })
    done.sort(key=lambda item: str(item.get("closed_at") or ""), reverse=True)
    return done


def search_done_today(group: str, host: str = "") -> List[Dict[str, Any]]:
    """Issues closed since local midnight today."""
    return search_done_on(group, date.today(), host=host)


# ------------------------------------------------------------------ writes

def _my_user_id(host: str) -> Optional[int]:
    me = _api_json("user", host)
    uid = me.get("id") if isinstance(me, dict) else None
    return uid if isinstance(uid, int) else None


def create_issue(group: str, repo: str, title: str, body: str, host: str = "") -> str:
    """File one issue on ``group/repo`` assigned to the signed-in user; returns its URL."""
    payload: Dict[str, Any] = {"title": title, "description": body}
    uid = _my_user_id(host)
    if uid is not None:
        payload["assignee_ids"] = [uid]
    created = _send("POST", f"projects/{_project(group, repo)}/issues", payload, host)
    url = str(created.get("web_url") or "")
    if not url:
        raise GlabError("glab issue create returned no issue URL")
    return url


def _issue(group: str, repo: str, number: int, host: str, timeout: float) -> Dict[str, Any]:
    data = _api_json(
        f"projects/{_project(group, repo)}/issues/{int(number)}", host, timeout=timeout,
    )
    if not isinstance(data, dict):
        raise GlabError("glab issue read printed no object")
    return data


def issue_body(group: str, repo: str, number: int, host: str = "") -> str:
    """The description of ``group/repo#number``; :class:`GlabError` when it did not read."""
    return str(_issue(group, repo, number, host, _GLAB_TIMEOUT_SECONDS).get("description") or "")


def issue_title(
    group: str, repo: str, number: int, *, timeout: float = 4.0, host: str = ""
) -> str:
    """The title of issue ``group/repo#number``; :class:`GlabError` when it did not read."""
    return str(_issue(group, repo, number, host, timeout).get("title") or "").strip()


def comment_issue(group: str, repo: str, number: int, body: str, host: str = "") -> None:
    """Post one note on ``group/repo#number``; the body travels on stdin."""
    _send("POST", f"projects/{_project(group, repo)}/issues/{int(number)}/notes",
          {"body": body}, host)


# ------------------------------------------------------------------ cache

_lock = threading.Lock()
_cache: Dict[str, Any] = {
    "fetched_at": None, "issues": [], "records": [], "truncated": False,
    "prs": [], "done": [], "error": None,
}

# Matches the owner-check sweep's own cooldown.
AUTO_REFRESH_COOLDOWN_SECONDS = 60


def recently_filled(seconds: float = AUTO_REFRESH_COOLDOWN_SECONDS) -> bool:
    """Whether a refresh landed inside ``seconds``; never-filled or unreadable is no."""
    stamp = _parse(snapshot().get("fetched_at"))
    if stamp is None:
        return False
    return (datetime.now(timezone.utc) - stamp).total_seconds() < seconds


def snapshot() -> Dict[str, Any]:
    """The cached GitLab view: a pure memory read, safe on every poll."""
    with _lock:
        return dict(_cache)


def refresh(group: str, host: str = "") -> Dict[str, Any]:
    """Run the ``glab`` reads now and replace the cache.

    Subprocess-heavy, so callers invoke it on explicit demand only, never on
    a poll. On failure the previous data is kept and only ``error`` changes.
    An empty ``group`` never touches a subprocess: the snapshot carries a
    configuration hint as its ``error``.
    """
    group = (group or "").strip()
    if not group:
        return _record_error("set gitlab_group in Settings")
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        rows = [
            _norm_issue(r) for r in _group_issues(
                group, "state=opened&order_by=updated_at&sort=desc", host)
        ]
        issues = [i for i in rows if _is_actionable(i["labels"])]
        records = [i for i in rows if not _is_actionable(i["labels"])]
        prs = search_open_prs(group, host)
        done = search_done_today(group, host)
    except GlabError as exc:
        logger.warning("⚠️ glab refresh failed: %s", exc)
        return _record_error(str(exc))
    except Exception as exc:
        logger.exception("❌ glab refresh failed unexpectedly")
        return _record_error(f"refresh failed unexpectedly: {type(exc).__name__}: {exc}")
    with _lock:
        _cache.update(
            fetched_at=fetched_at, issues=issues, records=records,
            truncated=len(rows) >= _PAGE, prs=prs, done=done, error=None,
        )
        logger.info(
            "✅ glab refresh: %d open issues (%d records), %d MRs, %d done today",
            len(rows), len(records), len(prs), len(done),
        )
        return dict(_cache)


def _record_error(error: str) -> Dict[str, Any]:
    """Keep the previous data, surface ``error`` to every later reader."""
    with _lock:
        _cache["error"] = error
        return dict(_cache)


def reset_cache() -> None:
    """Test helper: restore the pristine empty cache."""
    with _lock:
        _cache.update(
            fetched_at=None, issues=[], records=[], truncated=False, prs=[], done=[], error=None,
        )
