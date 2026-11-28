"""Status, preview and publish for one linked project.

All three start the same way (:func:`_prepare`): fetch both mains into the
scratch repo and build the snapshot tree. Up to date means that tree equals the
team main's tree, worked out fresh every time, so neither repo needs any
bookkeeping. Publish re-runs every check itself and trusts nothing a preview
returned to the browser.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.clients import llm as llm_client
from src.team_mirror import forge, scan, snapshot, state
from src.team_mirror.links import MirrorLink, work_identity

logger = logging.getLogger(__name__)

MODE_MERGE, MODE_PR_ONLY = "merge", "pr_only"
MODES = (MODE_MERGE, MODE_PR_ONLY)

# How long a computed status row is served before a fetch is due again; the
# page says "refreshes every 30 min".
STATUS_TTL_SECONDS = 30 * 60
_PR_TEXT_TIMEOUT = 30.0
_PR_TEXT_MAX_TOKENS = 600
_PR_TEXT_PROMPT = (
    "You write the description of a pull request that brings a team repository up to date with "
    "its source. You get the commit subjects since the last update, newest first. Write a short "
    "summary paragraph, then a bullet list of the notable changes grouped by theme. Plain "
    "Markdown, no heading, no names of people, no links."
)

# One scratch work tree per project: a check, a preview and a publish of the
# same project take turns on it.
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_STATUS_CACHE: Dict[str, Tuple[float, MirrorLink, Dict[str, Any]]] = {}


class PublishBusy(RuntimeError):
    """A check, preview or publish of the same project is already running."""


@dataclass(frozen=True)
class Context:
    """What every call needs about one project; the paths default to the runtime data root."""

    project: str
    project_dir: Path
    link: MirrorLink
    extra_terms: Tuple[str, ...] = ()
    hub_url: str = ""
    scratch_root: Optional[Path] = None
    state_path: Optional[Path] = None


@dataclass
class _Prepared:
    repo: Path
    personal_sha: str
    team_sha: str
    tree: str
    team_tree: str
    last: Dict[str, Any]

    @property
    def up_to_date(self) -> bool:
        return self.tree == self.team_tree


def _prepare(ctx: Context) -> _Prepared:
    repo = snapshot.scratch_repo(ctx.project, ctx.scratch_root)
    personal_sha = snapshot.fetch_personal(repo, ctx.project_dir)
    team_sha = snapshot.fetch_team(repo, ctx.link.team_url, ctx.link.branch)
    tree = snapshot.build_tree(repo, personal_sha, ctx.link.leave_out)
    last = state.last_publish(ctx.project, ctx.state_path)
    return _Prepared(repo, personal_sha, team_sha, tree, snapshot.tree_of(repo, team_sha), last)


def _since_last(prep: _Prepared) -> Optional[str]:
    """The last published personal commit when it is still in the personal history, else None."""
    sha = str(prep.last.get("personal_sha") or "")
    if sha and snapshot.has_commit(prep.repo, sha) and snapshot.is_ancestor(prep.repo, sha, prep.personal_sha):
        return sha
    return None


def _behind(prep: _Prepared) -> Optional[int]:
    since = _since_last(prep)
    return snapshot.count_between(prep.repo, since, prep.personal_sha) if since else None


def _scan(ctx: Context, prep: _Prepared, texts: List[Tuple[str, str]]) -> scan.ScanResult:
    return scan.scan(prep.repo, snapshot.tracked_files(prep.repo), ctx.project_dir, ctx.extra_terms, texts)


def _team_changes(prep: _Prepared) -> List[Dict[str, str]]:
    recorded = str(prep.last.get("team_sha") or "") or None
    return [{"sha": sha, "subject": subject}
            for sha, subject in snapshot.team_commits_since(prep.repo, recorded, prep.team_sha)]


def _title(now: datetime) -> str:
    return f"Mirror update {now:%Y-%m-%d}"


def draft_pr_text(ctx: Context, subjects: List[str]) -> Tuple[str, str]:
    """The PR text and where it came from: ``hub``, or ``subjects`` when the hub is unset or down."""
    plain = "\n".join(f"- {s}" for s in subjects) or "- Files brought up to date."
    if not ctx.hub_url.strip() or not subjects:
        return plain, "subjects"
    try:
        text = llm_client.summarize(ctx.hub_url, "\n".join(subjects), _PR_TEXT_PROMPT,
                                    max_tokens=_PR_TEXT_MAX_TOKENS, timeout=_PR_TEXT_TIMEOUT)
    except llm_client.LlmError as exc:
        logger.info("ℹ️ team mirror: the hub did not draft the PR text (%s); using the subjects", exc)
        return plain, "subjects"
    return text.strip(), "hub"


def status(ctx: Context) -> Dict[str, Any]:
    """One Mirrors-list row: ``up_to_date``, ``behind``, ``blocked`` or ``error``."""
    row: Dict[str, Any] = {"project": ctx.project, "checked_at": _stamp(datetime.now(timezone.utc))}
    try:
        with _lock(ctx.project):
            prep = _prepare(ctx)
            if prep.up_to_date:
                return {**row, "state": "up_to_date", "behind": 0}
            result = _scan(ctx, prep, [])
            behind = _behind(prep)
            team = _team_changes(prep)
    except snapshot.SnapshotError as exc:
        return {**row, "state": "error", "error": str(exc)}
    out = {**row, "behind": behind, "published_before": bool(prep.last), "team_changes": len(team),
           "scan": result.as_dict()}
    return {**out, "state": "behind" if result.state == scan.CLEAR else "blocked"}


def cached_status(ctx: Context, *, refresh: bool = False) -> Dict[str, Any]:
    """:func:`status`, reused for :data:`STATUS_TTL_SECONDS` while the link is unchanged."""
    hit = _STATUS_CACHE.get(ctx.project)
    if hit and not refresh and hit[1] == ctx.link and time.monotonic() - hit[0] < STATUS_TTL_SECONDS:
        return hit[2]
    row = status(ctx)
    _STATUS_CACHE[ctx.project] = (time.monotonic(), ctx.link, row)
    return row


def forget_status(project: str) -> None:
    _STATUS_CACHE.pop(project, None)


def preview(ctx: Context, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything the Preview sheet shows; nothing is written to either repo."""
    with _lock(ctx.project):
        return _preview(ctx, now or datetime.now(timezone.utc))


