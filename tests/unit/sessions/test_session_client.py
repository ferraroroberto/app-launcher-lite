"""The loopback session-host client (`src.clients.session`).

Focused on the request *shape* — these are thin `requests` wrappers, so we
stub the shared pooled session's `request` (issue #605 — calls go through
`_loopback_http.SESSION`, not the bare `requests.request` function) and
assert the JSON body the session-host receives.
"""

from __future__ import annotations

import time

import pytest

from src.clients import session as session_client


class _Resp:
    status_code = 200

    def json(self):
        return {"session_id": "s1"}


def test_create_session_includes_phone_dimensions(monkeypatch):
    """The phone's terminal size rides the /sessions create body so the PTY
    spawns at the right width for a ratatui TUI (issue #126)."""
    captured: dict = {}

    def fake_request(method, url, timeout=None, **kwargs):
        captured["method"] = method
        captured["json"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(session_client._loopback_http.SESSION, "request", fake_request)

    session_client.create_session(
        8466, r"C:\proj", "name", "", agent="codex", rows=50, cols=42
    )

    body = captured["json"]
    assert captured["method"] == "POST"
    assert body["rows"] == 50
    assert body["cols"] == 42
    assert body["agent"] == "codex"


def test_create_session_defaults_dimensions(monkeypatch):
    """Omitted dimensions fall back to the legacy 40×120 default."""
    captured: dict = {}

    def fake_request(method, url, timeout=None, **kwargs):
        captured["json"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(session_client._loopback_http.SESSION, "request", fake_request)

    session_client.create_session(8466, r"C:\proj", "name", "")

    assert captured["json"]["rows"] == 40
    assert captured["json"]["cols"] == 120


def test_create_session_includes_history_lines_when_given(monkeypatch):
    """The Settings-tab-configurable scrollback depth (issue #435
    follow-up) rides the /sessions create body when the caller passes one."""
    captured: dict = {}

    def fake_request(method, url, timeout=None, **kwargs):
        captured["json"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(session_client._loopback_http.SESSION, "request", fake_request)

    session_client.create_session(
        8466, r"C:\proj", "name", "", agent="codex", history_lines=5000,
    )

    assert captured["json"]["history_lines"] == 5000


def test_create_session_omits_history_lines_when_not_given(monkeypatch):
    """No override → the key is absent entirely, so the session-host falls
    back to its own default rather than receiving a spurious null/zero."""
    captured: dict = {}

    def fake_request(method, url, timeout=None, **kwargs):
        captured["json"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(session_client._loopback_http.SESSION, "request", fake_request)

    session_client.create_session(8466, r"C:\proj", "name", "")

    assert "history_lines" not in captured["json"]


# ---------------------------------------------------------------- send_input
#
# A just-spawned Copilot discards text written before it is ready while the
# host still reports the write as delivered, so ``send_input`` holds the write
# until the session has set its window title and stopped painting its boot
# screen, and refuses, with nothing sent, when the TUI never comes up.


class _Host:
    """A stand-in session-host: serves ``GET /sessions/<id>`` from a script
    (the last entry repeats) and records every ``POST .../input``."""

    def __init__(self, infos):
        self.infos = list(infos)
        self.posts: list = []
        self.gets = 0

    def __call__(self, method, port, path, **kwargs):
        if method == "GET":
            self.gets += 1
            info = self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]
            if isinstance(info, Exception):
                raise info
            return info
        self.posts.append((path, kwargs.get("json")))
        return {"delivered": True, "reason": "unverified"}


def _info(*, title="GitHub Copilot", age=30.0, quiet_for=5.0,
          agent="copilot", kind="pty", alive=True):
    now = time.time()
    return {
        "session_id": "s1", "kind": kind, "agent": agent, "alive": alive,
        "live_title": title, "started_at": now - age,
        "last_output_at": now - quiet_for,
    }


@pytest.fixture
def host(monkeypatch):
    def install(infos, wait=0.3):
        h = _Host(infos)
        monkeypatch.setattr(session_client, "_request", h)
        monkeypatch.setattr(session_client, "_READY_POLL_S", 0.01)
        monkeypatch.setattr(session_client, "_READY_WAIT_S", wait)
        return h
    return install


def test_send_input_waits_for_the_tui_then_writes(host):
    h = host([_info(title="", age=1), _info(title="", age=1), _info(age=2)])

    session_client.send_input(8466, "s1", "hello")

    assert h.posts == [("/sessions/s1/input", {"data": "hello", "submit": True})]
    assert h.gets == 3


def test_send_input_waits_out_the_boot_paint(host):
    """The title is set ~0.1 s before Copilot reads input; a message sent in
    that gap was lost, so it waits for the boot output to stop too."""
    h = host([_info(age=2, quiet_for=0.1), _info(age=2, quiet_for=0.1), _info(age=3)])

    session_client.send_input(8466, "s1", "hello")

    assert len(h.posts) == 1
    assert h.gets == 3


def test_send_input_refuses_when_the_tui_never_comes_up(host):
    h = host([_info(title="", age=1)], wait=0.05)

    with pytest.raises(session_client.SessionNotReady) as err:
        session_client.send_input(8466, "s1", "hello")

    assert err.value.status == 409
    assert "NOT delivered" in str(err.value)
    assert h.posts == []


def test_send_input_writes_into_an_agent_that_keeps_painting(host):
    """Up but busy (already working): it takes type-ahead, so the cap sends."""
    h = host([_info(age=2, quiet_for=0.0)], wait=0.05)

    session_client.send_input(8466, "s1", "hello")

    assert len(h.posts) == 1


@pytest.mark.parametrize(
    "info",
    [
        _info(),
        _info(title="", age=600.0),
        _info(title="", alive=False),
        _info(title="", agent="other"),
        _info(title="", kind="remote"),
    ],
    ids=["ready", "old", "dead", "other-agent", "detached"],
)
def test_send_input_writes_at_once_when_not_starting(host, info):
    h = host([info])

    session_client.send_input(8466, "s1", "hello")

    assert len(h.posts) == 1


def test_send_input_writes_when_the_session_cannot_be_read(host):
    """The write itself then reports the real state (404, host down)."""
    h = host([session_client.SessionHostError("down", status=503)])

    session_client.send_input(8466, "s1", "hello")

    assert len(h.posts) == 1
