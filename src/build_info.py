"""Process build identity — the git SHA this process actually loaded.

Captured once, at import time, so it reflects the code running in *this*
process rather than live git state (a process started three days ago still
reports the SHA it booted with, even if ``HEAD`` has since moved). Shared by
the webapp's ``/api/version`` and the session-host's ``/healthz`` (#615) so
both processes report their identity the same way — the session-host is
excluded from ``tray.bat --restart``'s reclaim sweep (project-scaffolding#35,
to protect live PTYs), so it can run stale for days with nothing visible
saying so; this is the mechanism that makes that determinable.
"""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Dict

from src.git_run import run_git

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_git_sha(project_root: Path = PROJECT_ROOT) -> str:
    """Short git SHA of ``project_root``'s checkout.

    Falls back to ``"unknown"`` if git isn't on PATH or this isn't a repo —
    both happen in test envs and shouldn't crash startup. The invocation
    contract (including the ``CREATE_NO_WINDOW`` that keeps a stray console
    from flashing under the console-less pythonw tray) lives in
    :func:`src.git_run.run_git`.
    """
    result = run_git(project_root, ["rev-parse", "--short", "HEAD"])
    if result is None:
        return "unknown"
    sha = (result.stdout or "").strip()
    if not sha:
        logger.warning(
            "⚠️ build_info: git rev-parse exit=%s stderr=%r",
            result.returncode, (result.stderr or "").strip(),
        )
        return "unknown"
    return sha


def build_identity(project_root: Path = PROJECT_ROOT) -> Dict[str, str]:
    """``{"git_sha", "captured_at"}`` for ``project_root``, computed now.

    Call once at process/module import to capture "what this process
    loaded"; call again later (fresh, uncached) to get the live, current
    value for comparison — that's exactly the staleness check #615 needs.
    """
    return {
        "git_sha": resolve_git_sha(project_root),
        "captured_at": _dt.datetime.now().replace(microsecond=0).isoformat(),
    }


def _resolve_default_remote_ref(project_root: Path) -> str | None:
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


def resolve_deployed_sha(project_root: Path = PROJECT_ROOT) -> str:
    """Short git SHA of ``project_root``'s resolved default remote branch
    (``origin/HEAD`` -> ``origin/main`` -> ``origin/master``) — the ref that
    reflects what's actually mergeable/deployed.

    Unlike :func:`resolve_git_sha`, which reports the live checkout's
    *current branch tip*, this is stable across whatever branch the checkout
    transiently sits on (e.g. a worker occupying the primary tree mid-issue)
    — the exact mismatch that made ``/api/version``'s ``stale_relevant``
    compare against the wrong ref in #641. No fetch is performed: this reads
    the local ``origin/*`` remote-tracking ref as last synced, same as
    :func:`src.scanner._default_branch`. Falls back to ``"unknown"`` when no
    candidate ref resolves, git isn't on PATH, or this isn't a repo.
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
            "⚠️ build_info: git rev-parse %s exit=%s stderr=%r",
            ref, result.returncode, (result.stderr or "").strip(),
        )
        return "unknown"
    return sha
