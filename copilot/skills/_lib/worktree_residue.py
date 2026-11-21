"""Classify a repo's extra registered worktrees for `/cleanup-fleet-all` teardown.

Why this exists
----------------
Teardown's check 1 is not the bare rule "`git worktree list`
must show the primary only, anything else is residue, and residue halts the
whole run. That rule exists to stop the run building on its *own* dirt, and it
is wrong for a worktree the run never created.

A chief-dispatched lane can create a worktree in a repo after the scheduled
run's pre-flight. Pre-flight defers repos that already hold a worktree
(`repo_preflight.py`), but one that appears later is not covered. If it is
clean and its branch is already squash-merged, the bare rule would still call
it residue and halt the run with the rest of its issues never started.

This module is the classification teardown runs instead of reading the list
itself. Each registered worktree past the primary gets exactly one class:

  own               -- created by this run (see below). Residue, halts.
  foreign-dirty     -- someone else's, with uncommitted work. Residue, halts.
  foreign-unmerged  -- someone else's, clean, but its HEAD is not proven
                       merged into the default branch. Residue, halts.
  foreign-merged    -- someone else's, clean, HEAD proven merged. Reported;
                       the caller defers the rest of that repo's work.
  unknown           -- a fact could not be established. Halts, like residue.

**"Created by this run"** is decided from what the caller passes in, never
from the worktree itself: the run knows every worktree path and branch its own
lanes reported or would have created (`<repo>-wt-<N>` for every issue `N` it
holds for that repo), and passes them as `--own-worktree` / `--own-branch`. A
registered worktree whose path *or* checked-out branch matches any of them is
`own`. Everything else is foreign. The match errs toward `own` on purpose: a
coincidental hit only restores the old halt, which is safe; a miss would let
the run step past its own dirt, which is what the halt exists to prevent.

**"Merged"** needs positive proof, read offline from git alone against the
remote default branch (`origin/<default>`, fetched first; a failed fetch only
makes proof harder, which only makes the verdict more conservative):

  1. the worktree's HEAD is an ancestor of `origin/<default>`, or
  2. `git merge-tree --write-tree origin/<default> <HEAD>` produces exactly
     `origin/<default>`'s own tree -- merging it would change nothing, which
     is what a squash-merged branch looks like. (`git branch --merged` is
     useless here: the fleet squash-merges, so the original tip is never an
     ancestor.)

Neither proof → `foreign-unmerged`, i.e. today's halt. A branch whose squash
was later edited on the same lines conflicts in step 2 and halts too; that is
the conservative direction.

The overall verdict is `clean` (no extra worktree), `foreign-deferred` (every
extra worktree is `foreign-merged`), `residue` (any `own`, `foreign-dirty` or
`foreign-unmerged`), or `unknown`. Only `clean` and `foreign-deferred` pass
teardown's check 1.

This helper never removes, prunes, or touches a worktree -- a foreign one is
not the run's to remove.

Subcommand
----------
  classify <repo-path> [--own-worktree PATH]... [--own-branch NAME]... [--no-fetch]
        Prints one line per extra worktree:
          WORKTREE=<path> BRANCH=<name|(detached)> CLASS=<class> REASON=<why>
        then `VERDICT=<verdict>`, plus `REASON=` when the list itself could
        not be read and `NOTE=` when the fetch failed. Always exits 0 -- it
        reports, the caller decides.

The decision core (`is_own`, `classify_worktree`, `overall_verdict`) is pure
and unit-tested in `tests/repo/test_worktree_residue.py`, alongside real-git
fixtures for each class. stdlib only.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path
from typing import Callable, Iterable, List, NamedTuple, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_run  # noqa: E402

OWN = "own"
FOREIGN_DIRTY = "foreign-dirty"
FOREIGN_UNMERGED = "foreign-unmerged"
FOREIGN_MERGED = "foreign-merged"
UNKNOWN = "unknown"

# The one spelling of the sibling-worktree separator (`<repo>-wt-<N>`). A leaf
# module owns it so `worktree_config` (which `worktree_claim` imports) and every
# scanner below reach it without a cycle.
WT_SEP = "-wt-"

VERDICT_CLEAN = "clean"
VERDICT_FOREIGN_DEFERRED = "foreign-deferred"
VERDICT_RESIDUE = "residue"
VERDICT_UNKNOWN = "unknown"

_RESIDUE_CLASSES = (OWN, FOREIGN_DIRTY, FOREIGN_UNMERGED)


class WorktreeEntry(NamedTuple):
    path: str
    head: Optional[str]
    branch: Optional[str]  # None when detached
    prunable: bool


def parse_worktree_porcelain(porcelain: str) -> List[WorktreeEntry]:
    """Every worktree past the primary, from `git worktree list --porcelain`."""
    return parse_all_worktrees(porcelain)[1:]


def parse_all_worktrees(porcelain: str) -> List[WorktreeEntry]:
    """Every worktree, primary first, from `git worktree list --porcelain`.

    Records are blank-line separated; the first is always the primary.
    """
    entries: List[WorktreeEntry] = []
    for block in porcelain.replace("\r\n", "\n").split("\n\n"):
        path = head = branch = None
        prunable = False
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif line.startswith("HEAD "):
                head = line[len("HEAD "):].strip()
            elif line.startswith("branch "):
                ref = line[len("branch "):].strip()
                branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
            elif line.startswith("prunable"):
                prunable = True
        if path:
            entries.append(WorktreeEntry(path, head, branch, prunable))
    return entries


def normalize_path(p: str) -> str:
    """Compare Windows paths the way the filesystem does: separator- and
    case-insensitive, no trailing slash. `git worktree list` prints forward
    slashes; the workflow script passes backslashes."""
    return p.replace("\\", "/").rstrip("/").lower()


class DirFacts(NamedTuple):
    """Read-only facts about one folder; `None` means the fact could not be read,
    which no caller may take as a pass."""

    has_git: bool
    reparse: Optional[bool]
    empty: Optional[bool]  # None too for a reparse point, which is never listed


def is_reparse(path: Path) -> Optional[bool]:
    """Whether `path` is a junction/symlink itself; `None` when it cannot be stat'ed."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    attrs = getattr(st, "st_file_attributes", None)
    if attrs is None:  # non-Windows: a symlink is the nearest equivalent
        return stat.S_ISLNK(st.st_mode)
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def is_empty(path: Path) -> Optional[bool]:
    """Whether `path` has no top-level child (so no descendant either); `None` when unreadable."""
    try:
        with os.scandir(path) as it:
            return next(it, None) is None
    except OSError:
        return None


