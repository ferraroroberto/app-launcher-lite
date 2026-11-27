"""Coding tab: a live session's terminal screen — read it, and type into it
(split out of ``session_transcript.py``).

Neither route reads the transcript. They read the PTY's current screen
(:mod:`src.session_view.vt_screen`) and/or type keystrokes into the PTY, so
they share nothing with the transcript pager but the session lookup.
:mod:`app.webapp.routers.session_folder_trust` types its answer through the
same writer.

``POST .../model`` switches a live session's model with Copilot's own
``/model`` command, and ``GET .../context`` reads the session's context-window
use off the same screen, which the overlay bar draws as a ring. Each route has
its own ``_TERMINAL_GUARD_RULES`` row.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidHandshake, WebSocketException

from app.webapp.hot_poll import BuildSteps, session_poll
from app.webapp.route_pools import SESSION_FILES, SESSION_HOST, run_on
from app.webapp.routers._helpers import audit_off_loop, maybe_json
from app.webapp.routers.board_spawn import SESSION_HOST_UNREACHABLE, read_live_sessions
from src import audit
from src.agents import agent_of
from src.clients import session as session_client
from src.session_view import copilot_figures, copilot_session, model_switch
from src.session_view.vt_screen import read_vt_screen
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

router = APIRouter()

# The gap between two keystrokes of one write. A TUI takes one key per input
# event, so each key is its own write; 0.35 s keeps two writes from landing in
# one read even on a loaded box.
_KEY_GAP_S = 0.35


def _refuse_if_host_unreachable(reason: Optional[str]) -> None:
    """503 for a POST whose session lookup could not reach the session-host
    — not the 409 "no longer running" a session that is gone gets,
    since this one may well be alive and the caller should retry."""
    if reason == SESSION_HOST_UNREACHABLE:
        raise HTTPException(
            status_code=503, detail="The session host is unreachable, so nothing was sent"
        )


class _PartialAnswer(Exception):
    """A write failed after ``sent`` of ``total`` had already gone in."""

    def __init__(self, sent: int, total: int, detail: str) -> None:
        super().__init__(detail)
        self.sent, self.total, self.detail = sent, total, detail


async def _type_into_pty(port: int, sid: str, keys: List[Tuple[str, bool]]) -> None:
    """Raw keystrokes over the session-host socket — the same ``input``
    frame the terminal's own key bar sends, so no bracketed-paste framing.
    ``role=pc`` so this short-lived connection never claims the PTY's size;
    whatever the host streams back is drained and dropped."""
    writes = [w for text, enter in keys for w in ([text, "\r"] if enter else [text])]
    sent = 0
    try:
        async with ws_connect(session_client.ws_url(port, sid, "pc"), max_size=None) as ws:
            async def drain() -> None:
                async for _ in ws:
                    pass

            drainer = asyncio.create_task(drain())
            try:
                for data in writes:
                    if sent:
                        await asyncio.sleep(_KEY_GAP_S)
                    await ws.send(json.dumps({"type": "input", "data": data}))
                    sent += 1
                # Let the last frame reach the host before the close does.
                await asyncio.sleep(_KEY_GAP_S)
            finally:
                drainer.cancel()
    except (OSError, InvalidHandshake, WebSocketException) as exc:
        raise _PartialAnswer(sent, len(writes), f"terminal socket failed: {exc}") from exc


# The last state logged per session, so a poll every few seconds leaves one
# line per change rather than one per tick.
_CONTEXT_SEEN: Dict[str, str] = {}


def _log_context(sid: str, state: str, detail: str = "") -> None:
    if _CONTEXT_SEEN.get(sid) == state:
        return
    _CONTEXT_SEEN[sid] = state
    logger.info("ℹ️ context %s: %s%s", sid[:8], state, f" ({detail})" if detail else "")


# --- Switching the session's model from the Chat view's model chip --------
#
# Copilot's own `/model` does the switch, for that session only
# (src.session_view.model_switch). It counts only once Copilot's event log,
# the source the chip already reads, reports the new model.

# How long a switch waits for Copilot to report the new model, and how often
# it looks.
_MODEL_CONFIRM_S = 10.0
_MODEL_POLL_S = 0.5


async def _live_session(cfg: WebappConfig, sid: str) -> Dict[str, Any]:
    """The session-host's row for ``sid``: 503 unreachable, 409 gone."""
    live, host_error = await run_on(SESSION_HOST, read_live_sessions, cfg.session_host_port)
    session = next((s for s in live if str(s.get("session_id")) == str(sid)), None)
    if session is None:
        _refuse_if_host_unreachable(SESSION_HOST_UNREACHABLE if host_error else None)
        raise HTTPException(status_code=409, detail="This session is no longer running")
    return session


