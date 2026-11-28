"""Git plumbing for a mirror, all of it inside Lite's own scratch repo.

One scratch repo per project lives under the runtime data root. It fetches
the personal main from the live app folder (read-only: a fetch *from* a repo
never writes to it) and the team main from the team URL, builds the snapshot
tree in its own index, and commits it with ``commit-tree``. The live app
folder is never checked out, committed to or pushed from.

The scratch repo's work tree holds exactly the snapshot's files, so the scan
reads them from disk. ``read-tree -u --reset`` keeps it in step with each new
tree and ``git rm`` takes the leave-out paths out of both index and disk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from src import runtime_files
from src.git_utils import resolve_default_ref, run_git, run_git_proc
from src.runtime_data import runtime_data_dir
from src.team_mirror.links import Identity

logger = logging.getLogger(__name__)

PERSONAL_REF = "refs/mirror/personal"
TEAM_REF = "refs/mirror/team"

# A fetch or push crosses the network to the team host; a local plumbing call
# over a large tree still takes seconds on a slow disk.
NETWORK_TIMEOUT = 120.0
LOCAL_TIMEOUT = 60.0

# The personal main as Lite pulls it: the remote-tracking main first, so an
# unpushed local commit is never published, then the local branch for a repo
# with no remote.
_PERSONAL_REFS = ("origin/main", "origin/master", "main", "master")

# Fixed scratch config: no line-ending conversion, so the scanned files are
# the blobs' own bytes, and no background GC in a repo Lite owns.
_SCRATCH_CONFIG = (
    ("core.autocrlf", "false"),
    ("core.safecrlf", "false"),
    ("gc.auto", "0"),
)


class SnapshotError(RuntimeError):
    """A git step failed; the message is the one the page shows."""


@dataclass(frozen=True)
class Change:
    status: str  # A, M or D
    path: str


def scratch_root() -> Path:
    return runtime_data_dir("app-launcher-lite") / "team-mirror"


def scratch_repo(project: str, root: Optional[Path] = None) -> Path:
    """The project's scratch repo, made on first use."""
    base = root or scratch_root()
    repo = base / project
    if (repo / ".git").is_dir():
        return repo
    runtime_files.make_dirs(repo)
    if run_git(repo, ["init", "-q"], timeout=LOCAL_TIMEOUT, warn_on_failure=True) is None:
        raise SnapshotError("could not create the scratch repo for this app")
    for key, value in _SCRATCH_CONFIG:
        run_git(repo, ["config", key, value], timeout=LOCAL_TIMEOUT)
    return repo


def _git(repo: Path, args: Sequence[str], what: str, *, timeout: float = LOCAL_TIMEOUT,
         network: bool = False) -> str:
    """stdout of one git step; :class:`SnapshotError` with ``what`` and git's first stderr line."""
    proc = run_git_proc(repo, args, timeout=timeout, no_prompt=network, log=logger.warning)
    if proc is None:
        raise SnapshotError(f"{what}: git did not run or timed out")
    if proc.returncode != 0:
        lines = [l for l in (proc.stderr or "").splitlines() if l.strip()]
        logger.warning("⚠️ team mirror: %s failed (exit %s)", what, proc.returncode)
        raise SnapshotError(f"{what}: {lines[-1].strip() if lines else 'git exited ' + str(proc.returncode)}")
    return proc.stdout.strip()


def personal_ref(project_dir: Path) -> str:
    """The full ref name of the personal main in the live app folder."""
    ref = resolve_default_ref(project_dir, fallback_refs=_PERSONAL_REFS)
    if not ref:
        raise SnapshotError("the app folder has no main branch to publish")
    return f"refs/remotes/{ref}" if ref.startswith("origin/") else f"refs/heads/{ref}"


def fetch_personal(repo: Path, project_dir: Path) -> str:
    ref = personal_ref(project_dir)
    _git(repo, ["fetch", "--no-tags", "--quiet", str(project_dir), f"+{ref}:{PERSONAL_REF}"],
         "reading the personal main", timeout=NETWORK_TIMEOUT)
    return rev(repo, PERSONAL_REF)


def fetch_team(repo: Path, team_url: str, branch: str) -> str:
    _git(repo, ["fetch", "--no-tags", "--quiet", team_url, f"+refs/heads/{branch}:{TEAM_REF}"],
         f"fetching the team repo's {branch}", timeout=NETWORK_TIMEOUT, network=True)
    return rev(repo, TEAM_REF)


def rev(repo: Path, ref: str) -> str:
    return _git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], f"resolving {ref}")


def tree_of(repo: Path, commit: str) -> str:
    return _git(repo, ["rev-parse", f"{commit}^{{tree}}"], "reading a commit's tree")


