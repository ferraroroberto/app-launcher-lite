"""A message send runs on its own pool, names each stage's time, and never sends twice.

``POST /api/claude-code/sessions/{sid}/input`` used to run on the loop's
default executor, shared with every route without a pool of its own, so with
the phone polling ten things at once a send queued behind reads. These tests
fill that executor with stalled work and check that a send still answers, pin
the stage breadcrumb against injected stage times (a fake clock, not load), and
check that a dropped or timed-out session-host call is never retried.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest
import requests

from app.webapp import input_pool
from src.clients import session as session_client
from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOKEN_SCRIPT = _REPO_ROOT / "tests" / "js" / "send_token_timing.test.mjs"
_HANG_S = 6.0
_SEND_BOUND_S = 1.0
_DEFAULT_WORKERS = 2
_SEND = "/api/claude-code/sessions/s1/input"

_CONFIRMED = {
    "delivered": True, "ingested": True, "submitted": True, "submit_confirmed": True,
    "reason": "ok", "submit_state": "confirmed",
}


@pytest.fixture
def send_app(webapp_client, monkeypatch):
    from src import middleware

    monkeypatch.setattr(middleware.trust, "LOOPBACK_HOSTS",
                        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}))
    client, app, overrides = webapp_client
    overrides["session"].send_input.return_value = dict(_CONFIRMED)
    return client, app, overrides["session"]


def _breadcrumbs(caplog) -> list:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("📨 send")]


def test_a_send_answers_while_the_default_executor_is_full(send_app):
    """Every default-executor thread is held by a stalled call; the send, its
    audit write included, still answers at once."""
    _client, app, _session = send_app
    release = threading.Event()

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=_DEFAULT_WORKERS))
        stalls = [
            asyncio.create_task(asyncio.to_thread(release.wait, _HANG_S))
            for _ in range(_DEFAULT_WORKERS + 2)
        ]
        await asyncio.sleep(0.1)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                started = time.perf_counter()
                sent = await client.post(_SEND, json={"data": "hi", "submit": True})
                return time.perf_counter() - started, sent.status_code
        finally:
            release.set()
            await asyncio.gather(*stalls)

    send_s, status = asyncio.run(run())
    assert status == 200
    assert send_s < _SEND_BOUND_S, f"a send waited {send_s:.2f}s behind the shared default executor"


def test_the_send_runs_on_the_input_pool(send_app):
    client, _app, session = send_app
    seen: Dict[str, str] = {}

    def record(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        seen["thread"] = threading.current_thread().name
        return dict(_CONFIRMED)

    session.send_input.side_effect = record
    assert client.post(_SEND, json={"data": "hi", "submit": True}).status_code == 200
    assert seen["thread"].startswith("input")


def test_the_breadcrumb_names_each_stage(send_app, caplog):
    """Readiness and host times from the client, ingest and confirm from the
    host's verdict, the token step from the page's headers."""
    client, _app, session = send_app

    def deliver(*_args: Any, stages: Dict[str, Any], **_kwargs: Any) -> Dict[str, Any]:
        stages.update(ready_ms=120.0, host_ms=4600.0)
        return {**_CONFIRMED, "waited_ms": 3000, "written_at": 100.0, "submitted_at": 104.5}

    session.send_input.side_effect = deliver
    caplog.set_level(logging.INFO, logger="launcher.slowreq")
    resp = client.post(
        _SEND, json={"data": "hi", "submit": True},
        headers={"X-Send-Token-Ms": "12", "X-Send-Token-Source": "cached"},
    )
    assert resp.status_code == 200
    [line] = _breadcrumbs(caplog)
    assert "token cached 12ms" in line
    assert "ready 120ms" in line
    assert "host 4600ms (ingest 3000ms, confirm 1500ms)" in line
    assert line.endswith("ok/confirmed")
    assert "queue unknown" not in line


def test_a_fresh_passkey_and_a_missing_report_are_told_apart(send_app, caplog):
    client, _app, _session = send_app
    caplog.set_level(logging.INFO, logger="launcher.slowreq")
    client.post(
        _SEND, json={"data": "hi", "submit": True},
        headers={"X-Send-Token-Ms": "2400", "X-Send-Token-Source": "passkey"},
    )
    client.post(_SEND, json={"data": "hi", "submit": True})
    client.post(
        _SEND, json={"data": "hi", "submit": True},
        headers={"X-Send-Token-Ms": "soon", "X-Send-Token-Source": "made-up"},
    )
    passkey, missing, malformed = _breadcrumbs(caplog)
    assert "token passkey 2400ms" in passkey
    assert "token unknown unknown" in missing
    assert "token unknown unknown" in malformed
    # A verdict without the host's timing fields leaves both windows unknown.
    assert "(ingest unknown, confirm unknown)" in missing


def test_a_refused_send_logs_its_stages_and_status(send_app, caplog):
    client, _app, session = send_app
    session.send_input.side_effect = session.SessionHostError(
        "session s1 not accepting input (exited)", status=409
    )
    caplog.set_level(logging.INFO, logger="launcher.slowreq")
    assert client.post(_SEND, json={"data": "hi", "submit": True}).status_code == 409
    [line] = _breadcrumbs(caplog)
    assert line.endswith("refused 409")


def test_the_confirm_window_is_unknown_without_a_submit():
    windows = input_pool.host_windows(
        {"waited_ms": 800, "written_at": 10.0, "submitted_at": None}
    )
    assert windows == {"ingest_ms": 800.0, "confirm_ms": None}


# ------------------------------------------------------- the client's stages


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def test_send_input_times_the_readiness_wait_and_the_host_call(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(session_client.time, "monotonic", clock.monotonic)

    def ready(_port, _sid):
        clock.now += 0.25
        return None

    def host(method, port, path, **kwargs):
        clock.now += 3.5
        return dict(_CONFIRMED)

    monkeypatch.setattr(session_client, "_await_tui_ready", ready)
    monkeypatch.setattr(session_client, "_request", host)
    stages: Dict[str, Any] = {}
    session_client.send_input(8466, "s1", "hello", True, stages=stages)
    assert stages == {"ready_ms": 250.0, "host_ms": 3500.0}


def test_a_readiness_refusal_still_reports_its_wait(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(session_client.time, "monotonic", clock.monotonic)

    def never_ready(_port, _sid):
        clock.now += 15.0
        raise session_client.SessionNotReady("still starting", status=409)

    monkeypatch.setattr(session_client, "_await_tui_ready", never_ready)
    stages: Dict[str, Any] = {}
    with pytest.raises(session_client.SessionNotReady):
        session_client.send_input(8466, "s1", "hello", True, stages=stages)
    assert stages == {"ready_ms": 15000.0}


# --------------------------------------------- one write, never a second one


class _Transport:
    """Stands in for the pooled HTTP session: fails the input POST with ``exc``."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.posts = 0

    def __call__(self, method, url, **kwargs):
        if method == "POST":
            self.posts += 1
        raise self.exc


