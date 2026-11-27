"""``POST /api/claude-code/sessions/{sid}/model`` and ``GET .../context``: the
screen comes from the session-host's VT mirror and the switch is confirmed by
the session's own ``events.jsonl``. Nothing here opens a socket: the screen
read and the PTY are replaced by recorders."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from tests.unit.sessions.test_copilot_session import IDLE, NARROW, TYPED, WORKING


def _live(kind: str = "pty", agent: str = "copilot") -> Dict[str, Any]:
    return {"session_id": "s1", "kind": kind, "agent": agent, "alive": True,
            "rows": 40, "cols": 60, "project_dir": "E:/automation/proj"}


def _event(kind: str, **data: Any) -> str:
    return json.dumps({"type": kind, "data": data}) + "\n"


@pytest.fixture
def copilot_client(webapp_client, monkeypatch, tmp_path):
    """One live Copilot PTY on ``gpt-6-luna`` whose screen is
    ``state["screen"]``; a typed ``/model <id>`` lands in its event log
    while ``state["applies"]``."""
    from app.webapp.routers import session_screen as router
    from src import middleware
    from src.session_view import copilot_session

    monkeypatch.setattr(middleware.trust, "LOOPBACK_HOSTS",
                        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}))
    client, _, overrides = webapp_client
    session = overrides["session"]
    session.list_sessions.return_value = [_live()]
    typed: List[Any] = []
    router._COPILOT_LAST.clear()
    router._CONTEXT_SEEN.clear()
    monkeypatch.setattr(router, "_KEY_GAP_S", 0)
    state_session = session
    monkeypatch.setattr(router, "_MODEL_CONFIRM_S", 0.4)
    monkeypatch.setattr(router, "_MODEL_POLL_S", 0.02)
    log = tmp_path / "events.jsonl"
    log.write_text(_event("session.start", selectedModel="gpt-6-luna"), encoding="utf-8")
    state: Dict[str, Any] = {"screen": IDLE, "applies": True, "log": log, "session": state_session}
    monkeypatch.setattr(copilot_session, "event_log", lambda sess, fresh=False: state["log"])

    async def fake_screen(port, sid, rows, cols):
        return state["screen"]

    async def fake_pty(port, sid, keys):
        typed.append(keys)
        command = keys[0][0]
        if state["applies"] and command.startswith("/model "):
            with log.open("a", encoding="utf-8") as fh:
                fh.write(_event("session.model_change", newModel=command.split()[1]))

    monkeypatch.setattr(router, "read_vt_screen", fake_screen)
    monkeypatch.setattr(router, "_type_into_pty", fake_pty)
    return client, typed, state


def _post(client, model: Any = "mai-code-2-flash"):
    return client.post("/api/claude-code/sessions/s1/model", json={"model": model})


def test_a_copilot_switch_is_confirmed_by_its_event_log(copilot_client):
    client, typed, _ = copilot_client
    r = _post(client)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "outcome": "switched", "model": "mai-code-2-flash",
                        "detail": None}
    assert typed == [[("/model mai-code-2-flash", True)]]


def test_the_model_already_running_types_nothing(copilot_client):
    client, typed, _ = copilot_client
    assert _post(client, "gpt-6-luna").json()["outcome"] == "switched"
    assert typed == []


def test_a_switch_the_log_never_records_is_not_confirmed(copilot_client):
    client, _, state = copilot_client
    state["applies"] = False
    body = _post(client).json()
    assert body["outcome"] == "unconfirmed" and "didn't report" in body["detail"]


@pytest.mark.parametrize("screen,why", [
    (WORKING, "working"), (TYPED, "holds text"), (None, "can't see"),
])
def test_nothing_is_typed_unless_copilot_sits_at_an_empty_prompt(copilot_client, screen, why):
    client, typed, state = copilot_client
    state["screen"] = screen
    r = _post(client)
    assert r.status_code == 409 and why in r.json()["detail"]
    assert typed == []


def test_no_event_log_means_nothing_is_typed(copilot_client):
    client, typed, state = copilot_client
    state["log"] = None
    r = _post(client)
    assert r.status_code == 409 and "first prompt" in r.json()["detail"]
    assert typed == []


def test_a_model_off_the_copilot_list_is_refused(copilot_client):
    client, typed, _ = copilot_client
    assert _post(client, "opus").status_code == 400
    assert typed == []


@pytest.mark.parametrize("screen,body", [
    (IDLE, {"available": True, "percent": 3, "reason": None}),
    (NARROW, {"available": True, "percent": None, "reason": "not_showing"}),
    (None, {"available": False, "percent": None, "reason": "no_screen"}),
])
def test_the_context_ring_reads_copilots_status_line(copilot_client, screen, body):
    client, _, state = copilot_client
    state["screen"] = screen
    got = client.get("/api/claude-code/sessions/s1/context").json()
    assert {k: got[k] for k in body} == body


def test_the_context_route_carries_the_footers_ai_credits(copilot_client):
    from app.webapp.routers import session_screen as router
    router._COPILOT_LAST.clear()
    client, _, state = copilot_client
    footer = IDLE[-3].replace("/ commands", "/ commands   Session: 5.62 AIC used")
    state["screen"] = IDLE[:-3] + [footer, "", ""]
    got = client.get("/api/claude-code/sessions/s1/context").json()
    assert (got["percent"], got["ai_credits"]) == (3, 5.62)
    # The footer without the figure, and a narrow terminal, carry none.
    for screen in (IDLE, NARROW):
        router._COPILOT_LAST.clear()
        state["screen"] = screen
        assert client.get("/api/claude-code/sessions/s1/context").json()["ai_credits"] is None


def test_a_screen_without_the_figures_keeps_the_ring_the_session_had(copilot_client):
    """A repaint caught mid-draw reads no footer: the ring and the credits it
    showed a moment ago stay, and only a session that is gone clears them."""
    from app.webapp.routers import session_screen as router
    router._COPILOT_LAST.clear()
    client, _, state = copilot_client
    footer = IDLE[-3].replace("/ commands", "/ commands   Session: 5.62 AIC used")
    state["screen"] = IDLE[:-3] + [footer, "", ""]
    assert client.get("/api/claude-code/sessions/s1/context").json()["percent"] == 3
    state["screen"] = NARROW
    got = client.get("/api/claude-code/sessions/s1/context").json()
    assert (got["available"], got["percent"], got["ai_credits"]) == (True, 3, 5.62)
    router._COPILOT_LAST.clear()
    assert client.get("/api/claude-code/sessions/s1/context").json()["percent"] is None


def test_attached_sessions_name_copilots_logged_model(copilot_client):
    from src.board.sessions import attach_transcript_model
    _, _, state = copilot_client
    with state["log"].open("a", encoding="utf-8") as fh:
        fh.write(_event("session.model_change", newModel="gpt-5.6-sol"))
    out = attach_transcript_model([_live()])
    assert out[0]["transcript_model"] == "gpt-5.6-sol"


def test_the_switch_route_is_passkey_gated():
    from src.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/model") == "passkey"


@pytest.mark.parametrize("live,status,detail", [
    (_live(agent="other"), 409, "can't switch model"),
    (_live(kind="remote"), 409, "detached"),
])
def test_a_switch_that_cannot_be_driven_types_nothing(copilot_client, live, status, detail):
    client, typed, state = copilot_client
    state["session"].list_sessions.return_value = [live]
    r = _post(client)
    assert r.status_code == status and detail in r.json()["detail"]
    assert typed == []


def test_a_gone_session_and_an_unreachable_host_are_told_apart(copilot_client):
    from src.clients.session import SessionHostError

    client, typed, state = copilot_client
    state["session"].list_sessions.return_value = []
    assert _post(client).status_code == 409
    state["session"].list_sessions.side_effect = SessionHostError("timed out")
    assert _post(client).status_code == 503
    assert typed == []


def test_the_switch_is_audited_without_the_conversation(copilot_client, monkeypatch):
    from src import audit

    client, _, _ = copilot_client
    logged: List[Any] = []
    monkeypatch.setattr(audit, "session_log", lambda sid, event, **kw: logged.append((sid, event, kw)))
    _post(client)
    assert logged == [("s1", "model_switch", {"agent": "copilot", "model": "mai-code-2-flash",
                                              "reason": "switched"})]
