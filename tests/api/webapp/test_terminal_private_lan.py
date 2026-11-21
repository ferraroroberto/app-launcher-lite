"""``terminal_allow_private_lan``: a phone on the trusted LAN reaches the terminal.

The terminal-grade routes (transcript, input, image, WebSocket) accept
loopback, the Tailscale range and ``tailnet_allowlist``. The switch adds the
private ranges to that one verdict, so HTTP, the WebSocket and the SPA's
up-front reachability answer move together. Off (the default) nothing changes.
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
    assert "terminal_allow_private_lan" in off["reason"]
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
