"""The Remote session switch reaches every Copilot launch, and only those."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.webapp.routers import _helpers
from src.webapp_config import WebappConfig


def _spawn(*_args, **_kwargs):
    return {"session_id": "s1"}


def _launch(agent: str, *, remote: bool) -> None:
    cfg = WebappConfig(copilot_remote_session=remote)
    asyncio.run(_helpers.spawn_launcher_session(
        _spawn, cfg, project_dir=Path("."), name="n", flags="", agent=agent,
        rows=40, cols=120,
    ))


@pytest.mark.parametrize("remote", [True, False])
def test_a_copilot_launch_applies_the_switch(copilot_remote_calls, remote):
    _launch("copilot", remote=remote)
    assert copilot_remote_calls == [remote]


def test_another_agent_launch_leaves_copilot_settings_alone(copilot_remote_calls):
    _launch("claude", remote=True)
    assert copilot_remote_calls == []


def test_the_switch_defaults_on_and_round_trips(webapp_client):
    client, app, _ = webapp_client
    assert WebappConfig().copilot_remote_session is True
    assert client.get("/api/config").json()["copilot"]["remote_session"] is True
    assert client.post("/api/config", json={"copilot_remote_session": False}).status_code == 200
    assert app.state.webapp_config.copilot_remote_session is False
    assert client.get("/api/config").json()["copilot"]["remote_session"] is False


def test_team_os_launch_applies_the_switch(webapp_client, tmp_path, monkeypatch, copilot_remote_calls):
    from src import middleware
    from app.webapp.routers import team_os as team_os_router

    monkeypatch.setattr(
        middleware.trust, "LOOPBACK_HOSTS", frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )
    client, app, _ = webapp_client
    skill = tmp_path / "team-os" / ".claude" / "skills" / "journal-daily"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: journal-daily\ndescription: d\n---\n# journal-daily\n", encoding="utf-8",
    )
    cfg = app.state.webapp_config
    cfg.team_os_dir = str(tmp_path / "team-os")
    cfg.copilot_remote_session = False
    monkeypatch.setattr(team_os_router, "spawn_claude_session", lambda *a, **k: {"session_id": "t1"})
    monkeypatch.setattr(team_os_router, "open_local_terminal_window", lambda *a, **k: None)

    resp = client.post("/api/team-os/skills/journal-daily/launch", json={"mode": "pty"})
    assert resp.status_code == 200, resp.text
    assert copilot_remote_calls == [False]