async def _type_switch(cfg: WebappConfig, sid: str, keys: List[Tuple[str, bool]]) -> None:
    """Type the switch; a socket failure is a 502 saying how far it got."""
    try:
        await _type_into_pty(cfg.session_host_port, sid, keys)
    except _PartialAnswer as exc:
        await audit_off_loop(
            audit.session_log, sid, "model_switch", agent="copilot", reason="error",
            sent=exc.sent, detail=exc.detail[:200],
        )
        logger.info("⚠️ model switch %s failed after %d writes: %s", sid[:8], exc.sent, exc.detail[:200])
        raise HTTPException(
            status_code=502,
            detail=("Switch not sent: nothing reached the terminal" if exc.sent == 0
                    else "Switch only partly sent: check the terminal"),
        ) from exc


async def _copilot_screen(cfg: WebappConfig, session: Dict[str, Any]) -> Optional[List[str]]:
    """A Copilot session's current screen from the session-host's VT mirror:
    its PTY capture's tail is a run of partial repaints, not a screen."""
    return await read_vt_screen(
        cfg.session_host_port, str(session.get("session_id")),
        int(session.get("rows") or 40), int(session.get("cols") or 120),
    )


async def _switch_copilot(
    cfg: WebappConfig, sid: str, keys: List[Tuple[str, bool]], model: str, session: Dict[str, Any]
) -> Tuple[str, Optional[str]]:
    """``(outcome, detail)`` for a switch: Copilot never asks, and its event
    log records the model it moved to."""
    log = await run_on(SESSION_FILES, copilot_session.event_log, session, True)
    if log is None:
        # Copilot writes its log at the first prompt; the switch is confirmed there.
        raise HTTPException(
            status_code=409,
            detail="Copilot keeps no event log until its first prompt, so a switch couldn't be "
                   "confirmed: send a prompt first. Nothing was sent",
        )
    if await run_on(SESSION_FILES, copilot_session.current_model, log) == model:
        return "switched", None
    lines = await _copilot_screen(cfg, session)
    refusal = (copilot_session.switch_refusal(lines) if lines is not None
               else "Chat can't see the terminal's screen, so nothing was sent")
    if refusal:
        raise HTTPException(status_code=409, detail=refusal)
    await _type_switch(cfg, sid, keys)
    loop = asyncio.get_running_loop()
    end = loop.time() + _MODEL_CONFIRM_S
    while loop.time() < end:
        if await run_on(SESSION_FILES, copilot_session.current_model, log) == model:
            return "switched", None
        await asyncio.sleep(_MODEL_POLL_S)
    return "unconfirmed", f"Copilot didn't report the switch to {model}: check the terminal"


