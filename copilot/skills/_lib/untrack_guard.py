"""Keep the live copy of a file a fast-forward untracks.

Why this exists
----------------
A merged change that untracks a machine-local file -- `git rm --cached` plus a
`.gitignore` line, or a rename to `<name>.sample` -- is correct for the repo,
and it still deletes the file from every checkout that fast-forwards over it.
Git removes a working-tree file whose path the incoming commit drops from the
index, even when that path is ignored afterwards. Nothing warns: the pull exits
0 and the tree reads clean, because the file is now both absent and ignored.

A lane that untracks live JSON configs therefore loses them from the primary
at its `land-primary` fast-forward, and the next nightly job fails on a missing
config. Neither the gate nor the reviewer can see it: the lane builds in a
worktree, and the loss only happens at the primary's fast-forward.

What it does
------------
`guarded_fast_forward(repo, target)` is a `git merge --ff-only` to a resolved
commit, with one guard around it:

  1. list the paths the incoming range deletes or renames away
     (`git diff --name-status -M --diff-filter=DR HEAD..<target>`) that exist
     in the working tree, and copy each aside;
  2. fast-forward;
  3. put back every preserved path that is now gone **and ignored**.

A path the merge deletes but does not ignore stays deleted: that is a tracked
deletion the merge intends, and restoring it would leave the tree dirty. A path
whose ignore state cannot be established is neither restored nor lost -- it is
reported as kept aside, with the stash directory, for a human to decide
(unknown is never folded into either passing state). A range that cannot be
listed refuses the fast-forward outright: landing blind is the exact failure
this guards against.

Every restored path is reported, never restored silently -- `land-primary`,
the `/cleanup-fleet-all` teardown and `/issue-finish` carry it into their
results as `restoredUntracked`.

CLI
---
  untrack_guard.py fast-forward <repo> [--ref <ref>] [--no-fetch]

fetches the ref's remote (unless `--no-fetch`), then runs the guarded
fast-forward to `--ref` (default: the current branch's upstream, `@{u}`) and
prints:

  FF=done before=<sha> after=<sha>      or   FF=refused reason=<why>
  RESTORED_UNTRACKED=none               or   RESTORED_UNTRACKED=<path>,<path>
  KEPT_ASIDE=<path> stash=<dir>         (one line per path not put back)

Exit 0: fast-forwarded, nothing kept aside. 1: refused, tree untouched.
3: fast-forwarded, but at least one preserved file is only in the stash.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_run  # noqa: E402
from fleet_core import runtime_files  # noqa: E402
from fleet_core.utf8_stdio import ensure_utf8_stdio  # noqa: E402


@dataclass
class GuardedFastForward:
    ok: bool
    detail: str = ""
    before: str = ""
    after: str = ""
    restored: List[str] = field(default_factory=list)
    kept_aside: List[str] = field(default_factory=list)
    stash: Optional[Path] = None   # kept on disk only while `kept_aside` is non-empty


def parse_removed_paths(name_status_z: str) -> List[str]:
    """Old paths of the `D` and `R` rows in `git diff --name-status -z` output.

    `-z` output is a NUL-separated token stream: a status token, then two paths
    for a rename (`R<score>`, old then new) and one for anything else. Only the
    old path is removed from the working tree, so only it is returned.
    """
    tokens = name_status_z.split("\0")
    removed: List[str] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        paths = 2 if status.startswith("R") else 1
        if status[0] in "DR" and i + 1 < len(tokens):
            removed.append(tokens[i + 1])
        i += 1 + paths
    return removed


def _git(repo: Path, *args: str):
    return git_run.run_git(["-C", str(repo), *args])


def _rev(repo: Path, ref: str) -> Optional[str]:
    r = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    out = (r.stdout or "").strip()
    return out if r.returncode == 0 and out else None


def removed_paths(repo: Path, target: str) -> Optional[List[str]]:
    """Paths the range HEAD..target deletes or renames away, or None if unlistable."""
    r = _git(repo, "diff", "--name-status", "-z", "-M", "--diff-filter=DR", f"HEAD..{target}")
    if r.returncode != 0:
        return None
    return parse_removed_paths(r.stdout or "")


def is_ignored(repo: Path, rel: str) -> Optional[bool]:
    """True/False from `git check-ignore --no-index`; None when git cannot say."""
    r = _git(repo, "check-ignore", "-q", "--no-index", "--", rel)
    return {0: True, 1: False}.get(r.returncode)


def _first_line(proc) -> str:
    lines = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
    return lines[0] if lines else "?"


def guarded_fast_forward(repo: Path, target: str) -> GuardedFastForward:
    """Fast-forward `repo` to `target`, keeping every live file the range untracks."""
    repo = Path(repo)
    sha = _rev(repo, target)
    if sha is None:
        return GuardedFastForward(False, f"cannot resolve {target}")
    before = _rev(repo, "HEAD") or ""
    candidates = removed_paths(repo, sha)
    if candidates is None:
        return GuardedFastForward(False, f"cannot list the paths HEAD..{target} removes")
    live = [p for p in candidates if (repo / p).is_file()]

    stash: Optional[Path] = None
    if live:
        stash = Path(tempfile.mkdtemp(prefix="untrack-guard-"))
        try:
            for rel in live:
                dst = stash / rel
                runtime_files.make_dirs(dst.parent, parents=True, exist_ok=True)
                runtime_files.copy_with_metadata(repo / rel, dst)
        except OSError as exc:
            shutil.rmtree(stash, ignore_errors=True)
            return GuardedFastForward(False, f"could not preserve {rel}: {exc}")

    merge = _git(repo, "merge", "--ff-only", "--quiet", sha)
    if merge.returncode != 0:
        if stash:
            shutil.rmtree(stash, ignore_errors=True)
        return GuardedFastForward(False, f"merge --ff-only failed: {_first_line(merge)}", before=before)

    result = GuardedFastForward(True, before=before, after=_rev(repo, "HEAD") or "")
    for rel in live:
        dst = repo / rel
        if dst.exists():
            continue                  # the merge kept or re-added it: nothing was lost
        ignored = is_ignored(repo, rel)
        if ignored is False:
            continue                  # a tracked deletion the merge intends
        if ignored is None:
            result.kept_aside.append(rel)
            continue
        try:
            # runtime-write-exempt: restores a live file the merge untracked into the repo, which may be this one's code folders
            dst.parent.mkdir(parents=True, exist_ok=True)
            # runtime-write-exempt: restores a live file the merge untracked into the repo, which may be this one's code folders
            shutil.copy2(stash / rel, dst)
            result.restored.append(rel)
        except OSError:
            result.kept_aside.append(rel)
    if stash:
        if result.kept_aside:
            result.stash = stash
        else:
            shutil.rmtree(stash, ignore_errors=True)
    return result


def report_lines(ff: GuardedFastForward) -> List[str]:
    """The restore report, or [] when the fast-forward untracked nothing live."""
    lines = []
    if ff.restored:
        lines.append(f"RESTORED_UNTRACKED={','.join(ff.restored)}")
    lines.extend(f"KEPT_ASIDE={rel} stash={ff.stash}" for rel in ff.kept_aside)
    return lines


def cmd_fast_forward(repo: Path, ref: str, fetch: bool) -> int:
    if fetch:
        remote = ref.split("/", 1)[0] if "/" in ref and not ref.startswith("@") else ""
        fetched = _git(repo, "fetch", "--quiet", *([remote] if remote else []))
        if fetched.returncode != 0:
            print(f"FF=refused reason=fetch failed: {_first_line(fetched)}")
            print("RESTORED_UNTRACKED=none")
            return 1
    ff = guarded_fast_forward(repo, ref)
    if not ff.ok:
        print(f"FF=refused reason={ff.detail}")
        print("RESTORED_UNTRACKED=none")
        return 1
    print(f"FF=done before={ff.before[:7]} after={ff.after[:7]}")
    print(f"RESTORED_UNTRACKED={','.join(ff.restored) or 'none'}")
    for rel in ff.kept_aside:
        print(f"KEPT_ASIDE={rel} stash={ff.stash}")
    return 3 if ff.kept_aside else 0


def main(argv: Optional[List[str]] = None) -> int:
    ensure_utf8_stdio()
    ap = argparse.ArgumentParser(
        description="Fast-forward a checkout, keeping every live file the incoming range untracks."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fast-forward")
    f.add_argument("repo_path", type=Path)
    f.add_argument("--ref", default="@{u}")
    f.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args(argv)
    return cmd_fast_forward(args.repo_path, args.ref, not args.no_fetch)


if __name__ == "__main__":
    raise SystemExit(main())
