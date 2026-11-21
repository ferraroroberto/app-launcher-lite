"""The claim half of `worktree_claim.py`: the lock directory and its pure decisions.

Everything here is filesystem-only and git-free, so the unit tests drive it
hermetically; `worktree_claim.py` wires git in and re-exports these names.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from fleet_core import runtime_files
from worktree_residue import WT_SEP

LOCK_NAME = "issue-claim.lock"
META_NAME = "meta.json"
DEFAULT_TTL_HOURS = 8.0


# ---- pure helpers (unit-tested without git) -------------------------------


def worktree_path(repo_root: Path, issue: str) -> Path:
    """Sibling worktree path: `<parent>/<repo-name>-wt-<N>`.

    Same convention as /issue-batch. The `<repo>-wt-<N>` *prefix-matches* the
    repo's `cwd_prefix` in projects.toml, so notify_on_idle still names the right
    project (a `.worktrees/` layout would break that match). `primary_for_worktree`
    inverts this, so `worktree_residue.WT_SEP` is the one place the separator is spelled.
    """
    return repo_root.parent / f"{repo_root.name}{WT_SEP}{issue}"


def is_stale(
    meta: Optional[dict],
    now: float,
    ttl_hours: float,
    branch_exists: Optional["Callable[[str], bool]"] = None,
) -> bool:
    """A claim is stale once it ages past the TTL, its meta is unreadable, or its
    recorded branch no longer exists.

    No PID-liveness check: a one-shot helper invocation can't capture the
    long-lived agent-session PID, and Windows PID checks are unreliable. The TTL
    is the crash-safety valve — a generous default so a legitimately long build
    is never reclaimed out from under itself.

    `branch_exists` is an injected predicate (`branch -> bool`) so this stays a
    pure, git-free function for the unit tests; the CLI wires in a git-backed
    one. When a claim records a `branch` and that branch is already
    merged-and-deleted, the claim is definitionally done — treat it as stale so a
    leaked claim self-heals on the next `acquire` instead of blocking for the
    full TTL. A claim with no recorded branch, or when no
    predicate is supplied, falls back to the TTL alone.
    """
    if not meta:
        return True
    created = meta.get("created")
    try:
        if (now - float(created)) > ttl_hours * 3600.0:
            return True
    except (TypeError, ValueError):
        return True
    branch = meta.get("branch")
    if branch and branch_exists is not None and not branch_exists(branch):
        return True
    return False


def read_meta(lock_dir: Path) -> Optional[dict]:
    try:
        return json.loads((lock_dir / META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_meta(lock_dir: Path, meta: dict) -> None:
    runtime_files.save_text(lock_dir / META_NAME, json.dumps(meta, indent=2), encoding="utf-8")


def _publish_claim(lock_dir: Path, meta: dict) -> bool:
    """Atomically create `lock_dir` **fully populated** with meta.json.

    `mkdir` then a separate `write_meta` (the racy shape) leaves a window
    where `lock_dir` exists but meta.json doesn't yet — a concurrent racer that
    hits `FileExistsError` on its own `mkdir` in that window reads `None` meta,
    treats it as stale, and reclaims (`rmtree` + re-`mkdir`) out from under the
    first winner, which then blindly overwrites meta and *also* returns
    "primary" (two sessions both got MODE=primary on the same
    repo, silently losing one session's uncommitted work).

    Fix: write meta into a private temp dir first (invisible to every other
    process — its name is never guessed), then publish with one `Path.rename`.
    On Windows `os.rename` never replaces an existing destination — it raises
    if `lock_dir` already exists — so the rename is the single atomic
    compare-and-swap: `lock_dir` is only ever observed either fully absent or
    fully populated, never in between.
    """
    tmp_dir = lock_dir.with_name(f"{lock_dir.name}.tmp-{os.getpid()}-{time.time_ns()}")
    runtime_files.make_dirs(tmp_dir, parents=False)
    write_meta(tmp_dir, meta)
    try:
        tmp_dir.rename(lock_dir)
        return True
    except OSError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False


def owner_check(holder: Optional[dict], issue: str, dirty: bool) -> Tuple[bool, str]:
    """Decide whether `issue` may switch this checkout's primary tree to main.

    Pure decision for the `assert-owner` guard: a checkout
    that lands a merge (`/issue-finish` step 5) or syncs main (`/issue-start`
    step 4) must refuse rather than blindly `git checkout <main>` when either
    (a) the tree is dirty — uncommitted work that isn't this session's to
    switch out from under, or (b) the claim is live and held by a *different*
    issue. Passes when the tree is clean and the claim is free or already
    owned by `issue`. Returns `(ok, reason)`; `reason` is the human-readable
    line the CLI prints either way.
    """
    if dirty:
        return False, "working tree has uncommitted changes"
    if holder and str(holder.get("issue")) != str(issue):
        return False, (f"primary held since {holder.get('created_iso', '?')} "
                        f"by issue {holder.get('issue', '?')} on {holder.get('branch', '?')}")
    return True, ("free" if not holder else "owned")


def land_primary_check(
    holder: Optional[dict],
    issue: str,
    dirty: bool,
    current_branch: str,
    main_branch: str,
    behind: Optional[int],
    service: Tuple[bool, str],
) -> Tuple[bool, str]:
    """Decide whether it is safe to fast-forward the primary checkout.

    Pure decision for the `land-primary` guard. A worktree
    lane's merge is authoritative on the remote but *not live* until the
    primary tree is fast-forwarded -- acutely so in this repo, where `hooks/`
    and `skills/` reach `~/.claude` through junctions rooted at the primary, so
    a merged hook or skill change does nothing fleet-wide until that tree moves.

    The landing is a fast-forward and nothing else (guarded by
    `untrack_guard`). `owner_check` supplies
    the "is this tree anyone else's to touch?" half; the branch check enforces
    the other half -- only ever pull a tree *already sitting on* its default
    branch, because a worktree lane must never `git checkout` the primary.
    `behind` and `service` are passed in exactly as `holder` / `dirty` /
    `current_branch` are, so the whole decision stays pure and unit-testable
    with no git and no network inside it -- and so omitting either is a
    `TypeError` at the call site rather than a silently skipped guard.

    `behind` is how many commits the tree is behind its remote default branch,
    or `None` when that could not be counted. **Zero short-circuits the service
    guard**: there is nothing to fast-forward, so no live
    process can be skewed by a landing that isn't going to happen, and the tree
    is already exactly what the merge produced. Refusing there reported `stale`
    for a demonstrably live deploy -- downgrading an established fact, the
    mirror image of the unknown-is-not-pass rule the rest of this guard exists
    to uphold, and the one outcome that trains a reader to ignore the line. An
    *unknown* count is not zero: it falls through to the service guard, which
    refuses on its own terms. The earlier guards still speak first -- a dirty
    or off-branch primary reports its own reason however far behind it is.

    Returns `(ok, reason)`; on refusal `reason` is the text that reaches the
    finish summary as `PRIMARY=stale reason=<reason>`.
    """
    ok, reason = owner_check(holder, issue, dirty)
    if not ok:
        return False, reason
    if not current_branch:
        return False, f"primary is on a detached HEAD, not '{main_branch}'"
    if current_branch != main_branch:
        return False, f"primary is on '{current_branch}', not '{main_branch}'"
    if behind == 0:
        return True, (f"clean, on {main_branch}, claim {reason}, "
                      "already level with origin -- nothing to fast-forward")
    service_ok, service_reason = service
    if not service_ok:
        return False, service_reason
    return True, f"clean, on {main_branch}, claim {reason}, {service_reason}"


def format_primary_state(ok: bool, reason: str, behind: Optional[int] = None) -> str:
    """The one machine-readable line a worktree lane's finish summary carries.

    `PRIMARY=live behind=0`, or `PRIMARY=stale reason=<why>`. "Merged" and
    "live" are two separate facts: a lane that could not establish the second
    reports it as its own named state rather than letting silence imply
    success. An uncountable `behind` is likewise stale, never passing -- the
    fleet's unknown-is-not-pass rule.
    """
    if not ok:
        return f"PRIMARY=stale reason={reason}"
    if behind == 0:
        return "PRIMARY=live behind=0"
    if behind is None or behind < 0:
        return "PRIMARY=stale reason=could not count commits behind origin"
    return f"PRIMARY=stale reason=still {behind} behind after the fast-forward"


def try_acquire(
    lock_dir: Path,
    meta: dict,
    now: float,
    ttl_hours: float,
    branch_exists: Optional["Callable[[str], bool]"] = None,
) -> Tuple[str, dict]:
    """Atomically claim `lock_dir`. Returns ('primary', meta) on win, else
    ('worktree', holder-meta). Reclaims a stale lock; loses a reclaim race
    gracefully to worktree mode. Pure filesystem — hermetic, no git; the
    optional `branch_exists` predicate (passed straight to `is_stale`) is the
    only seam where the CLI injects git, so the tests stay hermetic.
    """
    if _publish_claim(lock_dir, meta):
        return "primary", meta
    holder = read_meta(lock_dir)
    if not is_stale(holder, now, ttl_hours, branch_exists):
        return "worktree", holder or {}
    # Stale -> reclaim. rmtree + re-publish isn't atomic as a pair, so a
    # concurrent reclaimer may win the re-publish; that racer's failed rename
    # below sends it to worktree mode. Net: exactly one ends up primary.
    shutil.rmtree(lock_dir, ignore_errors=True)
    if _publish_claim(lock_dir, meta):
        return "primary", meta
    return "worktree", read_meta(lock_dir) or {}
