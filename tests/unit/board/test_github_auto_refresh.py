"""The open Board's GitLab refresh: one server-side, single-flight refresh per TTL.

A visible Board tab's poll carries the view header; the server, not each tab,
decides when the ``glab`` reads run (``src/board/view_refresh.py``). Clocks
are injected; nothing here waits on real time. Synthetic groups only.
"""

from __future__ import annotations

import subprocess

from src.board import view_refresh
from src.clients import gitlab as forge

TTL = view_refresh.REFRESH_TTL_SECONDS


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _HeldSpawn:
    """Keeps the refresh 'running' until the test lets it finish."""

    def __init__(self) -> None:
        self.pending: list = []

    def __call__(self, target) -> None:
        self.pending.append(target)

    def finish(self) -> None:
        self.pending.pop(0)()


def _gate():
    clock, spawn = _Clock(), _HeldSpawn()
    return view_refresh.ViewRefresh(clock=clock, spawn=spawn), clock, spawn


def test_a_never_filled_cache_starts_one_refresh_and_overlapping_views_share_it():
    gate, _clock, spawn = _gate()
    runs: list = []
    assert gate.maybe_start(None, lambda: runs.append(1)) is True
    for _ in range(5):  # five tabs polling while it runs
        assert gate.maybe_start(None, lambda: runs.append(1)) is False
    assert len(spawn.pending) == 1
    spawn.finish()
    assert runs == [1]


def test_one_attempt_per_ttl_even_when_the_refresh_failed():
    gate, clock, spawn = _gate()

    def failing() -> None:
        raise RuntimeError("glab down")

    assert gate.maybe_start(None, failing) is True
    spawn.finish()  # the failure is logged, not raised, and frees the gate
    clock.now += TTL - 1
    assert gate.maybe_start(None, failing) is False, "a failed refresh is not retried every poll"
    clock.now += 1
    assert gate.maybe_start(None, failing) is True


def test_a_cache_younger_than_the_ttl_is_left_alone():
    gate, clock, spawn = _gate()
    assert gate.maybe_start(TTL - 1, lambda: None) is False
    assert gate.maybe_start(TTL + 1, lambda: None) is True
    spawn.finish()
    clock.now += TTL
    assert gate.maybe_start(30.0, lambda: None) is False, "a ↻ tap just filled it"


def test_board_polls_with_the_view_header_start_one_refresh_for_every_tab(webapp_client, monkeypatch):
    client, app, _overrides = webapp_client
    clock, spawn = _Clock(), _HeldSpawn()
    app.state.board_view_refresh = view_refresh.ViewRefresh(clock=clock, spawn=spawn)
    refreshed: list = []
    monkeypatch.setattr(view_refresh, "refresh_all", lambda cfg: refreshed.append(cfg))

    assert client.get("/api/board").status_code == 200
    assert spawn.pending == [], "a poll without the view header (a hidden page) starts nothing"

    for _ in range(3):  # three tabs' polls inside one refresh
        assert client.get("/api/board", headers={view_refresh.VIEW_HEADER: "1"}).status_code == 200
    assert len(spawn.pending) == 1
    spawn.finish()
    assert len(refreshed) == 1

    clock.now += TTL
    client.get("/api/board", headers={view_refresh.VIEW_HEADER: "1"})
    assert len(spawn.pending) == 1, "the never-filled cache is due again a TTL later"


def test_the_refresh_tap_always_runs(webapp_client, monkeypatch):
    client, app, _overrides = webapp_client
    app.state.webapp_config.gitlab_group = "grp"
    calls: list = []

    def _nothing_open(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(forge.subprocess, "run", _nothing_open)
    first = client.post("/api/board/github/refresh").json()
    assert first["fetched_at"] and calls
    seen = len(calls)
    client.post("/api/board/github/refresh")
    assert len(calls) > seen
