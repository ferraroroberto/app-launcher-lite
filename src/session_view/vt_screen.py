"""A full-screen agent's current screen, as the session-host's VT mirror holds it.

Copilot repaints only the cells that change, so its PTY capture's tail is not
a screen: rendered on its own it shows a status line of ``8%`` with the rest
painted long before. The session-host keeps a headless VT mirror of every
full-screen session (:mod:`src.vt_snapshot`) and sends its current frame as
the opening message of a terminal socket, so this opens one, takes that
frame, and renders it at the session's size. ``role=pc`` keeps the socket
from claiming the PTY's size, and nothing is ever sent on it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidHandshake, WebSocketException

from src.clients import session as session_client
from src.session_view.plan_picker import screen_lines

logger = logging.getLogger(__name__)

# The session-host's buffer wipe, sent on its own before the frame.
_CLEAR_FRAME = "\x1b[H\x1b[2J\x1b[3J"
# The frame is the host's second message, sent from memory: a wait past this
# is a host that is not answering, not a slow render.
_FRAME_TIMEOUT_S = 3.0


async def read_vt_screen(port: int, sid: str, rows: int, cols: int) -> Optional[List[str]]:
    """The session's current screen, one string a row, or ``None`` when the
    socket fails or no frame arrives in time."""
    try:
        async with asyncio.timeout(_FRAME_TIMEOUT_S):
            async with ws_connect(session_client.ws_url(port, sid, "pc"), max_size=None) as ws:
                async for message in ws:
                    text = message if isinstance(message, str) else message.decode("utf-8", "replace")
                    if text and text != _CLEAR_FRAME:
                        return await asyncio.to_thread(screen_lines, text, rows, cols)
    except (TimeoutError, OSError, InvalidHandshake, WebSocketException) as exc:
        logger.info("ℹ️ vt screen %s unreadable: %s", sid[:8], exc or type(exc).__name__)
    return None
