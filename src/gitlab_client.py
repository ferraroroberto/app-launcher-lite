"""Fleet-wide GitLab reads for the Board tab, via the ``glab`` CLI (Phase 5).

Wraps the group-scoped queries the Board consumes — open issues and today's
closed issues across every project under the configured GitLab group —
behind a module-level cache. The contract mirrors the Coding tab's ⎇
git-status button: **``glab`` is a subprocess per call and is never invoked
on a poll** — :func:`refresh` runs the queries only when the user asks (or
once on first Board activation), while :func:`snapshot` is a pure memory
read the 5s board poll can hit for free.

:func:`search_open_prs` (merge requests) is public and tested but not wired
into :func:`refresh` — the 4-column Board has no MR surface; it awaits a
future one.

Errors degrade, never break the board: a failed refresh keeps the previous
snapshot's data and surfaces ``error`` so the UI can badge it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote

from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

_GLAB_TIMEOUT_SECONDS = 20.0
_ISSUE_LIMIT = 100
_MR_LIMIT = 50


class GlabError(RuntimeError):
    """``glab`` missing, timed out, unauthenticated, or non-zero exit."""


def _run_glab(
    args: Sequence[str], *, host: str = "", timeout: float = _GLAB_TIMEOUT_SECONDS
) -> str:
    """Run ``glab <args>`` and return stdout; :class:`GlabError` on any failure.

    A non-empty ``host`` rides the ``GITLAB_HOST`` env var (glab's own
    self-hosted-instance selector); empty means glab's default context.
    """
    env: Optional[Dict[str, str]] = None
    if host:
        env = {**os.environ, "GITLAB_HOST": host}
    try:
        proc = subprocess.run(
            ["glab", *args],
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
        raise GlabError(
            f"glab {' '.join(args[:2])} exited {proc.returncode}: {first}"
        )
    return proc.stdout


def _api(path: str, host: str = "") -> List[Dict[str, Any]]:
    out = _run_glab(["api", path], host=host)
    try:
        data = json.loads(out or "[]")
    except ValueError as exc:
        raise GlabError(f"glab returned unparseable JSON: {exc}") from exc
    return data if isinstance(data, list) else []


def _repo_name(row: Dict[str, Any]) -> str:
    """Short project name from ``references.full``.

    A group endpoint returns subgroup projects too, so ``full`` can be
    ``grp/sub/project#N`` (or ``!N`` for MRs) — the repo short name is the
    LAST path segment of the part before the ``#``/``!`` marker.
    """
    refs = row.get("references") or {}
    full = str(refs.get("full") or "")
    head = full.split("#", 1)[0].split("!", 1)[0]
    return head.split("/")[-1]


def _norm_issue(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "issue",
        "repo": _repo_name(row),
        "number": row.get("iid"),
        "title": row.get("title"),
        "url": row.get("web_url"),
        "updated_at": row.get("updated_at"),
        # GitLab labels are plain strings already (gh gave [{name}]).
        "labels": [str(lab) for lab in (row.get("labels") or [])],
    }


def _norm_mr(row: Dict[str, Any]) -> Dict[str, Any]:
    # Kind stays "pr" — the internal card vocabulary board.js reads.
    return {
        "kind": "pr",
        "repo": _repo_name(row),
        "number": row.get("iid"),
        "title": row.get("title"),
        "url": row.get("web_url"),
        "updated_at": row.get("updated_at"),
        "is_draft": bool(row.get("draft")),
        "labels": [str(lab) for lab in (row.get("labels") or [])],
    }


# The fleet's ``/codebase-audit`` skill files ledger/decision-log issues under
# this label ("codebase-audit ledger / metadata — not actionable work") —
# real bookkeeping, not dispatchable work, so the Board hides them entirely.
_NON_ACTIONABLE_LABELS = {"audit-meta"}


def _is_actionable(labels: Sequence[str]) -> bool:
    return not any(str(lab).lower() in _NON_ACTIONABLE_LABELS for lab in labels)


def _group_path(group: str) -> str:
    # "grp/sub" must reach the API as one URL-encoded path segment.
    return quote(group, safe="")


def search_open_issues(group: str, host: str = "") -> List[Dict[str, Any]]:
    rows = _api(
        f"groups/{_group_path(group)}/issues"
        f"?state=opened&order_by=updated_at&sort=desc&per_page={_ISSUE_LIMIT}",
        host,
    )
    issues = [_norm_issue(r) for r in rows]
    return [i for i in issues if _is_actionable(i["labels"])]


def search_open_prs(group: str, host: str = "") -> List[Dict[str, Any]]:
    """Open merge requests across the group, normalized to ``kind: "pr"``.

    Tested but deliberately unwired: the 4-column Board dropped its MR
    surface (the old "Other" column), so :func:`refresh` never calls this.
    Kept public — field mapping validated by tests — for a future MR view.
    """
    rows = _api(
        f"groups/{_group_path(group)}/merge_requests"
        f"?state=opened&order_by=updated_at&sort=desc&per_page={_MR_LIMIT}",
        host,
    )
    prs = [_norm_mr(r) for r in rows]
    return [p for p in prs if _is_actionable(p["labels"])]


def _local_midnight() -> datetime:
    return datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def search_done_today(group: str, host: str = "") -> List[Dict[str, Any]]:
    """Closed issues since local midnight, newest first.

    Group issues have no ``closed_after`` param, so the query uses
    ``state=closed&updated_after=<local midnight>`` and then filters
    client-side on ``closed_at >= local midnight`` (a row can be *updated*
    today but closed yesterday, and ``closed_at`` may be null on some rows —
    both are excluded).
    """
    midnight = _local_midnight()
    since = quote(midnight.isoformat(), safe="")
    closed = _api(
        f"groups/{_group_path(group)}/issues"
        f"?state=closed&updated_after={since}&per_page={_ISSUE_LIMIT}",
        host,
    )
    done: List[Dict[str, Any]] = []
    for row in closed:
        closed_at = _parse_iso(row.get("closed_at"))
        if closed_at is None or closed_at < midnight:
            continue
        issue = _norm_issue(row)
        if not _is_actionable(issue["labels"]):
            continue
        done.append({**issue, "state": "closed", "labels": []})
    done.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return done


# ------------------------------------------------------------------ cache

_lock = threading.Lock()
_cache: Dict[str, Any] = {
    "fetched_at": None,
    "issues": [],
    # Reserved for a future MR surface — refresh() never fills it (the
    # 4-column Board consumes issues + done only).
    "prs": [],
    "done": [],
    "error": None,
}


def snapshot() -> Dict[str, Any]:
    """The cached GitLab view — a pure memory read, safe on every poll."""
    with _lock:
        return dict(_cache)


def refresh(group: str, host: str = "") -> Dict[str, Any]:
    """Run the ``glab`` queries now and replace the cache.

    Subprocess-heavy (two ``glab`` calls) — callers invoke this on explicit
    user demand only, never on a poll. On failure the previous data is kept
    and only ``error`` is updated, so a flaky ``glab`` degrades to a badge
    instead of an empty board. An empty ``group`` never touches a subprocess
    — the snapshot just carries a configuration hint as its ``error``.
    """
    group = (group or "").strip()
    if not group:
        with _lock:
            _cache["error"] = "set gitlab_group in Settings"
            return dict(_cache)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        issues = search_open_issues(group, host)
        done = search_done_today(group, host)
    except GlabError as exc:
        logger.warning("⚠️ glab refresh failed: %s", exc)
        with _lock:
            _cache["error"] = str(exc)
            return dict(_cache)
    with _lock:
        _cache.update(fetched_at=fetched_at, issues=issues, done=done, error=None)
        logger.info(
            "✅ glab refresh: %d issues, %d done today", len(issues), len(done)
        )
        return dict(_cache)


def reset_cache() -> None:
    """Test helper — restore the pristine empty cache."""
    with _lock:
        _cache.update(fetched_at=None, issues=[], prs=[], done=[], error=None)
