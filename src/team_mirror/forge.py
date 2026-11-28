"""Opening and merging the mirror PR, chosen from the team URL's host.

* ``github`` (github.com): ``gh pr create`` then ``gh pr merge --squash``.
* ``gitlab`` (any other https/ssh host): ``glab mr create`` then ``glab mr merge --squash``.
* ``local`` (a path or ``file://`` URL): no PR exists, so "Publish and merge"
  pushes the commit straight to the team branch and "Open PR only" leaves it
  on the ``mirror/<date>`` branch.

Sign-in is the CLI's own; Lite passes no token. A merge the host refuses (a
protected branch, a required review) is not an error: the result says the PR
is open and carries its link.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import List, Sequence
from urllib.parse import urlparse

from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

GITHUB, GITLAB, LOCAL = "github", "gitlab", "local"
# Creating or merging a PR is one API round-trip, slower on a cold CLI.
_CLI_TIMEOUT = 90.0
_SCP_RE = re.compile(r"^[\w.-]+@(?P<host>[\w.-]+):(?P<path>.+)$")
_URL_RE = re.compile(r"https?://\S+")


class ForgeError(RuntimeError):
    """The PR could not be opened; the message is the one the page shows."""


@dataclass(frozen=True)
class Remote:
    kind: str
    host: str = ""
    path: str = ""  # owner/repo (GitHub) or group/sub/project (GitLab)


@dataclass(frozen=True)
class PrResult:
    url: str
    merged: bool
    note: str = ""


def remote_of(team_url: str) -> Remote:
    """Which forge serves ``team_url``, and the repo path its CLI names."""
    scp = _SCP_RE.match(team_url)
    if scp:
        host, path = scp.group("host"), scp.group("path")
    else:
        parsed = urlparse(team_url)
        if parsed.scheme in ("", "file") or len(parsed.scheme) == 1:  # a path, or a drive letter
            return Remote(LOCAL)
        host, path = parsed.hostname or "", parsed.path
    path = path.strip("/")
    path = path[:-4] if path.endswith(".git") else path
    kind = GITHUB if host.lower() == "github.com" else GITLAB
    return Remote(kind, host, path)


def _run(argv: Sequence[str], *, host: str = "") -> str:
    env = {**os.environ, "GITLAB_HOST": host} if host else None
    try:
        proc = subprocess.run(
            list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=_CLI_TIMEOUT, env=env, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ForgeError(f"{argv[0]} could not run: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        lines = [l.strip() for l in (proc.stderr or proc.stdout or "").splitlines() if l.strip()]
        raise ForgeError(f"{argv[0]} {argv[1]} {argv[2]}: {lines[-1] if lines else 'exit ' + str(proc.returncode)}")
    return proc.stdout


def _pr_url(out: str) -> str:
    urls: List[str] = _URL_RE.findall(out)
    if not urls:
        raise ForgeError("the PR was created but its link was not printed")
    return urls[-1]


def open_and_merge(remote: Remote, head: str, base: str, title: str, body: str, *, merge: bool) -> PrResult:
    """Open the PR from ``head`` into ``base`` and, with ``merge``, squash-merge it."""
    if remote.kind == GITHUB:
        url = _pr_url(_run(["gh", "pr", "create", "--repo", remote.path, "--head", head, "--base", base,
                            "--title", title, "--body", body]))
        merge_argv = ["gh", "pr", "merge", url, "--squash", "--delete-branch"]
    elif remote.kind == GITLAB:
        url = _pr_url(_run(["glab", "mr", "create", "--repo", remote.path, "--source-branch", head,
                            "--target-branch", base, "--title", title, "--description", body, "--yes"],
                           host=remote.host))
        merge_argv = ["glab", "mr", "merge", url, "--repo", remote.path, "--squash", "--yes",
                      "--remove-source-branch"]
    else:
        raise ForgeError("a local team repo has no PRs")
    logger.info("✅ team mirror: opened %s", url)
    if not merge:
        return PrResult(url, merged=False)
    try:
        _run(merge_argv, host=remote.host if remote.kind == GITLAB else "")
    except ForgeError as exc:
        logger.warning("⚠️ team mirror: the host did not merge %s (%s)", url, exc)
        return PrResult(url, merged=False, note=f"the host did not allow merging from here: {exc}")
    logger.info("✅ team mirror: merged %s", url)
    return PrResult(url, merged=True)