@pytest.fixture
def no_ready_wait(monkeypatch):
    monkeypatch.setattr(session_client, "_await_tui_ready", lambda _p, _s: None)


def test_a_dropped_connection_is_not_retried(monkeypatch, no_ready_wait):
    """The host may have read the body before the drop; a retry would type the text twice."""
    transport = _Transport(requests.exceptions.ConnectionError("Connection aborted."))
    monkeypatch.setattr(session_client._loopback_http.SESSION, "request", transport)
    with pytest.raises(session_client.SessionHostError) as raised:
        session_client.send_input(8466, "s1", "hello", True)
    assert transport.posts == 1
    assert raised.value.status == 503


def test_a_timed_out_send_says_the_text_may_be_there(monkeypatch, no_ready_wait):
    """A read timeout is not "unreachable": the host took the call, so the
    answer warns against a resend instead of inviting one."""
    transport = _Transport(requests.exceptions.ReadTimeout("read timed out"))
    monkeypatch.setattr(session_client._loopback_http.SESSION, "request", transport)
    with pytest.raises(session_client.SessionHostError) as raised:
        session_client.send_input(8466, "s1", "hello", True)
    assert transport.posts == 1
    assert raised.value.status == 504
    assert "may already be in the session" in str(raised.value)


def test_other_calls_still_retry_a_dropped_connection(monkeypatch):
    calls = {"n": 0}

    class _Ok:
        status_code = 200

        def json(self):
            return {}

    def flaky(method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("stale socket")
        return _Ok()

    monkeypatch.setattr(session_client._loopback_http.SESSION, "request", flaky)
    session_client.rename(8466, "s1", "title")
    assert calls["n"] == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_the_page_reports_its_token_step() -> None:
    """Cached token: no ceremony. Missing: the ceremony first. A failed ceremony sends nothing."""
    result = subprocess.run(
        ["node", str(_TOKEN_SCRIPT)],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.stdout.splitlines()[-1] == "OK"
