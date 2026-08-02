"""Board tab — the fleet kanban's data plane (issues #300, #301 / #164 / #399).

    GET  /api/board                       → the four computed columns (token-gated)
    POST /api/board/gitlab/refresh        → run the glab queries now (token-gated)
    GET  /api/board/sessions/{sid}/exchange → last user↔assistant exchange
                                            (private network + passkey — transcript text)
    POST /api/board/issues/start          → spawn /issue-start|yolo <N> in the
                                            issue's repo (private network + passkey)

Split off a single-file god-router (issue #691, `/codebase-audit`), the way
``jobs.py`` and ``sessions.py`` already were: the shared launch helpers
(repo resolution, the Copilot install guard, the degradation-safe session
list) live in :mod:`app.webapp.routers.board_spawn`.

``GET /api/board`` is the 5s poll target, so it does only cheap work: the live
session list from the session-host plus two state-file reads (in worker
threads, gathered concurrently) and a pure memory read of the GitLab cache.
The ``glab`` subprocesses run **only** inside the explicit refresh endpoint —
the exact on-demand contract of the Coding tab's ⎇ git-status button. Column
assembly is pure logic in :mod:`src.board`.

The board + refresh routes are read-only repo/session metadata — the same gate
class as ``GET /api/coding/sessions`` (bearer token, no passkey). The
drill-down exchange and issue-start routes (#301) are terminal-grade and get
the passkey gate in ``middleware._terminal_guard_level``; the reply proxy
lives beside its session siblings in ``routers/sessions.py``.

Issue-start is injection-safe by construction: the positional prompt is built
**server-side** as ``/issue-<mode> <N>`` with ``mode`` allowlisted and ``N``
int-validated, so the string that reaches the session-host's unquoted
``cmd /c`` line can never contain a metacharacter.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from src import agents, audit, board, gitlab_client, session_client
from src.board_exchange import resolve_exchange, unavailable
from src.launcher import open_local_terminal_window, spawn_agent_session
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import (
    audit_session_start_and_maybe_mirror,
    maybe_json,
    spawn_session_or_400,
)
from app.webapp.routers.board_spawn import (
    _copilot_agent_and_flags,
    _resolve_repo_entry,
    _safe_list_sessions,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _gitlab_section(snap: Dict[str, Any]) -> Dict[str, Any]:
    return {"fetched_at": snap.get("fetched_at"), "error": snap.get("error")}


def _mark_active_backlog(
    columns: Dict[str, List[Dict[str, Any]]], active_rows: Dict[str, Any]
) -> None:
    """Annotate each backlog card from the shared ``repo#number`` mapping."""
    active_keys = {str(key).lower() for key in active_rows}
    for card in columns.get("backlog", []):
        repo = str(card.get("repo") or "").strip().lower()
        number = card.get("number")
        key = f"{repo}#{number}" if repo and isinstance(number, int) else ""
        card["in_progress"] = key in active_keys


