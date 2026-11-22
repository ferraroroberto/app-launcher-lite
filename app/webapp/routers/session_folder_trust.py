"""Board: answer Copilot's "Confirm folder trust" dialog for a live session.

Copilot asks it before it reads a first message, and no launch flag answers it
(``src.session_view.folder_trust``). The Board card for a session sitting on
it offers "Trust this folder", which picks Copilot's own "Yes, and remember
this folder" option, so Copilot records the trust itself.

``POST /api/claude-code/sessions/{sid}/folder-trust`` reads the screen again at
send time and types the digit only while the dialog is still up; its own
``_TERMINAL_GUARD_RULES`` row makes it passkey-gated like ``/input``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request

from app.webapp.hot_poll import run_on_default_executor
from app.webapp.routers._helpers import audit_off_loop
from app.webapp.routers.board_spawn import SESSION_HOST_UNREACHABLE, read_live_sessions
from app.webapp.routers.session_screen import (
    _PartialAnswer,
    _refuse_if_host_unreachable,
    _type_into_pty,
)
from src import audit
from src.session_view import folder_trust
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

router = APIRouter()

# How long the dialog gets to close after the key before the answer is
# reported unconfirmed, and how often it is looked at.
_TRUST_CONFIRM_S = 6.0
_TRUST_POLL_S = 0.5


async def _folder_trust_prompt(
    cfg: WebappConfig, sid: str
) -> Tuple[Optional[Dict[str, Any]], Optional[folder_trust.FolderTrustPrompt]]:
    """``(session, prompt)`` for ``sid``: the session as the host lists it
    (``None`` when it is gone) and the dialog it shows now (``None``: none)."""
    live, host_error = await run_on_default_executor(
        "read_live_sessions", read_live_sessions, cfg.session_host_port
    )
    _refuse_if_host_unreachable(SESSION_HOST_UNREACHABLE if host_error else None)
    session = next((s for s in live if str(s.get("session_id")) == str(sid)), None)
    if session is None:
        return None, None
    prompt = await run_on_default_executor(
        "read_capture_tail", folder_trust.pending_for_session, session
    )
    return session, prompt


@router.post("/api/claude-code/sessions/{sid}/folder-trust")
async def session_folder_trust(sid: str, request: Request) -> Dict[str, Any]:
    """Answer Copilot's folder-trust dialog with "Yes, and remember this folder".

    A 409 and nothing typed when the session is gone or no dialog is showing.
    Copilot writes ``trustedFolders`` itself. ``trusted`` is whether the dialog
    had closed within a few seconds: ``false`` means the key went in and
    nothing confirmed it, and the card keeps offering the tap.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    session, prompt = await _folder_trust_prompt(cfg, sid)
    if session is None:
        raise HTTPException(status_code=409, detail="This session is no longer running")
    if prompt is None:
        raise HTTPException(
            status_code=409,
            detail="This session is not waiting on a folder-trust prompt, so nothing was sent",
        )
    try:
        await _type_into_pty(cfg.session_host_port, sid, folder_trust.answer_keys(prompt))
    except _PartialAnswer as exc:
        await audit_off_loop(
            audit.session_log, sid, "folder_trust", reason="error", detail=exc.detail[:200],
        )
        logger.info("⚠️ folder trust %s not sent: %s", sid[:8], exc.detail[:200])
        raise HTTPException(
            status_code=502, detail="Trust not sent: nothing reached the terminal"
        ) from exc
    deadline = time.monotonic() + _TRUST_CONFIRM_S
    trusted = False
    while time.monotonic() < deadline and not trusted:
        await asyncio.sleep(_TRUST_POLL_S)
        _, still = await _folder_trust_prompt(cfg, sid)
        trusted = still is None
    await audit_off_loop(
        audit.session_log, sid, "folder_trust", reason="trusted" if trusted else "unverified",
    )
    logger.info(
        "⌨️ folder trust %s answered for %s (%s)", sid[:8],
        folder_trust.folder_name(prompt.folder), "closed" if trusted else "still showing",
    )
    return {"ok": True, "folder": prompt.folder, "trusted": trusted}
