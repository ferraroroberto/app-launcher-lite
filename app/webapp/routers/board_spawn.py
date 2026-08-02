"""Shared launch helpers for the Board tab's routes.

Split off ``app/webapp/routers/board.py`` (issue #691, a `/codebase-audit`
maintainability finding), the same way ``jobs_run.py`` was split off
``jobs.py``. This module owns the launch-path pieces ``board.py`` calls
through:

* :func:`_safe_list_sessions` — the degradation-safe live-session read.
* :func:`_resolve_repo_entry` — bare repo name → live projects-folder entry.
* :func:`_copilot_agent_and_flags` — the install-guarded Copilot launch
  flags (always the persisted Coding model).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import HTTPException

from src import agents, session_client
from src.launch_flags import build_copilot_flags
from src.registry import AppEntry, live_coding_entries
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)


def _safe_list_sessions(port: int) -> List[Dict[str, Any]]:
    """Live sessions, or [] when the session-host is down — the board must
    keep rendering the GitLab cards regardless (#164 degradation)."""
    try:
        return session_client.list_sessions(port)
    except session_client.SessionHostError as exc:
        logger.debug(f"board: session list failed: {exc}")
        return []


def _resolve_repo_entry(cfg: WebappConfig, repo: str) -> AppEntry:
    """Resolve ``repo`` to a live coding entry, or 404.

    ``start_issue`` takes a bare repo name and needs a case-insensitive
    lookup against the live projects-folder listing.
    """
    entries = live_coding_entries(
        Path(cfg.projects_dir), list(cfg.projects_ignore)
    )
    entry = next(
        (e for e in entries if e.name.lower() == repo.lower()), None
    )
    if entry is None or not entry.project_dir:
        raise HTTPException(
            status_code=404, detail=f"repo not in the projects folder: {repo}"
        )
    return entry


def _copilot_agent_and_flags(cfg: WebappConfig) -> Tuple[str, str]:
    """``(agent, flags)`` for a Board launch — always the persisted model.

    Every Board launch routes to Copilot with the persisted Coding model
    (``build_copilot_flags``, no per-launch override). The ``is_installed``
    check is the same defence-in-depth 400 as ``apps.py`` — Board launches
    bypass the Coding tab's already-disabled button.
    """
    if not agents.is_installed(agents.DEFAULT_AGENT):
        raise HTTPException(
            status_code=400,
            detail=f"{agents.AGENTS[agents.DEFAULT_AGENT].label} is not installed",
        )
    return (agents.DEFAULT_AGENT, build_copilot_flags(cfg))
