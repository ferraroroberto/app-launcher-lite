"""Refuse everyone but the PC itself while the settings file cannot be read.

Unreadable settings load as defaults, and the default access token is empty,
which switches the bearer check off. One gate in front of every HTTP route and
WebSocket keeps that from opening the app to the network.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from src.middleware import trust

# Goes to remote callers, so it names no path and quotes no parse error.
_REFUSAL = (
    "Remote access is off: the settings file on the PC cannot be read. "
    "Fix it on the PC, then restart the app."
)


def report_unreadable_config(cfg: Any, logger: logging.Logger) -> None:
    """Log, at start, the file, the parse error and what is refused because of it."""
    if cfg.load_error:
        logger.error(
            f"❌ {cfg.load_error}. Running on defaults and refusing every request "
            "that is not from this PC until the file is fixed and the app restarted."
        )


class UnreadableConfigGate:
    """Plain ASGI, because ``BaseHTTPMiddleware`` never sees a WebSocket."""

    def __init__(self, app: ASGIApp, get_config: Callable[[], Any]) -> None:
        self.app = app
        self._get_config = get_config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and self._get_config().load_error:
            client = scope.get("client")
            if not trust.is_pc_itself(client[0] if client else "", Headers(scope=scope)):
                refusal = (
                    WebSocketClose(code=4403, reason="remote access is off")
                    if scope["type"] == "websocket"
                    else JSONResponse({"detail": _REFUSAL}, status_code=503)
                )
                await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)
