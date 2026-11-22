"""Settings > Performance over the API: the table in GET /api/config, saves, refusals, live pools."""

from __future__ import annotations

import pytest

from app.webapp import hot_poll
from app.webapp.routers import claude_code
from src.webapp_settings import performance as perf


@pytest.fixture(autouse=True)
def _restore_pools():
    yield
    hot_poll.resize_pools(perf.default("pool_hot_poll"), perf.default("pool_session_reads"))
    claude_code.resize_git_status_pool(perf.default("pool_git_status"))


def test_get_config_carries_the_table_the_sheet_draws_from(webapp_client):
    client, _, _ = webapp_client
    table = client.get("/api/config").json()["performance"]
    assert table["preset"] == "standard"
    assert table["values"] == perf.STANDARD
    assert set(table["presets"]) == {"standard", "light"}
    board = next(k for k in table["knobs"] if k["key"] == "poll_board_s")
    assert (board["default"], board["min"], board["max"]) == (5, 2, 120)


def test_a_preset_saves_every_limit_and_reads_back_as_that_preset(webapp_client):
    client, app, _ = webapp_client
    assert client.post("/api/config", json=perf.LIGHT).status_code == 200
    assert client.get("/api/config").json()["performance"]["preset"] == "light"
    assert app.state.webapp_config.poll_board_s == perf.LIGHT["poll_board_s"]
    assert client.post("/api/config", json=perf.STANDARD).status_code == 200
    assert client.get("/api/config").json()["performance"]["preset"] == "standard"


def test_one_edited_limit_makes_the_set_custom(webapp_client):
    client, _, _ = webapp_client
    assert client.post("/api/config", json={"poll_board_s": 9}).status_code == 200
    assert client.get("/api/config").json()["performance"]["preset"] == "custom"


def test_a_save_resizes_the_pools_without_a_restart(webapp_client):
    client, _, _ = webapp_client
    resp = client.post("/api/config", json={**perf.LIGHT})
    assert resp.status_code == 200
    assert hot_poll._POOL.limit == perf.LIGHT["pool_hot_poll"]
    assert hot_poll.SESSION_READS_POOL.limit == perf.LIGHT["pool_session_reads"]
    assert claude_code._GIT_STATUS_POOL.limit == perf.LIGHT["pool_git_status"]


@pytest.mark.parametrize(
    "patch, words",
    [
        ({"poll_board_s": 1}, "between 2 and 120"),
        ({"poll_board_s": 4.5}, "whole number"),
        ({"poll_board_s": True}, "whole number"),
        ({"poll_pause_hidden": "false"}, "on or off"),
        ({"pool_git_status": 0}, "between 1 and 32"),
        ({"git_status_ttl_s": 600}, "between 2 and 60"),
    ],
)
def test_an_invalid_value_is_refused_with_the_reason_and_changes_nothing(webapp_client, patch, words):
    client, app, _ = webapp_client
    before = app.state.webapp_config.poll_board_s
    resp = client.post("/api/config", json=patch)
    assert resp.status_code == 400
    assert words in resp.json()["detail"]
    assert app.state.webapp_config.poll_board_s == before


def test_one_bad_limit_in_a_preset_write_saves_none_of_them(webapp_client):
    client, app, _ = webapp_client
    resp = client.post("/api/config", json={**perf.LIGHT, "pool_hot_poll": 2})
    assert resp.status_code == 400
    assert app.state.webapp_config.poll_board_s == perf.STANDARD["poll_board_s"]


def test_request_stats_budgets_follow_the_scale(webapp_client):
    client, _, _ = webapp_client
    before = client.get("/api/request-stats").json()["budgets"]["board"]
    client.post("/api/config", json={"request_budget_scale": 2.0})
    after = client.get("/api/request-stats").json()["budgets"]["board"]
    assert after["hard_s"] == before["hard_s"] * 2


def test_the_health_line_answers_with_unknown_named_not_zero(webapp_client):
    client, _, _ = webapp_client
    body = client.get("/api/performance/health").json()
    assert {"cpu_percent", "memory_free_gb", "inflight", "max_inflight", "agent_processes", "lanes",
            "worker_cap", "unknown"} <= set(body)
    for key in ("cpu", "memory", "agents", "lanes"):
        assert (key in body["unknown"]) == (
            body[{"cpu": "cpu_percent", "memory": "memory_free_gb", "agents": "agent_processes", "lanes": "lanes"}[key]]
            is None
        )
