"""The one ``git`` invocation contract for this repo.

Every ``git -C <root> …`` shell-out here needs the same kwargs — capture the
output, close stdin so a git that wants to prompt fails instead of hanging,
decode as text, cap the wall clock, never raise on a non-zero exit, and pass
``creationflags=NO_WINDOW`` so no console flashes under the console-less
``pythonw`` tray, ``GIT_OPTIONAL_LOCKS=0`` so a killed read can't strand
``index.lock`` — plus the same ``except (OSError, subprocess.SubprocessError)``
arm around it. Named once here for :mod:`src.build_info`,
:mod:`src.session_host_paths` and :mod:`src.scanner`, so every caller warns or
degrades the same way on a failed run, and changing the timeout or the exception
set is a one-line edit.

The split of responsibility is deliberate:

* **This module** owns *running* git. It returns ``None`` only when the
  process never produced a result at all (git missing from PATH, a timeout, an
  OS-level spawn failure) and logs that once, with the command and the root.
* **Call sites** own *interpreting* the result — a non-zero ``returncode`` is
  an answer (not a repo, unknown ref, unresolvable sha), not a failure to run,
  so it comes back as a normal :class:`subprocess.CompletedProcess` for the
  caller to read however its own domain requires.

``None`` therefore means "couldn't determine", and callers must degrade to
their own unknown value — never to a confident negative.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Union

from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Default wall-clock cap. Every consumer but :mod:`src.scanner` (which walks
#: whole project directories and allows itself more headroom) runs with this.
DEFAULT_TIMEOUT_S = 5.0

#: Every call here is a read. Plain ``git status`` still takes ``index.lock``
#: optionally, to write refreshed stat info back, and a process killed
#: mid-write (this module's own timeout, on a loaded box) strands the 0-byte
#: lock — invisible to every read, fatal to the next ``add``/``checkout``/
#: ``pull`` in that checkout. With the variable
#: set a read never takes the lock, so a killed one cannot leave it behind.
READ_ONLY_ENV = {"GIT_OPTIONAL_LOCKS": "0"}


def run_git(
    project_root: Union[Path, str],
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Optional[subprocess.CompletedProcess]:
    """Run ``git -C <project_root> <args>`` under the shared contract.

    Returns the completed process regardless of its exit code, or ``None``
    when git could not be run at all (see the module docstring). Never
    raises.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=NO_WINDOW,
            env={**os.environ, **READ_ONLY_ENV},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "⚠️ git %s in %s could not run: %s: %s",
            " ".join(args), project_root, type(exc).__name__, exc,
        )
        return None


def _resolve_default_remote_ref(project_root: Union[Path, str]) -> Optional[str]:
    """``origin/HEAD``'s target (e.g. ``"origin/main"``), falling back to
    whichever of ``origin/main`` / ``origin/master`` exists. ``None`` when
    neither resolves (no ``origin`` remote, git missing, not a repo)."""
    result = run_git(
        project_root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]
    )
    if result is None:
        return None
    ref = (result.stdout or "").strip()
    if ref:
        return ref
    for candidate in ("origin/main", "origin/master"):
        verify = run_git(
            project_root, ["rev-parse", "--verify", "--quiet", candidate]
        )
        if verify is None:
            return None
        if verify.returncode == 0:
            return candidate
    return None


def resolve_deployed_sha(project_root: Union[Path, str] = PROJECT_ROOT) -> str:
    """Short git SHA of ``project_root``'s resolved default remote branch
    (``origin/HEAD`` -> ``origin/main`` -> ``origin/master``) — the ref that
    reflects what's actually mergeable/deployed.

    Unlike :func:`src.build_info.resolve_git_sha`, which reports the live
    checkout's *current branch tip*, this is stable across whatever branch the
    checkout transiently sits on (e.g. a worker occupying the primary tree
    mid-issue), so ``/api/version``'s ``stale_relevant`` compares against the
    ref that is actually deployed. No fetch is performed: this reads the local
    ``origin/*`` remote-tracking ref as last synced. Falls back to
    ``"unknown"`` when no candidate ref resolves, git isn't on PATH, or this
    isn't a repo.
    """
    ref = _resolve_default_remote_ref(project_root)
    if ref is None:
        return "unknown"
    result = run_git(project_root, ["rev-parse", "--short", ref])
    if result is None:
        return "unknown"
    sha = (result.stdout or "").strip()
    if not sha:
        logger.warning(
            "⚠️ git rev-parse %s exit=%s stderr=%r",
            ref, result.returncode, (result.stderr or "").strip(),
        )
        return "unknown"
    return sha