def _preview(ctx: Context, moment: datetime) -> Dict[str, Any]:
    prep = _prepare(ctx)
    identity = work_identity(prep.repo)
    base = {"project": ctx.project, "host": forge.remote_of(ctx.link.team_url).kind,
            "identity": identity.__dict__ if identity else None}
    if prep.up_to_date:
        return {**base, "up_to_date": True, "changes": [], "can_publish": False}
    subjects = snapshot.subjects_between(prep.repo, _since_last(prep), prep.personal_sha)
    body, source = draft_pr_text(ctx, subjects)
    title = _title(moment)
    result = _scan(ctx, prep, [("PR title", title), ("PR text", body)])
    team = _team_changes(prep)
    changes = snapshot.changes(prep.repo, prep.team_sha, prep.tree)
    return {
        **base,
        "up_to_date": False,
        "behind": _behind(prep),
        "changes": [{"status": c.status, "path": c.path} for c in changes],
        "scan": result.as_dict(),
        "team_changes": team,
        "needs_confirmation": bool(team),
        "pr_title": title,
        "pr_body": body,
        "pr_body_source": source,
        "can_publish": result.state == scan.CLEAR and identity is not None,
    }


def _lock(project: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(project, threading.Lock())


def publish(
    ctx: Context,
    *,
    mode: str = MODE_MERGE,
    confirm_team_changes: bool = False,
    pr_body: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Publish one snapshot. ``result`` is one of ``up_to_date``, ``blocked``, ``no_identity``,
    ``needs_confirmation``, ``merged``, ``pr_opened`` or ``branch_pushed``.

    :class:`snapshot.SnapshotError` / :class:`forge.ForgeError` propagate for a
    step that failed outright; :class:`PublishBusy` when one is already running.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    lock = _lock(ctx.project)
    if not lock.acquire(blocking=False):
        raise PublishBusy(f"{ctx.project} is being checked or published already: try again in a moment")
    try:
        return _publish(ctx, mode, confirm_team_changes, pr_body, now or datetime.now(timezone.utc))
    finally:
        forget_status(ctx.project)
        lock.release()


def _publish(ctx: Context, mode: str, confirm: bool, pr_body: Optional[str], now: datetime) -> Dict[str, Any]:
    prep = _prepare(ctx)
    if prep.up_to_date:
        return {"result": "up_to_date"}
    identity = work_identity(prep.repo)
    if identity is None:
        return {"result": "no_identity", "error": "set git's global user.name and user.email on this computer"}
    title = _title(now)
    body = pr_body.strip() if pr_body and pr_body.strip() else draft_pr_text(
        ctx, snapshot.subjects_between(prep.repo, _since_last(prep), prep.personal_sha))[0]
    result = _scan(ctx, prep, [("PR title", title), ("PR text", body)])
    if result.state != scan.CLEAR:
        logger.warning("⚠️ team mirror: publish of %s stopped by the privacy scan (%s, %d findings)",
                       ctx.project, result.state, len(result.findings))
        return {"result": "blocked", "scan": result.as_dict()}
    team = _team_changes(prep)
    if team and not confirm:
        return {"result": "needs_confirmation", "team_changes": team}
    commit = snapshot.make_commit(prep.repo, prep.tree, prep.team_sha, identity, f"{title}\n\n{body}\n")
    remote = forge.remote_of(ctx.link.team_url)
    branch = f"mirror/{now:%Y-%m-%d-%H%M%S}"
    if remote.kind == forge.LOCAL and mode == MODE_MERGE:
        snapshot.push(prep.repo, ctx.link.team_url, commit, ctx.link.branch)
        return _recorded(ctx, prep, commit, now, {"result": "merged", "commit": commit})
    snapshot.push(prep.repo, ctx.link.team_url, commit, branch)
    if remote.kind == forge.LOCAL:
        return {"result": "branch_pushed", "branch": branch, "commit": commit}
    pr = forge.open_and_merge(remote, branch, ctx.link.branch, title, body, merge=mode == MODE_MERGE)
    if not pr.merged:
        return {"result": "pr_opened", "url": pr.url, "note": pr.note, "branch": branch}
    team_after = snapshot.fetch_team(prep.repo, ctx.link.team_url, ctx.link.branch)
    return _recorded(ctx, prep, team_after, now, {"result": "merged", "url": pr.url, "commit": team_after}, pr.url)


def _recorded(ctx: Context, prep: _Prepared, team_sha: str, now: datetime, out: Dict[str, Any],
              pr_url: str = "") -> Dict[str, Any]:
    state.record_publish(ctx.project, {
        "personal_sha": prep.personal_sha,
        "team_sha": team_sha,
        "published_at": _stamp(now),
        "pr_url": pr_url,
    }, ctx.state_path)
    logger.info("✅ team mirror: published %s (%s)", ctx.project, out["result"])
    return out


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
