"""``middleware.ws_terminal_gate`` — the one WebSocket terminal gate (#60).

Starlette middleware never sees a WebSocket handshake, so every
terminal-grade socket re-applies the HTTP gate's legs through this one
function. Each leg is pinned here through the live terminal proxy route
(``/api/coding/sessions/{sid}/ws``): a refusal is its close code, and a
socket that gets through reaches the (stubbed, unreachable) session-host and
closes 4502 instead.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.webapp.middleware import ws_terminal_gate
from src.webauthn_gate import WebAuthnGate

TOKEN = "s3cret-bearer"
TAILNET = "100.101.102.103"
ALLOWLISTED = "10.20.30.40"
LAN = "192.168.1.50"
LOOPBACK = "127.0.0.1"
EDGE = {"cf-ray": "abc-AMS"}
# The gate let the socket through: the proxy dialled the session-host stub.
PASSED = 4502


class _UnreachableConnect:
    """``ws_connect`` stand-in: the session-host is not listening."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        raise OSError("connection refused")

    async def __aexit__(self, *exc) -> bool:
        return False


@pytest.fixture
def gated(webapp_client, monkeypatch, tmp_path: Path):
    """App with the bearer token, the passkey gate and an allowlist all live.
    Yields ``(app, tt)`` — ``tt`` a valid passkey terminal token."""
    _client, app, _overrides = webapp_client
    from app.webapp.routers import sessions as sessions_router

    monkeypatch.setattr(sessions_router, "ws_connect", _UnreachableConnect)
    cfg = app.state.webapp_config
    cfg.auth_token = TOKEN
    cfg.tailnet_allowlist = ["10.20.30.0/24"]
    cfg.webauthn_rp_id = "pc.example.ts.net"
    cfg.webauthn_origin = "https://pc.example.ts.net:8465"
    gate = WebAuthnGate(devices_path=tmp_path / "devices.json")
    app.state.webauthn_gate = gate
    yield app, gate.mint_local_token()


def _close_code(app, host: str, *, token: str = "", tt: str = "", headers=None) -> int:
    params = "&".join(f"{k}={v}" for k, v in (("token", token), ("tt", tt)) if v)
    url = "/api/coding/sessions/sid-1/ws" + ("?" + params if params else "")
    client = TestClient(app, client=(host, 50000))
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(url, headers=headers or {}) as ws:
            ws.receive_text()
    return excinfo.value.code


def test_refused_over_the_public_edge(gated):
    app, tt = gated
    assert _close_code(app, TAILNET, token=TOKEN, tt=tt, headers=EDGE) == 4403


def test_edge_header_overrides_a_loopback_address(gated):
    """cloudflared dials the webapp over loopback; its header must still win."""
    app, _ = gated
    assert _close_code(app, LOOPBACK, headers=EDGE) == 4403


def test_refused_off_the_private_network(gated):
    app, tt = gated
    assert _close_code(app, LAN, token=TOKEN, tt=tt) == 4403


def test_refused_without_the_bearer_token(gated):
    app, tt = gated
    assert _close_code(app, TAILNET, tt=tt) == 4401


def test_refused_with_a_wrong_bearer_token(gated):
    app, tt = gated
    assert _close_code(app, TAILNET, token="nope", tt=tt) == 4401


def test_refused_without_a_passkey_unlock(gated):
    app, _ = gated
    assert _close_code(app, TAILNET, token=TOKEN) == 4401


def test_refused_with_a_forged_passkey_token(gated):
    app, _ = gated
    assert _close_code(app, TAILNET, token=TOKEN, tt="not-minted") == 4401


def test_an_unlocked_tailnet_client_gets_through(gated):
    app, tt = gated
    assert _close_code(app, TAILNET, token=TOKEN, tt=tt) == PASSED


def test_an_unlocked_allowlisted_client_gets_through(gated):
    app, tt = gated
    assert _close_code(app, ALLOWLISTED, token=TOKEN, tt=tt) == PASSED


def test_the_pc_itself_gets_through_without_credentials(gated):
    app, _ = gated
    assert _close_code(app, LOOPBACK) == PASSED


def test_the_label_names_the_service_in_the_reason():
    socket = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(webapp_config=SimpleNamespace())),
        client=SimpleNamespace(host=LAN),
        headers={},
        query_params={},
    )
    code, reason = ws_terminal_gate(socket, label="screen")
    assert code == 4403
    assert reason.startswith("screen is private-network-only")
