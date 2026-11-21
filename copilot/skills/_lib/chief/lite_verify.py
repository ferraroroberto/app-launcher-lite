"""`chief_ops.py verify`: re-check a lane's self-reported end state on GitLab.

fleet-config's `verify` wraps `dirty_tree_check` (`--expect merged|built`). The
lite chief ships through GitLab merge requests, so its two end states name the
merge request:

  mr-open  the lane pushed its branch and opened a merge request: the checkout is
           clean, and GitLab shows an open merge request from `--branch`
           (default: the checkout's current branch).
  merged   the lane merged: the checkout is clean and back on the default
           branch, and GitLab shows the merge request from `--branch` merged.

Prints the upstream lines, `STATUS=CLEAN|DIRTY|UNKNOWN`, `BRANCH=<current>`, and
`REASON=` when not clean; exit 0 only on CLEAN. A fact that cannot be gathered (a
repo that does not resolve, a git that fails, glab missing or signed out) is
`UNKNOWN`, never folded into `DIRTY` or `CLEAN`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import gitlab_namespace
from git_run import run_git

from . import launcher

EXPECTS = ("mr-open", "merged")
REPOS_ROOT = launcher.REPOS_ROOT
Glab = Callable[[List[str]], subprocess.CompletedProcess]


class Unknown(Exception):
    """A fact `verify` needs could not be established."""


def resolve_repo_path(repo: str, root: Path = REPOS_ROOT) -> Path:
    """A literal existing directory, else the sibling checkout named `repo`."""
    for candidate in (Path(repo), root / repo):
        if candidate.is_dir():
            return candidate.resolve()
    raise Unknown(f"unknown repo: {repo!r} (not a directory, no sibling checkout under {root})")


GIT_TIMEOUT = 30.0


def _git(repo_path: Path, *args: str) -> str:
    try:
        res = run_git(["-C", str(repo_path), *args], timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise Unknown(f"git {' '.join(args)} timed out") from exc
    if res.returncode != 0:
        raise Unknown(f"git {' '.join(args)} failed: {(res.stderr or '').strip()[:200]}")
    return res.stdout.strip()


def tree_facts(repo_path: Path, default_branch: Optional[str]) -> Tuple[str, bool, str]:
    """`(current branch, porcelain empty, default branch)`."""
    current = _git(repo_path, "branch", "--show-current")
    clean = _git(repo_path, "status", "--porcelain") == ""
    if not default_branch:
        ref = _git(repo_path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        default_branch = ref.rsplit("/", 1)[-1]
    return current, clean, default_branch


def merge_requests(project: str, branch: str, glab: Glab) -> List[Dict[str, Any]]:
    """Every merge request from `branch` in any state, or Unknown when glab can't say."""
    try:
        res = glab(["mr", "list", "--repo", project, "--source-branch", branch, "--all",
                    "--output", "json"])
    except FileNotFoundError as exc:
        raise Unknown("glab not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise Unknown("glab timed out") from exc
    if res.returncode != 0:
        raise Unknown(f"glab mr list failed: {(res.stderr or '').strip()[:200] or 'no detail'}")
    try:
        data = json.loads(res.stdout or "[]")
    except ValueError as exc:
        raise Unknown("unparseable glab mr list output") from exc
    if not isinstance(data, list):
        raise Unknown("glab mr list output was not a list")
    return [mr for mr in data if isinstance(mr, dict)]


def evaluate(expect: str, current: str, clean: bool, default_branch: str,
             mr_states: List[str], branch: str) -> Tuple[str, Optional[str]]:
    """`(status, reason)` from gathered facts; pure."""
    if not clean:
        return "DIRTY", "uncommitted changes in the checkout"
    if expect == "merged":
        if current != default_branch:
            return "DIRTY", f"on {current or '(detached)'}, not back on {default_branch}"
        if "merged" not in mr_states:
            return "DIRTY", f"no merged merge request from {branch} (states: {', '.join(mr_states) or 'none'})"
        return "CLEAN", None
    if "opened" not in mr_states:
        return "DIRTY", f"no open merge request from {branch} (states: {', '.join(mr_states) or 'none'})"
    return "CLEAN", None


def _owner(args: argparse.Namespace, repo_path: Path) -> str:
    """`--owner`, else the launcher's `gitlab_group`, else the checkout's `origin` namespace."""
    owner, why = (args.owner, "") if args.owner else gitlab_namespace.resolve(repo_path)
    if owner is None:
        raise Unknown(f"{launcher.OWNER_UNKNOWN} ({why})")
    return owner


def run_verify(args: argparse.Namespace, glab: Glab = launcher.run_glab) -> Tuple[str, str, Optional[str]]:
    """`(status, current branch, reason)` for one `verify` call."""
    current = ""
    try:
        repo_path = resolve_repo_path(args.repo)
        current, clean, default_branch = tree_facts(repo_path, args.default_branch)
        branch = args.branch or (current if args.expect == "mr-open" else "")
        if not branch:
            raise Unknown("--branch is needed to find the lane's merge request")
        project = args.project or f"{_owner(args, repo_path)}/{repo_path.name}"
        states = [str(mr.get("state") or "") for mr in merge_requests(project, branch, glab)]
    except Unknown as exc:
        return "UNKNOWN", current, str(exc)
    status, reason = evaluate(args.expect, current, clean, default_branch, states, branch)
    return status, current, reason


def cmd_verify(args: argparse.Namespace) -> int:
    status, branch, reason = run_verify(args)
    print(f"STATUS={status}")
    print(f"BRANCH={branch}")
    if reason:
        print(f"REASON={reason}")
    return 0 if status == "CLEAN" else 1


def add_parser(sub: argparse._SubParsersAction) -> None:
    v = sub.add_parser("verify", help="re-check a lane's end state: clean tree plus its GitLab merge request")
    v.add_argument("repo", help="a sibling repo name or a checkout path")
    v.add_argument("--expect", choices=EXPECTS, required=True)
    v.add_argument("--branch", default=None, help="the lane's branch (mr-open defaults to the current one)")
    v.add_argument("--default-branch", default=None)
    v.add_argument("--owner", default=None, help="GitLab namespace (default: the launcher's gitlab_group)")
    v.add_argument("--project", default=None, help="full GitLab path; overrides <owner>/<repo>")
    v.set_defaults(func=cmd_verify)
