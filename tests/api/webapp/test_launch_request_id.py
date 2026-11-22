"""A Coding launch carrying a request id is never spawned twice, and the roster probe answers alone."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.board import roster
from src.launching import launch_requests


@pytest.fixture
def launch(webapp_client, monkeypatch):
    """``(post, spawns, client)``: POST a launch of ``live-proj`` and the list of spawns made."""
    client, _app, overrides = webapp_client
    from app.webapp.routers import apps as apps_router

    (overrides["tmp_projects_dir"] / "live-proj").mkdir()
    monkeypatch.setattr(apps_router.agents, "is_installed", lambda _: True)
    spawns: list = []

    def fake_spawn(project_dir, name, flags, port, kind="pty", agent="copilot",
                   rows=40, cols=120, history_lines=None, label=""):
        spawns.append(name)
        return {"session_id": f"s{len(spawns)}", "kind": kind, "agent": agent}

    monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)

    def post(**body):
        return client.post("/api/apps/live-proj/launch", json={"agent": "copilot", **body})

    return post, spawns, client


def test_a_repeat_with_the_same_request_id_answers_from_the_first_launch(launch):
    post, spawns, client = launch
    first, again = post(request_id="r-1"), post(request_id="r-1")
    assert first.status_code == again.status_code == 200, again.text
    assert spawns == ["live-proj"]
    assert first.json()["resumed"] is False and again.json()["resumed"] is True
    assert again.json()["session"]["session_id"] == first.json()["session"]["session_id"] == "s1"
    record = client.get("/api/apps/live-proj/launch/r-1").json()
    assert (record["state"], record["session"]["session_id"]) == ("done", "s1")
    assert post(request_id="r-2").json()["session"]["session_id"] == "s2", "a new id is a new launch"


def test_a_repeat_while_the_first_is_spawning_is_202_and_spawns_nothing(launch, webapp_client):
    post, spawns, client = launch
    from app.webapp.routers import apps as apps_router
    apps_router._launch_requests(webapp_client[1].state).claim("live-proj", "r-1")
    resp = post(request_id="r-1")
    assert (resp.status_code, resp.json()["state"]) == (202, "pending")
    assert spawns == []
    assert client.get("/api/apps/live-proj/launch/r-1").json()["state"] == "pending"


def test_a_refused_launch_is_refused_again_on_repeat_without_a_second_spawn(launch, monkeypatch):
    post, spawns, client = launch
    from app.webapp.routers import apps as apps_router

    def refusing_spawn(*_a, **_kw):
        spawns.append("refused")
        raise OSError("project dir unreadable")

    monkeypatch.setattr(apps_router, "spawn_claude_session", refusing_spawn)
    assert post(request_id="r-1").status_code == post(request_id="r-1").status_code == 400
    assert spawns == ["refused"]
    record = client.get("/api/apps/live-proj/launch/r-1").json()
    assert (record["state"], record["status"]) == ("failed", 400)


def test_unknown_and_malformed_request_ids(launch):
    post, spawns, client = launch
    assert client.get("/api/apps/live-proj/launch/nope").status_code == 404
    assert post(request_id="has spaces/slash").status_code == 400
    assert spawns == []


def test_the_spawn_finishes_into_its_slot_after_the_caller_has_gone():
    """The handler waits on the spawn shielded: a caller that disconnects (its handler
    cancelled) still leaves the finished launch for its repeat or its poll."""
    from app.webapp.routers import apps as apps_router

    async def scenario():
        finished = asyncio.Event()

        async def slow_launch(*_a):
            await asyncio.sleep(0.05)
            finished.set()
            return {"session": {"session_id": "late"}}

        state = SimpleNamespace()
        request = SimpleNamespace(app=SimpleNamespace(state=state))
        entry = SimpleNamespace(id="live-proj")
        original = apps_router._launch_coding
        apps_router._launch_coding = slow_launch
        try:
            handler = asyncio.ensure_future(apps_router._launch_coding_once(entry, None, request, "r-1"))
            await asyncio.sleep(0.01)
            handler.cancel()
            await finished.wait()
            await asyncio.sleep(0)
        finally:
            apps_router._launch_coding = original
        return state.launch_requests.get("live-proj", "r-1")

    slot = asyncio.run(scenario())
    assert slot.state == launch_requests.DONE
    assert slot.as_dict()["session"] == {"session_id": "late"}


def test_roster_rows_read_only_the_hosts_own_record():
    now = 1000.0
    rows = roster.roster_rows([
        {"session_id": "a", "project_dir": "C:/x/demo", "alive": True, "live_title": "t", "last_output_at": 999.0},
        {"session_id": "b", "project_dir": "C:/x/other", "alive": True, "live_title": "t", "last_output_at": 900.0},
        {"session_id": "c", "project_dir": "C:/x/fresh", "alive": True, "live_title": None},
        {"session_id": "d", "project_dir": "C:/x/console", "alive": True, "kind": "remote"},
        {"session_id": "e", "project_dir": "C:/x/quiet", "alive": True, "live_title": "t"},
        {"session_id": "f", "project_dir": "C:/x/gone", "alive": False},
    ], now=now)
    assert [(r["project"], r["state"]) for r in rows] == [
        ("demo", "working"), ("other", "awaiting-input"), ("fresh", "spawned"),
        ("console", "unknown"), ("quiet", "unknown"),
    ]


def test_the_roster_route_is_503_when_the_host_does_not_answer(webapp_client):
    client, _app, _overrides = webapp_client
    from app.webapp.routers import sessions as sessions_router
    host = sessions_router.session_client
    host.list_sessions_bounded = AsyncMock(return_value=[
        {"session_id": "a", "project_dir": "C:/x/demo", "alive": True, "live_title": None}])
    body = client.get("/api/claude-code/roster").json()
    assert body["source"] == "session-host"
    assert [(r["project"], r["state"]) for r in body["sessions"]] == [("demo", "spawned")]
    host.list_sessions_bounded = AsyncMock(side_effect=host.SessionHostError("timed out", status=503))
    resp = client.get("/api/claude-code/roster")
    assert resp.status_code == 503 and "not confirmed" in resp.json()["detail"]
