"""``terminal_allow_private_lan``: a phone on the trusted LAN reaches the terminal.

The terminal-grade routes (transcript, input, image, WebSocket) accept
loopback, the Tailscale range and ``tailnet_allowlist``. The switch adds the
private ranges to that one verdict, so HTTP, the WebSocket and the SPA's
up-front reachability answer move together. Off (the default) nothing changes.

The screen view shows the whole desktop, so it also needs
``screen_allow_private_lan``; both switches change only from the PC itself or
over Tailscale.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from src.middleware import terminal_gate

_LAN = ("192.168.1.50", 50000)
_PUBLIC = ("203.0.113.9", 5000)
_TRANSCRIPT = "/api/claude-code/sessions/some-sid/transcript"
_INPUT = "/api/claude-code/sessions/some-sid/input"


def _client(webapp_client, peer, *, private_lan):
    _client_, app, _overrides = webapp_client
    app.state.webapp_config.terminal_allow_private_lan = private_lan
    return TestClient(app, client=peer)


@pytest.mark.parametrize("route", [_TRANSCRIPT, _INPUT])
def test_lan_peer_is_refused_by_default(webapp_client, route) -> None:
    resp = _client(webapp_client, _LAN, private_lan=False).request(
        "GET" if route == _TRANSCRIPT else "POST", route,
    )
    assert resp.status_code == 403
    assert "Tailscale-only" in resp.json()["detail"]


@pytest.mark.parametrize("route", [_TRANSCRIPT, _INPUT])
def test_lan_peer_passes_the_gate_when_the_switch_is_on(webapp_client, route) -> None:
    resp = _client(webapp_client, _LAN, private_lan=True).request(
        "GET" if route == _TRANSCRIPT else "POST", route,
    )
    assert resp.status_code != 403, resp.text


def test_a_public_address_is_refused_even_with_the_switch_on(webapp_client) -> None:
    resp = _client(webapp_client, _PUBLIC, private_lan=True).get(_TRANSCRIPT)
    assert resp.status_code == 403


def test_the_public_tunnel_is_refused_even_with_the_switch_on(webapp_client) -> None:
    resp = _client(webapp_client, _LAN, private_lan=True).get(
        _TRANSCRIPT, headers={"Cf-Ray": "1"},
    )
    assert resp.status_code == 403


def test_the_passkey_is_still_required_when_configured(webapp_client, monkeypatch) -> None:
    from src.auth.webauthn_gate import WebAuthnGate

    monkeypatch.setattr(WebAuthnGate, "configured", staticmethod(lambda cfg: True))
    _c, app, _o = webapp_client
    app.state.webauthn_gate = SimpleNamespace(valid_terminal_token=lambda t: False)
    resp = _client(webapp_client, _LAN, private_lan=True).get(_TRANSCRIPT)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "passkey unlock required"


def _ws(peer, private_lan):
    cfg = SimpleNamespace(
        tailnet_allowlist=[], terminal_allow_private_lan=private_lan,
        auth_token="", api_tokens=[], webauthn_rp_id="", webauthn_origin="",
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(webapp_config=cfg)),
        client=SimpleNamespace(host=peer[0]),
        headers={}, query_params={}, url=SimpleNamespace(path="/ws"),
    )


def test_websocket_gate_follows_the_switch() -> None:
    assert terminal_gate.ws_terminal_gate(_ws(_LAN, False))[0] == 4403
    assert terminal_gate.ws_terminal_gate(_ws(_LAN, True)) is None
    assert terminal_gate.ws_terminal_gate(_ws(_PUBLIC, True))[0] == 4403


_SCREEN = "/api/screen/monitors"
_TAILNET = ("100.64.1.2", 50000)
_PC = ("127.0.0.1", 50000)


def _screen_client(webapp_client, monkeypatch, *, terminal, screen):
    from app.webapp.routers import desktop_screen

    monkeypatch.setattr(desktop_screen.desktop_capture, "list_monitors", lambda: [])
    _c, app, _o = webapp_client
    app.state.webapp_config.terminal_allow_private_lan = terminal
    app.state.webapp_config.screen_allow_private_lan = screen
    return TestClient(app, client=_LAN)


@pytest.mark.parametrize(("terminal", "screen"), [(False, False), (False, True), (True, False)])
def test_screen_http_needs_both_switches(webapp_client, monkeypatch, terminal, screen) -> None:
    resp = _screen_client(webapp_client, monkeypatch, terminal=terminal, screen=screen).get(_SCREEN)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "screen view is Tailscale-only"


def test_screen_http_passes_with_both_switches_on(webapp_client, monkeypatch) -> None:
    resp = _screen_client(webapp_client, monkeypatch, terminal=True, screen=True).get(_SCREEN)
    assert resp.status_code == 200, resp.text


def test_main_switch_alone_still_opens_the_transcript(webapp_client, monkeypatch) -> None:
    client = _screen_client(webapp_client, monkeypatch, terminal=True, screen=False)
    assert client.get(_TRANSCRIPT).status_code != 403


def test_screen_public_address_is_refused_with_both_switches_on(webapp_client, monkeypatch) -> None:
    _screen_client(webapp_client, monkeypatch, terminal=True, screen=True)
    _c, app, _o = webapp_client
    assert TestClient(app, client=_PUBLIC).get(_SCREEN).status_code == 403


def _ws_screen(peer, terminal, screen):
    ws = _ws(peer, terminal)
    ws.app.state.webapp_config.screen_allow_private_lan = screen
    return ws


def test_screen_websocket_needs_both_switches() -> None:
    gate = terminal_gate.ws_terminal_gate
    for terminal, screen in ((False, False), (False, True), (True, False)):
        refusal = gate(_ws_screen(_LAN, terminal, screen), label="screen", screen=True)
        assert refusal == (4403, "screen is Tailscale-only"), (terminal, screen)
    assert gate(_ws_screen(_LAN, True, True), label="screen", screen=True) is None
    assert gate(_ws_screen(_PUBLIC, True, True), label="screen", screen=True)[0] == 4403
    # The terminal socket does not care about the screen switch.
    assert gate(_ws_screen(_LAN, True, False), label="terminal") is None


def test_the_screen_socket_asks_the_screen_gate() -> None:
    """screen_ws passes screen=True; without it the main switch alone would
    open the whole desktop."""
    from pathlib import Path

    source = Path("app/webapp/routers/desktop_screen.py").read_text(encoding="utf-8")
    assert 'ws_terminal_gate(websocket, label="screen", screen=True)' in source


def test_tailnet_reaches_the_screen_with_no_switch(webapp_client, monkeypatch) -> None:
    _screen_client(webapp_client, monkeypatch, terminal=False, screen=False)
    _c, app, _o = webapp_client
    assert TestClient(app, client=_TAILNET).get(_SCREEN).status_code == 200


# --- who may flip the switches: POST /api/config ---------------------------


def _post(webapp_client, peer, body, headers=None):
    _c, app, _o = webapp_client
    return TestClient(app, client=peer).post("/api/config", json=body, headers=headers or {})


def test_the_pc_turns_the_switches_on_and_they_reach_disk(webapp_client) -> None:
    from src.webapp_config import load_webapp_config

    _c, app, overrides = webapp_client
    cfg = app.state.webapp_config
    assert (cfg.terminal_allow_private_lan, cfg.screen_allow_private_lan) == (False, False)

    assert _post(webapp_client, _PC, {"terminal_allow_private_lan": True}).status_code == 200
    assert _post(webapp_client, _PC, {"screen_allow_private_lan": True}).status_code == 200
    on_disk = load_webapp_config(overrides["tmp_webapp_cfg_path"])
    assert (on_disk.terminal_allow_private_lan, on_disk.screen_allow_private_lan) == (True, True)
    body = TestClient(app, client=_PC).get("/api/config").json()
    assert (body["terminal_allow_private_lan"], body["screen_allow_private_lan"]) == (True, True)


def test_the_screen_switch_cannot_be_on_while_the_main_one_is_off(webapp_client) -> None:
    resp = _post(webapp_client, _PC, {"screen_allow_private_lan": True})
    assert resp.status_code == 400
    assert webapp_client[1].state.webapp_config.screen_allow_private_lan is False


def test_turning_the_main_switch_off_turns_the_screen_off(webapp_client) -> None:
    _post(webapp_client, _PC, {"terminal_allow_private_lan": True, "screen_allow_private_lan": True})
    assert webapp_client[1].state.webapp_config.screen_allow_private_lan is True
    assert _post(webapp_client, _PC, {"terminal_allow_private_lan": False}).status_code == 200
    cfg = webapp_client[1].state.webapp_config
    assert (cfg.terminal_allow_private_lan, cfg.screen_allow_private_lan) == (False, False)


@pytest.mark.parametrize("junk", ["true", 1, None])
def test_the_switches_reject_non_booleans(webapp_client, junk) -> None:
    for key in ("terminal_allow_private_lan", "screen_allow_private_lan"):
        assert _post(webapp_client, _PC, {key: junk}).status_code == 400


def test_a_private_address_cannot_flip_a_switch_even_with_it_on(webapp_client) -> None:
    """The switch must not be reachable through the widening it grants."""
    _c, app, _o = webapp_client
    app.state.webapp_config.terminal_allow_private_lan = True
    resp = _post(webapp_client, _LAN, {"screen_allow_private_lan": True})
    assert resp.status_code == 403
    assert app.state.webapp_config.screen_allow_private_lan is False


def test_the_public_tunnel_cannot_flip_a_switch(webapp_client) -> None:
    resp = _post(webapp_client, _TAILNET, {"terminal_allow_private_lan": True}, {"Cf-Ray": "1"})
    assert resp.status_code == 403
    assert webapp_client[1].state.webapp_config.terminal_allow_private_lan is False


def test_a_tailnet_client_can_flip_a_switch(webapp_client) -> None:
    resp = _post(webapp_client, _TAILNET, {"terminal_allow_private_lan": True})
    assert resp.status_code == 200
    assert webapp_client[1].state.webapp_config.terminal_allow_private_lan is True


def test_a_tailnet_client_needs_the_passkey_when_one_is_configured(
    webapp_client, monkeypatch,
) -> None:
    from src.auth.webauthn_gate import WebAuthnGate

    monkeypatch.setattr(WebAuthnGate, "configured", staticmethod(lambda cfg: True))
    webapp_client[1].state.webauthn_gate = SimpleNamespace(valid_terminal_token=lambda t: False)
    resp = _post(webapp_client, _TAILNET, {"terminal_allow_private_lan": True})
    assert resp.status_code == 401
    assert webapp_client[1].state.webapp_config.terminal_allow_private_lan is False


def test_other_config_keys_stay_open_to_a_private_address(webapp_client) -> None:
    """Only the two switches are gated; ordinary settings keep their rules."""
    resp = _post(webapp_client, _LAN, {"copilot_effort": "low"})
    assert resp.status_code != 403


def test_reachability_tells_the_spa_the_truth_and_names_the_switch(webapp_client) -> None:
    _c, app, _o = webapp_client
    cfg = app.state.webapp_config

    def ask(private_lan):
        cfg.terminal_allow_private_lan = private_lan
        request = SimpleNamespace(
            app=app, client=SimpleNamespace(host=_LAN[0]), headers={},
        )
        return terminal_gate.terminal_reachability(request)

    off = ask(False)
    assert off["reachable"] is False
    assert "Allow private network without Tailscale" in off["reason"]
    assert ask(True) == {"reachable": True, "reason": "private-lan"}


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("10.1.2.3", True), ("172.16.0.1", True), ("172.31.255.255", True),
        ("192.168.0.1", True), ("169.254.1.1", True),
        ("fd00::1", True), ("fe80::1", True),
        ("172.32.0.1", False), ("8.8.8.8", False), ("2001:db8::1", False),
        ("testclient", False), ("", False),
    ],
)
def test_private_ranges(host, expected) -> None:
    cfg = SimpleNamespace(tailnet_allowlist=[], terminal_allow_private_lan=True)
    assert terminal_gate.client_allowed(host, cfg) is expected
