"""The Copilot side of ``POST /api/claude-code/sessions/{sid}/model`` and
``GET .../context``: the screen comes from the session-host's VT mirror and
the switch is confirmed by the session's own ``events.jsonl``. Nothing here
opens a socket: the screen read and the PTY are replaced by recorders."""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from tests.unit.board.test_plan_picker import _live, picker_client  # noqa: F401
from tests.unit.sessions.test_copilot_session import IDLE, NARROW, TYPED, WORKING


def _event(kind: str, **data: Any) -> str:
    return json.dumps({"type": kind, "data": data}) + "\n"


@pytest.fixture
def copilot_client(picker_client, monkeypatch, tmp_path):
    """One live Copilot PTY on ``gpt-6-luna`` whose screen is
    ``state["screen"]``; a typed ``/model <id>`` lands in its event log
    while ``state["applies"]``."""
    from app.webapp.routers import session_screen as router
    from src.session_view import copilot_session

    client, session, _show, typed = picker_client
    session.list_sessions.return_value = [_live(agent="copilot")]
    monkeypatch.setattr(router, "_MODEL_CONFIRM_S", 0.4)
    monkeypatch.setattr(router, "_MODEL_POLL_S", 0.02)
    log = tmp_path / "events.jsonl"
    log.write_text(_event("session.start", selectedModel="gpt-6-luna"), encoding="utf-8")
    state: Dict[str, Any] = {"screen": IDLE, "applies": True, "log": log}
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
                        "confirmed_dialog": False, "detail": None}
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
    client, _, state = copilot_client
    footer = IDLE[-3].replace("/ commands", "/ commands   Session: 5.62 AIC used")
    state["screen"] = IDLE[:-3] + [footer, "", ""]
    got = client.get("/api/claude-code/sessions/s1/context").json()
    assert (got["percent"], got["ai_credits"]) == (3, 5.62)
    # The footer without the figure, and a narrow terminal, carry none.
    for screen in (IDLE, NARROW):
        state["screen"] = screen
        assert client.get("/api/claude-code/sessions/s1/context").json()["ai_credits"] is None


def test_attached_sessions_name_copilots_logged_model(copilot_client):
    from src.board.sessions import attach_transcript_model
    _, _, state = copilot_client
    with state["log"].open("a", encoding="utf-8") as fh:
        fh.write(_event("session.model_change", newModel="gpt-5.6-sol"))
    out = attach_transcript_model([_live(agent="copilot")], {})
    assert out[0]["transcript_model"] == "gpt-5.6-sol"
