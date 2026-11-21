"""The fleet chief's worktree sweep.

Every `<repo>-wt-<N>` folder beside the fleet repos is a leftover of a
chief-dispatched lane: the owner does not use worktrees himself, and has
authorised the chief to remove finished ones on its own judgment, including
those whose issue is still open (umbrella issues, issues waiting on a live
check, plan-only lanes). The judgment is mechanical, so it lives here rather than being re-typed as
`git status` / `git log` / `gh pr list` by hand on every lane close.

`classify_worktree` puts each folder in exactly one class:

  finished      clean tree, and either no commits ahead of the default branch
                or every local commit is in a *merged* PR (a squash merge
                leaves the local SHAs ahead, so the PR is asked, not the log)
  in-use        a live launcher session's cwd is inside it, a live
                active-issue marker names it, a live chief-managed session
                is working that repo#N, or it is younger than the grace
                period (a lane may not have published its marker yet)
  has-work      dirty tree, or commits that are not on the default branch
                and not in a merged PR -- the only class the chief asks about
  orphan-empty  an empty folder that is not a git worktree
  unknown       anything the sweep could not establish (unreadable git, a PR
                lookup that failed, an unreadable board) -- its own state,
                never folded into `finished`

`--apply` removes only `finished` (through `worktree_claim.remove_worktree`,
the junction-safe teardown) and `orphan-empty` (`rmdir`, which refuses a
non-empty or held folder). A folder that will not go stays and is reported.
It never kills a process and never touches the other three classes.

`stale_sessions` is the companion read: chief-managed sessions that are
`idle-finished` after their issue closed, listed so the chief can `stop` them
in the same pass. Listed only -- stopping is `chief_ops.py stop`.

`scan`, `apply_findings` and `stale_sessions` take their lookups (PR state,
issue state, remover) as arguments, so the unit tests drive every class
against throwaway repos without `gh`.

stdlib only (plus the sibling helpers).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import active_issue  # noqa: E402
import git_run  # noqa: E402
import worktree_residue  # noqa: E402
from worktree_residue import WT_SEP  # noqa: E402

LOGGER = logging.getLogger(__name__)

WORKTREE_RE = re.compile(rf"^(?P<repo>.+){re.escape(WT_SEP)}(?P<number>\d+)$")
DEFAULT_REF_CANDIDATES = ("origin/main", "origin/master")
GH_TIMEOUT_S = 60
# A fresh worktree is clean and level with the default branch, so it reads as
# `finished` until its lane commits; ten minutes covers a lane that studies
# before it writes anything.
GRACE_S = 600

FINISHED = "finished"
IN_USE = "in-use"
HAS_WORK = "has-work"
ORPHAN_EMPTY = "orphan-empty"
UNKNOWN = "unknown"
CLASSES = (FINISHED, IN_USE, HAS_WORK, ORPHAN_EMPTY, UNKNOWN)
REMOVABLE = (FINISHED, ORPHAN_EMPTY)

IDLE_FINISHED = "idle-finished"

# A PR lookup answers a list of {number, state, headRefOid} rows, or None when
# the answer could not be established (gh failed, remote unparseable).
PrLookup = Callable[[str, str], Optional[List[Dict[str, Any]]]]


@dataclass
class Finding:
    name: str
    path: Path
    cls: str
    evidence: str
    outcome: str = ""


# ---- live-state inputs (pure) ----------------------------------------------


def _norm(path: Any) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def board_cards(board: Mapping[str, Any]) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    for column in (board.get("columns") or {}).values():
        cards.extend(c for c in column or [] if isinstance(c, dict) and c.get("session_id"))
    return cards


def live_dirs(cards: Iterable[Mapping[str, Any]]) -> List[str]:
    """Normalised cwd of every alive launcher session."""
    return [_norm(c["project_dir"]) for c in cards if c.get("alive") and c.get("project_dir")]


def live_session_ids(cards: Iterable[Mapping[str, Any]]) -> set:
    return {str(c["session_id"]) for c in cards if c.get("alive")}


def _holder(path: Path, dirs: Sequence[str]) -> Optional[str]:
    root = _norm(path)
    for cwd in dirs:
        if cwd == root or cwd.startswith(root + os.sep):
            return cwd
    return None


def _live_managed_session(
    repo: str, number: int, managed: Mapping[str, Any], session_ids: set,
) -> Optional[str]:
    for sid, row in managed.items():
        if (sid in session_ids and isinstance(row, dict) and row.get("number") == number
                and str(row.get("repo") or "").casefold() == repo.casefold()):
            return str(sid)
    return None


# ---- git facts ---------------------------------------------------------------


def _git(path: Path, *args: str) -> str:
    return git_run.run_git_or_raise(path, *args)


def github_pr_lookup(remote_url: str, branch: str) -> Optional[List[Dict[str, Any]]]:
    """Every PR (any state) whose head is `branch`, or None if not established."""
    slug = git_run.slug_from_url(remote_url)
    if not slug:
        return None
    res = git_run.run_gh(
        ["pr", "list", "--repo", slug, "--head", branch, "--state", "all",
         "--json", "number,state,headRefOid", "--limit", "10"],
        timeout=GH_TIMEOUT_S,
    )
    if res.returncode != 0:
        return None
    try:
        rows = json.loads(res.stdout)
    except ValueError:
        return None
    return rows if isinstance(rows, list) else None


def _merged_pr_covers_head(path: Path, head: str, prs: List[Dict[str, Any]]) -> Optional[int]:
    """Number of a merged PR whose head contains local HEAD, else None."""
    for pr in prs:
        if str(pr.get("state")).upper() != "MERGED":
            continue
        oid = str(pr.get("headRefOid") or "")
        if not oid:
            continue
        if oid == head:
            return int(pr["number"])
        # Local branch behind the merged head: HEAD is an ancestor of it.
        if git_run.run_git(["-C", str(path), "merge-base", "--is-ancestor", head, oid]).returncode == 0:
            return int(pr["number"])
    return None


# ---- classification ----------------------------------------------------------


def classify_worktree(
    path: Path,
    *,
    dirs: Optional[Sequence[str]],
    markers: Optional[Mapping[str, Any]],
    session_ids: Optional[set],
    pr_lookup: PrLookup,
    host: Optional[str] = None,
    pid_probe: Callable[[int], Optional[bool]] = active_issue.pid_state,
    managed: Optional[Mapping[str, Any]] = None,
    grace_s: float = GRACE_S,
    now: Callable[[], float] = time.time,
) -> Finding:
    """Class + evidence for one `<repo>-wt-<N>` folder. See the module docstring.

    `dirs` / `markers` / `session_ids` are None when the board or the marker
    file could not be read: a removable verdict then cannot be established and
    degrades to `unknown`, while `has-work` (git evidence alone) still stands.
    `managed` is the chief-managed session map (session id -> {repo, number}):
    a live session in it for this folder's repo#N holds the tree whether or not
    its marker was published or read back correctly.
    """
    name = path.name
    held = _holder(path, dirs) if dirs is not None else None
    if held:
        return Finding(name, path, IN_USE, f"live session cwd {held}")

    match = WORKTREE_RE.match(name)
    marker_key = active_issue.marker_key(match.group("repo"), int(match.group("number"))) if match else ""
    row = (markers or {}).get(marker_key)
    if row is not None:
        owner = active_issue.classify_owner(
            row, live_session_ids=session_ids, pid_probe=pid_probe,
            host=host or active_issue.current_host(),
        )
        if owner == active_issue.LIVENESS_LIVE:
            return Finding(name, path, IN_USE, f"live active-issue marker {marker_key}")
        if owner == active_issue.LIVENESS_UNKNOWN:
            return Finding(name, path, UNKNOWN, f"active-issue marker {marker_key} owner liveness unknown")

    if match and managed and session_ids:
        sid = _live_managed_session(match.group("repo"), int(match.group("number")), managed, session_ids)
        if sid:
            return Finding(name, path, IN_USE, f"live session {sid[:8]} (chief-managed {marker_key})")

    live_known = dirs is not None and markers is not None

    if (path / ".git").is_dir():
        return Finding(name, path, UNKNOWN, "a real repository named like a worktree")
    facts = worktree_residue.dir_facts(path)
    if not facts.has_git:
        cls, why = worktree_residue.classify_orphan(False, None if facts.empty is None else not facts.empty)
        if cls != "orphan-empty":
            return Finding(name, path, UNKNOWN, why)
        if not live_known:
            return Finding(name, path, UNKNOWN, "empty folder, live sessions not established")
        return Finding(name, path, ORPHAN_EMPTY, "empty folder, not a git worktree")

    try:
        branch = _git(path, "branch", "--show-current")
        dirty = len([line for line in _git(path, "status", "--porcelain").splitlines() if line.strip()])
        head = _git(path, "rev-parse", "HEAD")
        ref = git_run.resolve_default_branch_ref(path, candidates=DEFAULT_REF_CANDIDATES, final_fallback="")
        ahead = int(_git(path, "rev-list", "--count", f"{ref}..HEAD")) if ref else None
        remote = _git(path, "remote", "get-url", "origin")
    except (git_run.Unreadable, ValueError) as exc:
        return Finding(name, path, UNKNOWN, f"git facts unreadable: {exc}")

    facts = f"branch={branch or '(detached)'} ahead={'?' if ahead is None else ahead} dirty={dirty}"
    if dirty:
        return Finding(name, path, HAS_WORK, facts)
    if ahead is None:
        return Finding(name, path, UNKNOWN, f"{facts} (no default-branch ref)")

    if ahead == 0:
        verdict, evidence = FINISHED, f"{facts} (nothing beyond {ref})"
    elif not branch:
        return Finding(name, path, HAS_WORK, f"{facts} (detached HEAD)")
    else:
        prs = pr_lookup(remote, branch)
        if prs is None:
            return Finding(name, path, UNKNOWN, f"{facts} (PR state not established)")
        merged = _merged_pr_covers_head(path, head, prs)
        if merged is not None:
            verdict, evidence = FINISHED, f"{facts} pr=#{merged} merged"
        else:
            states = ",".join(f"#{p.get('number')} {str(p.get('state')).lower()}" for p in prs) or "none"
            return Finding(name, path, HAS_WORK, f"{facts} pr={states}")

    if not live_known:
        return Finding(name, path, UNKNOWN, f"{evidence}; live sessions not established")
    try:
        age_s = now() - (path / ".git").stat().st_mtime
    except OSError as exc:
        return Finding(name, path, UNKNOWN, f"{evidence}; tree age unreadable: {exc}")
    if age_s < grace_s:
        return Finding(name, path, IN_USE, f"younger than {grace_s / 60:g} min ({age_s / 60:.1f} min old)")
    return Finding(name, path, verdict, evidence)


def scan(root: Path, **lookups: Any) -> List[Finding]:
    """Classify every `<repo>-wt-<N>` directory directly under `root`."""
    found: List[Finding] = []
    for entry in sorted(Path(root).iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir() and WORKTREE_RE.match(entry.name):
            found.append(classify_worktree(entry, **lookups))
    return found


# ---- apply -------------------------------------------------------------------


def apply_findings(
    findings: Sequence[Finding],
    remover: Callable[[Path], int],
    rmdir: Callable[[Path], None] = lambda p: p.rmdir(),
) -> int:
    """Remove `finished` and `orphan-empty`; record each outcome. Returns the
    number of folders that were attempted and survived (busy / refused)."""
    stuck = 0
    for f in findings:
        if f.cls not in REMOVABLE:
            continue
        if f.cls == FINISHED:
            code = remover(f.path)
            f.outcome = "REMOVED" if code == 0 and not f.path.exists() else f"STAYS (remove-worktree exit {code})"
        else:
            try:
                rmdir(f.path)
                f.outcome = "REMOVED"
            except OSError as exc:
                f.outcome = f"STAYS (busy: {exc})"
        if f.outcome != "REMOVED":
            stuck += 1
            LOGGER.warning("⚠️ sweep: %s %s", f.name, f.outcome)
        else:
            LOGGER.info("✅ sweep: removed %s (%s)", f.name, f.cls)
    return stuck


# ---- stale lane sessions -------------------------------------------------------


def stale_sessions(
    cards: Iterable[Mapping[str, Any]],
    managed: Mapping[str, Any],
    fetch_issue: Callable[[str, int], Dict[str, Any]],
) -> List[str]:
    """One line per chief-managed `idle-finished` session, with its issue state.

    `stoppable` only when the issue is established CLOSED; an open issue is the
    chief's call (a lane can idle on a live check) and a failed lookup is
    `unknown`. Never stops anything.
    """
    lines: List[str] = []
    for card in cards:
        sid = str(card.get("session_id") or "")
        row = managed.get(sid)
        if not (card.get("alive") and row and card.get("status") == IDLE_FINISHED):
            continue
        repo, number = str(row.get("repo")), row.get("number")
        state = fetch_issue(repo, int(number))
        issue = str(state.get("state") or "").upper()
        if "error" in state or not issue:
            verdict, issue = "unknown", f"? ({state.get('error', 'no state')})"
        else:
            verdict = "stoppable" if issue == "CLOSED" else "idle-open"
        lines.append(f"SESSION {sid[:8]} {repo}#{number} {IDLE_FINISHED} issue={issue} -> {verdict}")
    return lines


# ---- report --------------------------------------------------------------------


def format_report(findings: Sequence[Finding], sessions: Sequence[str], applied: bool) -> List[str]:
    lines = [f"{f.cls:<12} {f.name}  {f.evidence}" + (f"  -> {f.outcome}" if f.outcome else "")
             for f in findings]
    if not findings:
        lines.append("(no <repo>-wt-<N> folders)")
    counts = {c: sum(1 for f in findings if f.cls == c) for c in CLASSES}
    lines.append("SUMMARY " + " ".join(f"{c}={n}" for c, n in counts.items())
                 + ("" if applied else "  (read-only; --apply removes finished + orphan-empty)"))
    lines.extend(sessions)
    return lines


def default_root() -> Path:
    """The folder holding the fleet repos: the parent of this checkout's repo."""
    return Path(__file__).resolve().parents[3].parent


def add_cli(parser: argparse.ArgumentParser) -> None:
    """Mount the sweep flags on `chief_ops.py sweep`."""
    parser.add_argument("--apply", action="store_true",
                        help="remove finished (remove-worktree) and orphan-empty (rmdir) folders")
    parser.add_argument("--root", default=None, help="folder holding the <repo>-wt-<N> dirs (default: beside this repo)")