@router.post("/api/claude-code/sessions/{sid}/model")
async def session_switch_model(sid: str, request: Request) -> Dict[str, Any]:
    """Switch a live session to another model (Tailscale + passkey).

    Body: ``{"model": str}``, one of ``copilot_models``. Refused before
    anything is typed: an agent with no in-session switch or a detached
    session (409), a model outside the list (400), and a screen that can't be
    read or that waits on something else (409).

    ``outcome`` is ``switched`` only once Copilot's event log reports the new
    model, including when it already ran it and nothing was typed; otherwise
    ``unconfirmed`` with ``detail`` saying why.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    session = await _live_session(cfg, sid)
    agent = agent_of(session)
    value = body.get("model")
    try:
        keys = model_switch.switch_keys(agent, value, cfg.copilot_models)
    except model_switch.SwitchRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if str(session.get("kind") or "pty") == "remote":
        raise HTTPException(
            status_code=409,
            detail="Chat can't switch a detached session's model: use /model in the PC console",
        )
    outcome, detail = await _switch_copilot(cfg, sid, keys, value, session)
    await audit_off_loop(
        audit.session_log, sid, "model_switch", agent=agent, model=value, reason=outcome,
    )
    logger.info("⌨️ model switch %s to %s: %s", sid[:8], value, outcome)
    return {"ok": True, "outcome": outcome, "model": value, "detail": detail}


@router.get("/api/claude-code/sessions/{sid}/context")
async def session_context(sid: str, request: Request) -> Dict[str, Any]:
    """How full the session's context window is, as its terminal's footer
    shows it now (Tailscale + passkey).

    ``{"available", "percent", "ai_credits", "reason"}``. ``ai_credits`` is the
    float the footer shows as ``Session: N AIC used`` and ``percent`` the int it
    shows as ``(NN%)``; each is ``None`` once the footer has shown none for
    :data:`~src.session_view.copilot_figures.KEEP_S`.
    ``reason`` tells apart a session that is gone, an agent with no such
    figure, a detached one (no screen to read), a screen that can't be read
    (``no_screen``), and a footer showing no context figure
    (``not_showing``). The overlay bar polls it and draws nothing without a
    number, so an unknown value is never shown as 0%.

    Built through a per-session :class:`app.webapp.hot_poll.HotPoll`:
    past its deadline the answer is the last good one, with ``stale: true``
    and ``age_seconds``.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    poll = session_poll(request.app.state, "GET context", sid)
    return await poll.get(functools.partial(_build_context, cfg, sid))


async def _context_screen(
    cfg: WebappConfig, sid: str, steps: BuildSteps
) -> Tuple[Optional[str], Optional[List[str]]]:
    """``(reason, lines)``: ``reason`` says why there is no screen to read
    (``session_not_found`` / ``session_host_unreachable`` /
    ``unsupported_agent`` / ``detached`` / ``no_screen``); otherwise ``lines``
    is the screen as the terminal shows it."""
    live, host_error = await steps.run("read_live_sessions", read_live_sessions, cfg.session_host_port)
    session = next((s for s in live if str(s.get("session_id")) == str(sid)), None)
    if session is None:
        return (SESSION_HOST_UNREACHABLE if host_error else "session_not_found"), None
    if agent_of(session) != "copilot":
        return "unsupported_agent", None
    if str(session.get("kind") or "pty") == "remote":
        return "detached", None
    lines = await _copilot_screen(cfg, session)
    return (None, lines) if lines is not None else ("no_screen", None)


async def _build_context(cfg: WebappConfig, sid: str, steps: BuildSteps) -> Dict[str, Any]:
    reason, lines = await _context_screen(cfg, sid, steps)
    if reason is not None:
        if reason == "session_not_found":
            _CONTEXT_SEEN.pop(sid, None)
            copilot_figures.forget(sid)
        else:
            _log_context(sid, reason)
        return {"available": False, "percent": None, "ai_credits": None, "reason": reason}
    percent, credits = copilot_figures.observe(sid, lines)
    if percent is None:
        _log_context(sid, "not showing")
        return {"available": True, "percent": None, "ai_credits": credits, "reason": "not_showing"}
    detail = f"{percent}%" if credits is None else f"{percent}% {credits} AIC"
    _log_context(sid, "showing", detail)
    return {"available": True, "percent": percent, "ai_credits": credits, "reason": None}