def build_tree(repo: Path, personal_sha: str, leave_out: Sequence[str]) -> str:
    """The personal tree minus ``leave_out``, checked out in the scratch work tree; its tree id."""
    _git(repo, ["read-tree", "-u", "--reset", personal_sha], "laying out the personal tree")
    if leave_out:
        _git(repo, ["rm", "-r", "-q", "-f", "--ignore-unmatch", "--", *leave_out], "dropping the leave-out paths")
    return _git(repo, ["write-tree"], "writing the snapshot tree")


def tracked_files(repo: Path) -> List[str]:
    out = _git(repo, ["ls-files", "-z"], "listing the snapshot's files")
    return [p for p in out.split("\0") if p]


def changes(repo: Path, team_sha: str, tree: str) -> List[Change]:
    """What the snapshot adds, modifies and deletes against the team main."""
    out = _git(repo, ["diff-tree", "-r", "-z", "--no-renames", "--name-status", team_sha, tree],
               "comparing with the team main")
    parts = [p for p in out.split("\0") if p]
    return [Change(parts[i][0], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    proc = run_git_proc(repo, ["merge-base", "--is-ancestor", older, newer], timeout=LOCAL_TIMEOUT)
    return proc is not None and proc.returncode == 0


def has_commit(repo: Path, sha: str) -> bool:
    proc = run_git_proc(repo, ["cat-file", "-e", f"{sha}^{{commit}}"], timeout=LOCAL_TIMEOUT)
    return proc is not None and proc.returncode == 0


def subjects_between(repo: Path, older: Optional[str], newer: str, limit: int = 50) -> List[str]:
    """Commit subjects in ``older..newer``, newest first; all of ``newer``'s history when ``older`` is None."""
    span = f"{older}..{newer}" if older else newer
    out = _git(repo, ["log", f"--max-count={limit}", "--format=%s", span], "reading commit subjects")
    return [s for s in out.splitlines() if s.strip()]


def count_between(repo: Path, older: str, newer: str) -> int:
    return int(_git(repo, ["rev-list", "--count", f"{older}..{newer}"], "counting commits") or 0)


def team_commits_since(repo: Path, recorded: Optional[str], team_sha: str) -> List[Tuple[str, str]]:
    """Team-main commits this computer did not publish: (short sha, subject), newest first.

    With no record, or a record the team main no longer contains, every team
    commit counts, since none of them is known to be a mirror commit.
    """
    known = recorded if recorded and has_commit(repo, recorded) and is_ancestor(repo, recorded, team_sha) else None
    if known == team_sha:
        return []
    span = f"{known}..{team_sha}" if known else team_sha
    out = _git(repo, ["log", "--max-count=20", "--format=%h%x09%s", span], "reading team-side commits")
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]  # type: ignore[misc]


def make_commit(repo: Path, tree: str, parent: str, identity: Identity, message: str) -> str:
    """One commit with ``tree`` on ``parent``, author and committer both ``identity``."""
    return _git(
        repo,
        ["-c", f"user.name={identity.name}", "-c", f"user.email={identity.email}",
         "-c", "commit.gpgSign=false", "commit-tree", tree, "-p", parent, "-m", message],
        "making the snapshot commit",
    )


def push(repo: Path, team_url: str, sha: str, branch: str) -> None:
    """Push ``sha`` to ``branch`` on the team repo. Never forced: a moved branch refuses."""
    _git(repo, ["push", "--quiet", "--no-verify", team_url, f"{sha}:refs/heads/{branch}"],
         f"pushing to the team repo's {branch}", timeout=NETWORK_TIMEOUT, network=True)


def check_access(repo: Path, team_url: str, branch: str) -> Optional[str]:
    """None when the team repo answers and has ``branch``; else what is wrong, in the page's words."""
    proc = run_git_proc(repo, ["ls-remote", "--heads", team_url, f"refs/heads/{branch}"],
                        timeout=NETWORK_TIMEOUT, no_prompt=True)
    if proc is None:
        return "git did not answer in time: the host may be unreachable"
    if proc.returncode != 0:
        return access_problem(proc.stderr or "")
    if not proc.stdout.strip():
        return f"the team repo answers, but has no branch named {branch}"
    return None


def access_problem(stderr: str) -> str:
    """git's fetch error as one plain sentence; a different sentence for each condition."""
    text = stderr.lower()
    if "could not resolve host" in text:
        return "the host name does not resolve: check the URL"
    if any(s in text for s in ("authentication failed", "could not read username", "terminal prompts disabled",
                               "permission denied", "403")):
        return "the sign-in was refused or is missing: this account has no access to the team repo"
    if any(s in text for s in ("not found", "does not appear to be a git repository", "does not exist")):
        return "no repository at that URL, or it is private to this account"
    if "timed out" in text or "connection refused" in text:
        return "the host did not answer: check the network or the URL"
    lines = [l.strip() for l in stderr.splitlines() if l.strip()]
    return f"git could not read the team repo: {lines[-1] if lines else 'no detail'}"
