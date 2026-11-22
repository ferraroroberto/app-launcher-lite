"""The git-status scan's footprint: two git processes at once, one scan per TTL window.

A fleet-wide scan on a slow PC took 10-15 s of wall time and about 70 CPU
seconds beside the session polls. These tests pin the bounds with fake repos
and injected delays, never a load burst: how many git calls overlap, how many
scans a window of calls causes, which repos go first, and how a cached answer
is labelled.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

import httpx
import pytest

from app.webapp.routers import claude_code
from src.scanner import GitStatus, ProjectDir
from src.webapp_settings import performance as perf

_NOT_GIT = GitStatus(is_git=False, branch=None, default_branch=None, dirty=False)


def _repos(names: List[str]) -> List[ProjectDir]:
    return [ProjectDir(id=n, name=n, project_dir=Path(f"C:/fake/{n}")) for n in names]


def _get(app, path: str = "/api/claude-code/git-status", **params):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return (await client.get(path, params=params)).json()

    return asyncio.run(run())


_REAL_POOL = claude_code._GIT_STATUS_POOL


@pytest.fixture(autouse=True)
def _default_pool():
    # Resized on the real pool by name: a test may swap the module's pool out.
    _REAL_POOL.resize(perf.default("pool_git_status"))
    yield
    _REAL_POOL.resize(perf.default("pool_git_status"))


def test_the_shipped_git_pool_is_two_threads():
    assert perf.default("pool_git_status") == 2
    assert claude_code._GIT_STATUS_POOL.limit == 2


def test_a_scan_never_has_more_than_two_git_processes_alive(webapp_client, monkeypatch):
    """Twelve repos, each held until a partner is inside too: the pair is proven
    concurrent (the pool really runs two), and a third can never join it."""
    _, app, _ = webapp_client
    monkeypatch.setattr(claude_code, "scan_project_dirs", lambda *a, **k: _repos([f"r{i}" for i in range(12)]))
    pair = threading.Barrier(2, timeout=10)
    lock = threading.Lock()
    live = {"now": 0, "peak": 0}

    def status(_path):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        try:
            pair.wait()
        finally:
            with lock:
                live["now"] -= 1
        return _NOT_GIT

    monkeypatch.setattr(claude_code, "git_status", status)
    body = _get(app)
    assert len(body["projects"]) == 12
    assert live["peak"] == 2, f"{live['peak']} git calls overlapped, wanted exactly 2"


def test_calls_inside_one_ttl_window_cause_one_scan(webapp_client, monkeypatch):
    _, app, _ = webapp_client
    monkeypatch.setattr(claude_code, "scan_project_dirs", lambda *a, **k: _repos(["alpha", "beta"]))
    scanned: List[str] = []
    monkeypatch.setattr(claude_code, "git_status", lambda path: scanned.append(path.name) or _NOT_GIT)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return [(await client.get("/api/claude-code/git-status")).json() for _ in range(6)]

    answers = asyncio.run(run())
    assert sorted(scanned) == ["alpha", "beta"], f"{len(scanned)} git calls for six requests, wanted one scan of two repos"
    assert [a["freshness"] for a in answers] == ["fresh"] * 6


def test_an_answer_past_the_ttl_is_labelled_stale_and_a_fresh_scan_is_fresh(webapp_client, monkeypatch):
    _, app, _ = webapp_client
    monkeypatch.setattr(claude_code, "scan_project_dirs", lambda *a, **k: _repos(["alpha"]))
    monkeypatch.setattr(claude_code, "git_status", lambda _path: _NOT_GIT)
    app.state.webapp_config.git_status_ttl_s = 0

    first = _get(app)
    stale = _get(app)
    assert first["freshness"] == "fresh"
    assert stale["freshness"] == "stale"


def test_lane_first_puts_repos_with_a_live_lane_ahead_keeping_each_group_in_order():
    projects = _repos(["alpha", "beta", "beta-wt-481", "gamma", "delta"])
    ordered = claude_code.lane_first(projects, frozenset({"delta", "beta"}))
    assert [p.name for p in ordered] == ["beta", "beta-wt-481", "delta", "alpha", "gamma"]
    assert claude_code.lane_first(projects, frozenset()) == projects


def test_a_scan_reads_lane_repos_before_the_rest_of_the_fleet(webapp_client, monkeypatch, tmp_path):
    _, app, _ = webapp_client
    monkeypatch.setattr(
        claude_code, "scan_project_dirs", lambda *a, **k: _repos(["alpha", "beta", "gamma", "delta"])
    )
    state_file = tmp_path / "sessions-state.json"
    (tmp_path / "active-issues.json").write_text(
        json.dumps({"delta#7": {"repo": "delta", "number": 7, "branch": "fix/7-x", "started_at": _now_iso()}}),
        encoding="utf-8",
    )
    app.state.webapp_config.sessions_state_file = str(state_file)
    # A one-thread FIFO executor in the pool's place: the order git is asked in is
    # then exactly the order the scan submitted the repos in.
    monkeypatch.setattr(claude_code, "_GIT_STATUS_POOL", ThreadPoolExecutor(max_workers=1))
    seen: List[str] = []
    monkeypatch.setattr(claude_code, "git_status", lambda path: seen.append(path.name) or _NOT_GIT)

    _get(app)
    assert seen[0] == "delta", f"scan order was {seen}"
    assert sorted(seen) == ["alpha", "beta", "delta", "gamma"]


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def test_each_repo_status_git_call_carries_a_ten_second_cap(monkeypatch):
    from src import scanner

    seen = {}

    def fake_run_git(_dir, _args, timeout):
        seen["timeout"] = timeout
        return None

    monkeypatch.setattr(scanner, "run_git", fake_run_git)
    scanner._run_git(Path("C:/fake"), ["status"])
    assert seen["timeout"] == 10
