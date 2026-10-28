"""The session-host serves local processes only; requests carrying ``Origin``
are turned away before any route runs."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.session_host import server


def _fail_create(*args, **kwargs):
    raise AssertionError("SessionManager.create must not run")


def test_post_with_origin_is_refused_before_the_route(monkeypatch):
    monkeypatch.setattr(server.manager, "create", _fail_create)
    client = TestClient(server.app)

    resp = client.post(
        "/sessions",
        content='{"project_dir": "C:\\\\", "flags": ""}',
        headers={"Origin": "https://example.invalid", "Content-Type": "text/plain"},
    )

    assert resp.status_code == 403


def test_get_with_origin_is_refused():
    client = TestClient(server.app)

    resp = client.get("/sessions", headers={"Origin": "https://example.invalid"})

    assert resp.status_code == 403


def test_websocket_with_origin_is_refused(monkeypatch):
    def _fail_get(sid):
        raise AssertionError("the WebSocket route must not run")

    monkeypatch.setattr(server.manager, "get", _fail_get)
    client = TestClient(server.app)

    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(
            "/sessions/any/ws", headers={"Origin": "https://example.invalid"}
        ):
            pass
    assert closed.value.code == 4403


def test_local_client_without_origin_still_reaches_routes():
    client = TestClient(server.app)

    resp = client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
