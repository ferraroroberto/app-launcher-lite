"""Board spawn helpers (``board_spawn``) — the per-launch model selector and
the readiness wait shared by the issue-start and fleet-chief launch paths.

The free-text dispatch endpoint (#302) that first exercised these is gone
(#1382); the spawn-then-type contract itself is still pinned end to end by
``tests/api/chief/`` (timeout kill, boot-never-quiescent cap) and
``tests/api/board/test_drawer_issue_start.py`` (chief-managed marking, #1283). What those go
through the HTTP routes for, this file reaches directly: the model allowlist
and the readiness edges (a dead session, a session-host that reports no
``output_chars``).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.webapp.routers import board_spawn


@pytest.fixture
def _fast_probe(monkeypatch):
    """Shrink the readiness constants so no test ever really waits."""
    monkeypatch.setattr(board_spawn, "DISPATCH_READY_CAP_S", 0.3)
    monkeypatch.setattr(board_spawn, "DISPATCH_SETTLE_S", 0.0)
    monkeypatch.setattr(board_spawn, "DISPATCH_POLL_S", 0.01)


class TestAwaitDispatchReady:

    def _host(self, monkeypatch, info):
        calls = []

        def fake_get(port, sid):
            calls.append((port, sid))
            return info

        monkeypatch.setattr(board_spawn.session_client, "get_session", fake_get)
        return calls

    def test_dead_session_504s_without_typing_into_it(
        self, _fast_probe, monkeypatch
    ):
        self._host(monkeypatch, {"alive": False})
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(board_spawn._await_dispatch_ready(8466, "s-1"))
        assert excinfo.value.status_code == 504
        assert "died" in excinfo.value.detail

    def test_missing_output_chars_fails_fast_with_502(
        self, _fast_probe, monkeypatch
    ):
        """No ``output_chars`` is unknown, not ready: refuse at once rather
        than type after a guessed grace."""
        calls = self._host(monkeypatch, {"alive": True})
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(board_spawn._await_dispatch_ready(8466, "s-1"))
        assert excinfo.value.status_code == 502
        assert "output_chars" in excinfo.value.detail
        assert len(calls) == 1

    def test_no_output_within_the_cap_504s(self, _fast_probe, monkeypatch):
        self._host(monkeypatch, {"alive": True, "output_chars": 0})
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(board_spawn._await_dispatch_ready(8466, "s-1"))
        assert excinfo.value.status_code == 504
        assert "no output" in excinfo.value.detail


class TestFreeTextDispatchIsGone:
    """#1382: the Add / Build / Yolo endpoint was removed, not just hidden."""

    def test_post_board_dispatch_is_404(self, webapp_client, monkeypatch):
        from src import middleware

        # Loopback, so the answer is the router's own and not the tailnet gate's.
        monkeypatch.setattr(
            middleware.trust, "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )
        client, app, _ = webapp_client
        # The 404 alone proves little (the old route also 404'd an unknown
        # repo), so pin that no route answers the path at all.
        assert "/api/board/dispatch" not in {
            getattr(r, "path", None) for r in app.routes
        }
        resp = client.post(
            "/api/board/dispatch",
            json={"repo": "x", "goal": "g", "mode": "add"},
        )
        assert resp.status_code == 404


class TestBoardModelResolution:
    """``_agent_and_flags`` validates a Board per-launch model against ``copilot_models``."""

    def _cfg(self, webapp_client):
        _, app, _ = webapp_client
        return app.state.webapp_config

    def test_a_listed_model_launches_copilot_with_it(self, webapp_client, monkeypatch):
        monkeypatch.setattr(board_spawn.agents, "is_installed", lambda _: True)
        cfg = self._cfg(webapp_client)
        agent, flags = board_spawn._agent_and_flags(cfg, f"copilot:{cfg.copilot_models[0]}")
        assert agent == "copilot"
        assert f"--model {cfg.copilot_models[0]}" in flags

    def test_default_lets_copilot_pick(self, webapp_client, monkeypatch):
        monkeypatch.setattr(board_spawn.agents, "is_installed", lambda _: True)
        agent, flags = board_spawn._agent_and_flags(self._cfg(webapp_client), "default", "low")
        assert agent == "copilot"
        assert "--model" not in flags and "--effort" not in flags

    @pytest.mark.parametrize("model", ["gpt5.6", "codex:gpt-5.6-sol", "grok:grok-4.7", "opus"])
    def test_anything_else_is_an_unknown_model(self, webapp_client, monkeypatch, model):
        monkeypatch.setattr(board_spawn.agents, "is_installed", lambda _: True)
        with pytest.raises(HTTPException) as excinfo:
            board_spawn._agent_and_flags(self._cfg(webapp_client), model)
        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == f"unknown model: {model}"

    def test_copilot_not_installed_is_a_distinct_400(self, webapp_client, monkeypatch):
        monkeypatch.setattr(board_spawn.agents, "is_installed", lambda _: False)
        cfg = self._cfg(webapp_client)
        with pytest.raises(HTTPException) as excinfo:
            board_spawn._agent_and_flags(cfg, cfg.copilot_models[0])
        assert excinfo.value.detail == "GitHub Copilot CLI is not installed"