def dir_facts(path: Path) -> DirFacts:
    """The facts every `<repo>-wt-*` scanner judges a folder by. Never descends: a
    `.venv` junction inside must not be walked, and a junction itself is not listed."""
    reparse = is_reparse(path)
    return DirFacts((path / ".git").exists(), reparse, is_empty(path) if reparse is False else None)


def classify_orphan(has_git: bool, has_entries: Optional[bool]) -> Tuple[str, str]:
    """Class and reason for a folder named like a worktree that git does not register."""
    if has_git:
        return "orphan-with-git-file", "has a .git entry but git does not register it as a worktree"
    if has_entries is None:
        return UNKNOWN, "could not read the folder"
    if has_entries:
        return "orphan-has-content", "not a registered worktree, and not empty"
    return "orphan-empty", "not a registered worktree, and empty"


def unregistered_siblings(primary: Path, registered: Iterable[str]) -> List[Path]:
    """Every directory beside `primary` named `<primary-name>-wt-*` that git does not list.

    Any suffix counts, not only digits, and the match is per repo, never a
    fleet-wide `*-wt-*`: another repo's in-flight worktree is not this repo's
    leftover. Case-insensitive like the filesystem. An unreadable parent is `[]`.
    """
    prefix = primary.name.lower() + WT_SEP
    known = {normalize_path(p) for p in registered}
    try:
        siblings = sorted(primary.parent.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    return [sib for sib in siblings
            if sib.name.lower().startswith(prefix) and (sib.is_dir() or is_reparse(sib))
            and normalize_path(str(sib)) not in known]


def is_own(entry: WorktreeEntry, own_paths: Iterable[str], own_branches: Iterable[str]) -> bool:
    """True when this run created (or would have created) the worktree."""
    wanted = {normalize_path(p) for p in own_paths if p}
    if normalize_path(entry.path) in wanted:
        return True
    branches = {b[len("refs/heads/"):] if b.startswith("refs/heads/") else b
                for b in own_branches if b}
    return entry.branch is not None and entry.branch in branches


def classify_worktree(
    own: bool, dirty: Optional[bool], merged: Optional[bool], detail: str = ""
) -> Tuple[str, str]:
    """Pure class for one extra worktree. `None` means "could not establish".

    Own is decided first and never probed further: the run's own leftover is
    residue whatever state it is in. Dirty is checked before merged, so a
    merged branch carrying uncommitted work still halts.
    """
    if own:
        return OWN, "created by this run -- its own leftover, residue"
    if dirty is None:
        return UNKNOWN, detail or "could not read the worktree's status"
    if dirty:
        return FOREIGN_DIRTY, "not this run's, but has uncommitted changes"
    if merged is None:
        return UNKNOWN, detail or "could not establish whether its HEAD is merged"
    if not merged:
        return FOREIGN_UNMERGED, detail or "not this run's, clean, but no proof its HEAD is merged"
    return FOREIGN_MERGED, detail or "not this run's, clean, HEAD merged"


def overall_verdict(classes: Sequence[str]) -> str:
    """Residue beats unknown beats foreign-deferred; both halting verdicts halt."""
    if not classes:
        return VERDICT_CLEAN
    if any(c in _RESIDUE_CLASSES for c in classes):
        return VERDICT_RESIDUE
    if any(c == UNKNOWN for c in classes):
        return VERDICT_UNKNOWN
    return VERDICT_FOREIGN_DEFERRED


# ---- git plumbing ---------------------------------------------------------

def worktree_dirty(path: str) -> Tuple[Optional[bool], str]:
    try:
        porcelain = git_run.run_git_or_raise(Path(path), "status", "--porcelain")
    except git_run.Unreadable as exc:
        return None, str(exc)
    return porcelain != "", ""


def head_merged(repo: Path, head: Optional[str], default_ref: str) -> Tuple[Optional[bool], str]:
    """(merged, how) -- positive proof only; see the module docstring."""
    if not head:
        return None, "worktree has no HEAD commit"
    anc = git_run.run_git(["-C", str(repo), "merge-base", "--is-ancestor", head, default_ref])
    if anc.returncode == 0:
        return True, f"HEAD {head[:7]} is an ancestor of {default_ref}"
    if anc.returncode != 1:
        err = (anc.stderr or "").strip().splitlines()
        return None, f"merge-base --is-ancestor failed (exit {anc.returncode})" + (f": {err[0]}" if err else "")
    mt = git_run.run_git(["-C", str(repo), "merge-tree", "--write-tree", default_ref, head])
    if mt.returncode == 0:
        merged_tree = (mt.stdout or "").strip().splitlines()[:1]
        try:
            base_tree = git_run.run_git_or_raise(repo, "rev-parse", f"{default_ref}^{{tree}}")
        except git_run.Unreadable as exc:
            return None, str(exc)
        if merged_tree and merged_tree[0] == base_tree:
            return True, f"merging HEAD {head[:7]} into {default_ref} changes nothing (squash-merged)"
        return False, f"HEAD {head[:7]} carries changes not in {default_ref}"
    if mt.returncode == 1:
        return False, f"HEAD {head[:7]} conflicts with {default_ref} -- not proven merged"
    err = (mt.stderr or "").strip().splitlines()
    return None, f"merge-tree failed (exit {mt.returncode})" + (f": {err[0]}" if err else "")


def classify_repo(
    repo: Path,
    own_paths: Sequence[str],
    own_branches: Sequence[str],
    fetch: bool = True,
    dirty_fn: Callable[[str], Tuple[Optional[bool], str]] = worktree_dirty,
) -> Tuple[List[Tuple[WorktreeEntry, str, str]], str, str, str]:
    """Real git calls + classification. Never raises.

    Returns (rows, verdict, reason, note): one (entry, class, reason) row per
    extra worktree; `reason` is set only when the list itself was unreadable,
    `note` only when the fetch failed.
    """
    try:
        porcelain = git_run.run_git_or_raise(repo, "worktree", "list", "--porcelain")
    except git_run.Unreadable as exc:
        return [], VERDICT_UNKNOWN, str(exc), ""
    entries = parse_worktree_porcelain(porcelain)
    if not entries:
        return [], VERDICT_CLEAN, "", ""

    note = ""
    if fetch and any(not is_own(e, own_paths, own_branches) for e in entries):
        r = git_run.run_git(["-C", str(repo), "fetch", "origin"])
        if r.returncode != 0:
            note = f"git fetch origin failed (exit {r.returncode}) -- merge proof read against the last-fetched ref"
    default_ref = git_run.resolve_default_branch_ref(repo)

    rows: List[Tuple[WorktreeEntry, str, str]] = []
    for e in entries:
        own = is_own(e, own_paths, own_branches)
        if own:
            cls, why = classify_worktree(True, None, None)
        elif e.prunable:
            cls, why = classify_worktree(False, None, None, "registered but its directory is gone (prunable)")
        else:
            dirty, dirty_err = dirty_fn(e.path)
            merged, how = (None, "") if dirty is not False else head_merged(repo, e.head, default_ref)
            cls, why = classify_worktree(False, dirty, merged, dirty_err or how)
        rows.append((e, cls, why))
    return rows, overall_verdict([c for _, c, _ in rows]), "", note


def cmd_classify(repo: Path, own_paths: List[str], own_branches: List[str], fetch: bool) -> None:
    if not repo.exists():
        print("VERDICT=unknown")
        print(f"REASON=no such path: {repo}")
        return
    rows, verdict, reason, note = classify_repo(repo, own_paths, own_branches, fetch)
    for e, cls, why in rows:
        print(f"WORKTREE={e.path} BRANCH={e.branch or '(detached)'} CLASS={cls} REASON={why}")
    print(f"VERDICT={verdict}")
    if reason:
        print(f"REASON={reason}")
    if note:
        print(f"NOTE={note}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Classify a repo's extra registered worktrees for /cleanup-fleet-all teardown."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("classify")
    c.add_argument("repo_path", type=Path)
    c.add_argument("--own-worktree", action="append", default=[])
    c.add_argument("--own-branch", action="append", default=[])
    c.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "classify":
        cmd_classify(args.repo_path, args.own_worktree, args.own_branch, not args.no_fetch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
