"""``probe_tray``: a registered tray is ready only when its /healthz and /api/version answer.

The probe runs against real throwaway listeners, so what is proven is the HTTP behaviour (a bound
port that does not serve, a build that is not the repo HEAD), not a mock of it.
"""

from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator, Optional

import pytest

import app.tray.registered_trays as rt_mod
from app.tray.registered_trays import (
    TRAY_ANSWERING,
    TRAY_BOUND_NOT_ANSWERING,
    TRAY_DOWN,
    TRAY_STALE,
    probe_tray,
)

_HEAD = "0123456789abcdef0123456789abcdef01234567"


@contextmanager
def _serve(health_status: int = 200, version: Optional[dict] = None) -> Iterator[int]:
    """A plain-HTTP server answering /healthz with ``health_status`` and /api/version with ``version``."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
            if self.path == "/healthz":
                status, body = health_status, b"ok"
            elif self.path == "/api/version" and version is not None:
                status, body = 200, json.dumps(version).encode()
            else:
                status, body = 404, b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # port-host-exempt: an ephemeral loopback listener
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _head(monkeypatch):
    monkeypatch.setattr(rt_mod, "run_git", lambda repo, args, **kw: _HEAD)


def test_nothing_listening_is_down() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))  # port-host-exempt: an ephemeral loopback port
        port = s.getsockname()[1]
    assert probe_tray(port).state == TRAY_DOWN


def test_a_port_that_accepts_but_serves_nothing_is_bound_not_answering() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))  # port-host-exempt: an ephemeral loopback port
        listener.listen(8)
        stop = threading.Event()

        def _accept_and_drop() -> None:
            listener.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except OSError:
                    continue
                conn.close()

        thread = threading.Thread(target=_accept_and_drop, daemon=True)
        thread.start()
        try:
            result = probe_tray(listener.getsockname()[1])
        finally:
            stop.set()
            thread.join(timeout=2)
    assert result.state == TRAY_BOUND_NOT_ANSWERING
    assert "accepts connections" in result.detail


def test_a_failing_healthz_is_bound_not_answering_not_ready() -> None:
    with _serve(health_status=503, version={"git_sha": _HEAD}) as port:
        result = probe_tray(port)
    assert result.state == TRAY_BOUND_NOT_ANSWERING
    assert "/healthz: HTTP 503" in result.detail


def test_a_missing_api_version_is_bound_not_answering() -> None:
    with _serve(version=None) as port:
        result = probe_tray(port)
    assert result.state == TRAY_BOUND_NOT_ANSWERING
    assert "/api/version: HTTP 404" in result.detail


def test_a_git_sha_that_is_not_the_repo_head_is_stale() -> None:
    with _serve(version={"git_sha": "deadbeef"}) as port:
        result = probe_tray(port, repo_dir=rt_mod.PROJECT_ROOT)
    assert result.state == TRAY_STALE
    assert "deadbeef" in result.detail


def test_a_matching_short_git_sha_is_answering() -> None:
    with _serve(version={"git_sha": _HEAD[:7]}) as port:
        result = probe_tray(port, repo_dir=rt_mod.PROJECT_ROOT)
    assert result.state == TRAY_ANSWERING


def test_no_reported_git_sha_is_answering_with_the_build_uncompared() -> None:
    with _serve(version={"pid": 1}) as port:
        result = probe_tray(port, repo_dir=rt_mod.PROJECT_ROOT)
    assert result.state == TRAY_ANSWERING
    assert "no git_sha" in result.detail


def test_an_unreadable_head_leaves_the_build_uncompared_and_says_so(monkeypatch) -> None:
    monkeypatch.setattr(rt_mod, "run_git", lambda repo, args, **kw: None)
    with _serve(version={"git_sha": "deadbeef"}) as port:
        result = probe_tray(port, repo_dir=rt_mod.PROJECT_ROOT)
    assert result.state == TRAY_ANSWERING
    assert "not compared" in result.detail