@router.get("/api/board")
async def get_board(request: Request) -> Dict[str, Any]:
    """The four columns + source health, cheap enough for the 5s poll."""
    cfg: WebappConfig = request.app.state.webapp_config

    active_issues_file = Path(cfg.sessions_state_file).with_name("active-issues.json")
    live, state, active_issues = await asyncio.gather(
        asyncio.to_thread(_safe_list_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
        asyncio.to_thread(board.read_active_issues, active_issues_file),
    )
    gitlab = gitlab_client.snapshot()

    session_cards = board.merge_sessions(
        live, state["rows"],
        active_issue_repos=board.active_issue_repos(active_issues["rows"]),
    )
    columns = board.build_board(session_cards, gitlab)
    _mark_active_backlog(columns, active_issues["rows"])

    return {
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "columns": columns,
        "gitlab": _gitlab_section(gitlab),
        "sessions_state": {
            "available": state["available"],
            "stale": state["stale"],
            "updated_at": state["updated_at"],
        },
        "active_issues": {
            "available": active_issues["available"],
            "updated_at": active_issues["updated_at"],
            "count": len(active_issues["rows"]),
        },
    }


@router.post("/api/board/gitlab/refresh")
async def refresh_gitlab(request: Request) -> Dict[str, Any]:
    """Run the group-wide glab queries now (subprocess-heavy, on demand only).

    An empty ``gitlab_group`` never reaches a subprocess —
    :func:`src.gitlab_client.refresh` short-circuits into an empty snapshot
    whose ``error`` tells the UI to point at Settings.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    snap = await asyncio.to_thread(
        gitlab_client.refresh, cfg.gitlab_group, cfg.gitlab_host
    )
    return _gitlab_section(snap)


@router.get("/api/board/sessions/{sid}/exchange")
async def session_exchange(sid: str, request: Request) -> Dict[str, Any]:
    """Last user↔assistant exchange for a live session (private network + passkey).

    Structured native history wins when it correlates safely. A missing
    hook JSONL or unsupported agent falls back to the launcher's exact-id PTY
    capture + input audit, parsed on demand (never on the Board poll). Distinct
    unavailable reasons let the client separate true-empty from source error.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    live, state = await asyncio.gather(
        asyncio.to_thread(_safe_list_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    session = next(
        (item for item in live if str(item.get("session_id")) == str(sid)), None
    )
    if session is None:
        return unavailable("session_not_found")
    row = board.state_row_for_session(live, state["rows"], sid)
    transcript = (row or {}).get("transcript_path")
    result = await asyncio.to_thread(
        resolve_exchange,
        session,
        transcript,
        audit.transcript_path(sid),
        audit.session_log_path(sid),
    )
    if result.get("source") == "launcher":
        logger.info(
            "ℹ️ Board exchange %s (%s) used exact-id launcher capture; "
            "native transcript unavailable",
            sid[:8], session.get("agent") or agents.DEFAULT_AGENT,
        )
    elif not result.get("available"):
        logger.info(
            "ℹ️ Board exchange %s (%s) unavailable: %s",
            sid[:8], session.get("agent") or agents.DEFAULT_AGENT,
            result.get("reason"),
        )
    return result


@router.post("/api/board/issues/start")
async def start_issue(request: Request) -> Dict[str, Any]:
    """One-tap ▶ Start / ⚡ YOLO on a backlog card (private network + passkey, #301).

    Body: ``{"repo": str, "number": int, "mode": "start"|"yolo",
    "rows": int, "cols": int, "title": str}``. The repo must
    resolve to a directory in the projects folder (the same live listing the
    Coding tab launches from); the prompt is built here as
    ``/issue-<mode> <number>`` — client text never reaches the command line.
    Spawns a streamed PTY session exactly like a Coding-tab launch (PC
    mirror rules included); the `/issue-*` skills themselves handle branch +
    worktree claiming inside the session. Always launches with the persisted
    Coding model — there is no per-launch model override.

    The optional ``title`` (the Board card's issue title) auto-names the
    session after the issue (#467) via the #458 manual-override path, so it is
    recognizable in the Coding tab without waiting for the agent to self-name.
    The title is display data — it never reaches the command line.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    repo = str(body.get("repo") or "").strip()
    mode = str(body.get("mode") or "start").strip().lower()
    if mode not in ("start", "yolo"):
        raise HTTPException(status_code=400, detail=f"unknown mode: {mode}")
    try:
        number = int(body.get("number"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="number must be an integer")
    if number <= 0:
        raise HTTPException(status_code=400, detail="number must be positive")
    rows = int(body.get("rows") or 40)
    cols = int(body.get("cols") or 120)
    title = str(body.get("title") or "").strip()
    agent, base_flags = _copilot_agent_and_flags(cfg)

    entry = _resolve_repo_entry(cfg, repo)

    prompt = f"/issue-{mode} {number}"
    native_name_flags = agents.native_session_name_flags_for(agent, title)
    flags = " ".join(
        part for part in (base_flags, native_name_flags, f'"{prompt}"') if part
    )
    session = await spawn_session_or_400(
        spawn_agent_session,
        Path(entry.project_dir),
        entry.name,
        flags,
        cfg.session_host_port,
        "pty",
        agent,
        rows,
        cols,
        history_lines=cfg.terminal_history_lines,
    )

    sid = str(session.get("session_id") or "")
    await audit_session_start_and_maybe_mirror(
        cfg, request, body,
        sid=sid, agent=agent, name=entry.name, project=entry.project_dir,
        skill=prompt, audit_mod=audit, mirror_fn=open_local_terminal_window,
    )
    # Auto-name the session after the issue title (#467): a Board-started
    # session is then recognizable in the Coding tab immediately, instead of
    # inheriting the first-prompt/OSC-derived default. Reuses the #458 manual
    # override (a launcher-side ``manual_title`` set, wins over the agent's
    # later self-naming). Best-effort — a rename failure must never fail an
    # otherwise-successful launch. No readiness wait needed: the rename is a
    # pure in-memory attribute set on the session record, never typed into
    # the PTY (the racy agent-native injection was removed in #555). Agents
    # with a verified spawn-time --name flag also receive the same safe title
    # above, so their native resume picker is synchronized from birth (#556).
    if sid and title:
        try:
            await asyncio.to_thread(
                session_client.rename, cfg.session_host_port, sid, title
            )
        except session_client.SessionHostError as exc:
            logger.warning(
                "⚠️ Board issue-start could not auto-name session %s: %s",
                sid[:8], exc,
            )
    return {"launched": prompt, "repo": entry.name, "session": session}
