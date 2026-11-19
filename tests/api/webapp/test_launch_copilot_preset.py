"""A Coding launch carries a Copilot launch preset's model, effort and context."""

from __future__ import annotations

import pytest

from src.launching.flags import build_copilot_flags
from src.webapp_config import WebappConfig


@pytest.fixture
def launch(webapp_client, monkeypatch):
    """POST a launch of ``live-proj``; returns ``(post, spawned)`` where
    ``spawned`` holds the arguments of the last spawn."""
    client, app, overrides = webapp_client
    from app.webapp.routers import apps as apps_router

    (overrides["tmp_projects_dir"] / "live-proj").mkdir()
    monkeypatch.setattr(apps_router.agents, "is_installed", lambda _: True)
    spawned: dict = {}

    def fake_spawn(project_dir, name, flags, port, kind="pty", agent="copilot",
                   rows=40, cols=120, history_lines=None, label=""):
        spawned.update(flags=flags, kind=kind, agent=agent)
        return {"session_id": "s1", "kind": kind, "agent": agent}

    monkeypatch.setattr(apps_router, "spawn_claude_session", fake_spawn)
    cfg = app.state.webapp_config
    cfg.copilot_models = ["gpt-6-luna", "mai-code-2-flash"]
    cfg.copilot_model = ""
    cfg.copilot_effort = "xhigh"
    cfg.copilot_context = "long_context"

    def post(**body):
        spawned.clear()
        return client.post("/api/apps/live-proj/launch", json={"agent": "copilot", **body})

    return post, spawned


class TestPresetLaunch:
    @pytest.mark.parametrize("effort", ["high", "medium"])
    def test_flash_launches_with_its_model_and_effort(self, launch, effort):
        post, spawned = launch
        resp = post(model="copilot:mai-code-2-flash", effort=effort)
        assert resp.status_code == 200, resp.text
        tokens = spawned["flags"].split()
        assert tokens[tokens.index("--model") + 1] == "mai-code-2-flash"
        assert tokens[tokens.index("--effort") + 1] == effort
        # No preset context: the saved tier still applies.
        assert tokens[tokens.index("--context") + 1] == "long_context"

    def test_a_preset_context_replaces_the_saved_tier(self, launch):
        post, spawned = launch
        assert post(model="copilot:gpt-6-luna", effort="xhigh", context="default").status_code == 200
        tokens = spawned["flags"].split()
        assert tokens[tokens.index("--context") + 1] == "default"
        assert tokens[tokens.index("--effort") + 1] == "xhigh"

    def test_resume_carries_the_preset_too(self, launch):
        post, spawned = launch
        assert post(model="copilot:mai-code-2-flash", effort="high", resume=True).status_code == 200
        assert spawned["flags"].startswith("--resume ")
        assert "--model mai-code-2-flash" in spawned["flags"] and "--effort high" in spawned["flags"]

    def test_no_preset_is_the_saved_launch_unchanged(self, launch, webapp_client):
        post, spawned = launch
        assert post().status_code == 200
        assert spawned["flags"] == build_copilot_flags(webapp_client[1].state.webapp_config)
        assert "--model" not in spawned["flags"]

    @pytest.mark.parametrize("body, fragment", [
        ({"model": "copilot:not-listed", "effort": "high"}, "not in the Copilot model list"),
        ({"model": "copilot:mai-code-2-flash", "effort": "xhigh"}, "not offered by copilot:mai-code-2-flash"),
        ({"effort": "high"}, "no model sent"),
        ({"model": "copilot:mai-code-2-flash", "context": "huge"}, "context 'huge' is not offered"),
        ({"model": "claude:opus"}, "does not belong to copilot"),
    ])
    def test_refuses_what_the_launch_cannot_honour(self, launch, body, fragment):
        post, spawned = launch
        resp = post(**body)
        assert resp.status_code == 400, resp.text
        assert fragment in resp.json()["detail"]
        assert "flags" not in spawned


def test_flags_put_a_flash_preset_ahead_of_the_saved_settings():
    cfg = WebappConfig(copilot_effort="xhigh", copilot_context="long_context")
    flags = build_copilot_flags(
        cfg, model_override="mai-code-2-flash", effort_override="medium",
        context_override="default",
    ).split()
    assert flags[flags.index("--model") + 1] == "mai-code-2-flash"
    assert flags[flags.index("--effort") + 1] == "medium"
    assert flags[flags.index("--context") + 1] == "default"
