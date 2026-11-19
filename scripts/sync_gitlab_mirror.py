"""Keep the GitLab mirror of this repo level with GitHub ``origin``.

gitlab.com's free tier has no pull mirroring, so this is the mirror: fetch
``origin``, then push its ``main`` and tags to the ``gitlab`` remote, fast-forward
only. It never forces. If GitLab's ``main`` holds a commit GitHub lacks, it
stops and says so rather than overwrite it.

Run from anywhere (``python scripts/sync_gitlab_mirror.py``); as a launcher Job
it needs no arguments. Exit codes: 0 level or pushed, 1 refused (the mirror
diverged, or a tag differs), 2 not established (a fetch, a lookup or a push
failed for a reason other than divergence).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.git_utils import run_git_proc  # noqa: E402

logger = logging.getLogger("sync_gitlab_mirror")

EXIT_OK, EXIT_REFUSED, EXIT_UNKNOWN = 0, 1, 2
# A network call; generous because a first push uploads the whole history.
_NET_TIMEOUT_SECONDS = 180.0
_LOCAL_TIMEOUT_SECONDS = 20.0


class _Outcome(Exception):
    """A terminal result: carries the exit code and the message to print."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _git(repo: Path, args: Sequence[str], *, net: bool = False):
    proc = run_git_proc(
        repo, args, timeout=_NET_TIMEOUT_SECONDS if net else _LOCAL_TIMEOUT_SECONDS,
        no_prompt=True,
    )
    if proc is None:
        raise _Outcome(EXIT_UNKNOWN, f"❌ git {args[0]} could not run (git missing or timed out)")
    return proc


def _detail(proc) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {proc.returncode}"


def _mirror_head(repo: Path, mirror: str, branch: str) -> Optional[str]:
    """The sha GitLab's ``branch`` points at, or ``None`` when the project has none yet."""
    proc = _git(repo, ["ls-remote", "--heads", mirror, f"refs/heads/{branch}"], net=True)
    if proc.returncode != 0:
        raise _Outcome(EXIT_UNKNOWN, f"❌ could not read {mirror}: {_detail(proc)}")
    out = proc.stdout.strip()
    return out.split()[0] if out else None


def sync(repo: Path, source: str = "origin", mirror: str = "gitlab", branch: str = "main") -> str:
    """Level ``mirror`` with ``source``; returns a one-line report or raises :class:`_Outcome`."""
    fetched = _git(repo, ["fetch", source, "--tags"], net=True)
    if fetched.returncode != 0:
        raise _Outcome(EXIT_UNKNOWN, f"❌ fetch {source} failed: {_detail(fetched)}")
    source_ref = f"refs/remotes/{source}/{branch}"
    head = _git(repo, ["rev-parse", "--verify", "--quiet", source_ref])
    if head.returncode != 0:
        raise _Outcome(EXIT_UNKNOWN, f"❌ {source}/{branch} not found after the fetch")
    source_sha = head.stdout.strip()

    mirror_sha = _mirror_head(repo, mirror, branch)
    if mirror_sha and mirror_sha != source_sha:
        # The mirror's commits must be in hand to tell "behind" from "diverged".
        got = _git(repo, ["fetch", mirror, f"refs/heads/{branch}"], net=True)
        if got.returncode != 0:
            raise _Outcome(EXIT_UNKNOWN, f"❌ fetch {mirror} failed: {_detail(got)}")
        ancestor = _git(repo, ["merge-base", "--is-ancestor", mirror_sha, source_sha])
        if ancestor.returncode == 1:
            raise _Outcome(
                EXIT_REFUSED,
                f"⚠️ refused: {mirror}/{branch} ({mirror_sha[:9]}) has commits {source}/{branch} "
                f"({source_sha[:9]}) lacks. Nothing was pushed; reconcile on {source} first.",
            )
        if ancestor.returncode != 0:
            raise _Outcome(EXIT_UNKNOWN, f"❌ could not compare the two {branch} heads: {_detail(ancestor)}")

    pushed = []
    if mirror_sha != source_sha:
        push = _git(repo, ["push", mirror, f"{source_ref}:refs/heads/{branch}"], net=True)
        if push.returncode != 0:
            raise _Outcome(EXIT_UNKNOWN, f"❌ push {branch} to {mirror} failed: {_detail(push)}")
        pushed.append(branch)
    tags = _git(repo, ["push", mirror, "refs/tags/*:refs/tags/*"], net=True)
    if tags.returncode != 0:
        # A tag already on the mirror at another commit is a refusal, not a retry.
        code = EXIT_REFUSED if "rejected" in (tags.stderr or "") else EXIT_UNKNOWN
        raise _Outcome(code, f"⚠️ pushing tags to {mirror} failed: {_detail(tags)}")
    if "new tag" in (tags.stderr or ""):
        pushed.append("tags")
    if not pushed:
        return f"✅ {mirror}/{branch} already at {source_sha[:9]}"
    return f"✅ pushed {' and '.join(pushed)} to {mirror}; {branch} at {source_sha[:9]}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, default=PROJECT_ROOT, help="checkout to sync from")
    parser.add_argument("--source", default="origin", help="remote that is the source of truth")
    parser.add_argument("--mirror", default="gitlab", help="remote that receives the push")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        logger.info(sync(args.repo, args.source, args.mirror, args.branch))
    except _Outcome as outcome:
        logger.error(str(outcome))
        return outcome.code
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
