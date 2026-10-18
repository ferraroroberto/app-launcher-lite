"""Screen view (#60, ported from app-launcher#1520): the PC's desktop, live, on
the phone.

``GET /api/screen/monitors`` lists the displays; ``WS /api/screen/ws`` streams
one of them as JPEG binary frames. Capture lives in :mod:`src.desktop_capture`
and runs only inside a connected socket.

A picture of the desktop shows whatever is on it, terminals included, so this
is terminal-grade content: the HTTP route carries a ``passkey`` row in
``middleware._TERMINAL_GUARD_RULES``, and the WebSocket, which the HTTP
middleware never sees, re-applies the same gate before a single grab via
``middleware.ws_terminal_gate`` — the same helper ``sessions.proxy_session_ws``
calls, so the legs, order and close codes read the same way on both.

Protocol: the server sends binary JPEG frames and JSON text status messages
(``{"type": "status", "state": "unavailable" | "live"}``); the client sends
the text ``ack`` after drawing each frame, and the next frame waits for it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from src import audit, desktop_capture

from app.webapp.middleware import ws_terminal_gate
from app.webapp.routers._helpers import audit_off_loop, client_ip_ws

logger = logging.getLogger(__name__)

router = APIRouter()

WS_PATH = "/api/screen/ws"


@router.get("/api/screen/monitors")
async def screen_monitors() -> Dict[str, Any]:
    """The displays the Screen view can show; no capture happens here."""
    monitors = await asyncio.to_thread(desktop_capture.list_monitors)
    return {"monitors": [m.to_dict() for m in monitors]}


def _int_param(websocket: WebSocket, name: str) -> Optional[int]:
    raw = websocket.query_params.get(name, "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


async def _read_acks(
    websocket: WebSocket, acked: asyncio.Event, stopped: asyncio.Event
) -> None:
    """Turn the client's ``ack`` messages into ``acked``; a disconnect stops."""
    try:
        while True:
            if await websocket.receive_text() == "ack":
                acked.set()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        stopped.set()


@router.websocket(WS_PATH)
async def screen_ws(websocket: WebSocket) -> None:
    """Stream one monitor until the viewer goes away."""
    # Accept first, then gate, so the browser gets the close code and reason.
    await websocket.accept()
    refusal = ws_terminal_gate(websocket, label="screen")
    if refusal is not None:
        await websocket.close(code=refusal[0], reason=refusal[1])
        return

    monitors = await asyncio.to_thread(desktop_capture.list_monitors)
    monitor = desktop_capture.pick_monitor(monitors, _int_param(websocket, "monitor"))
    if monitor is None:
        await websocket.close(code=4404, reason="no such monitor")
        return
    width = min(
        desktop_capture.clamp_width(_int_param(websocket, "width")), monitor.width
    )

    client = client_ip_ws(websocket)
    started = time.monotonic()
    await audit_off_loop(
        audit.audit_event, "screen_open", client=client, monitor=monitor.id,
        rect=list(monitor.rect), width=width,
    )

    acked = asyncio.Event()
    acked.set()
    stopped = asyncio.Event()

    # Counted here rather than taken from stream_frames' return value, so a
    # send that fails mid-stream still audits what did go out. The byte total
    # is the session's real traffic on the work link, and ``unavailable``
    # counts the times the desktop stopped being readable — the one durable
    # trace of a blocked capture, since the tray discards the webapp's own
    # log output (#60).
    sent = {"frames": 0, "bytes": 0, "unavailable": 0}

    async def send_frame(data: bytes) -> None:
        await websocket.send_bytes(data)
        sent["frames"] += 1
        sent["bytes"] += len(data)

    async def send_status(state: str) -> None:
        if state == "unavailable":
            sent["unavailable"] += 1
        await websocket.send_text(json.dumps({"type": "status", "state": state}))

    reader = asyncio.create_task(_read_acks(websocket, acked, stopped))
    try:
        await desktop_capture.stream_frames(
            lambda: desktop_capture.grab_monitor(monitor),
            send_frame, send_status, acked, stopped, width=width,
        )
    except (WebSocketDisconnect, RuntimeError, OSError) as exc:
        logger.debug("Screen socket ended while sending: %s", exc)
    finally:
        stopped.set()
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        # Shielded: a handler cancelled mid-teardown (server shutdown, or a
        # test client's cancel scope, which re-cancels at every await) must
        # still write the close line, since it carries the session's traffic.
        await asyncio.shield(audit_off_loop(
            audit.audit_event, "screen_close", client=client, monitor=monitor.id,
            duration_s=round(time.monotonic() - started, 1),
            frames=sent["frames"], bytes=sent["bytes"],
            unavailable=sent["unavailable"],
        ))
