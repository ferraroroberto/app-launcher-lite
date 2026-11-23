"""``GET /api/copilot-usage`` never waits for the usage log, and nor does anything else.

The log is SQLite another process writes, so a read can stall on a lock or a
slow disk. Here the reader blocks until the test releases it (a log locked
for as long as the test likes) and the route must still answer at once,
start only one read, keep the single-worker loop free, and leave the page
itself (``GET /``) untouched. Drives the real coroutine on one loop, the way
``test_webapp_board_merge_nonblocking.py`` does, plus an ASGI client on that one loop for the
page response (``TestClient`` opens a loop per request, whose teardown would
wait on the blocked thread, which the long-lived server loop never does).
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

from app.webapp.routers import copilot_usage as usage_router
from src import copilot_usage

_FAST_S = 0.25


def _blocked_snapshot(tmp_path: Path):
    release = threading.Event()
    calls: list[int] = []

    def reader(_path: Path, _now) -> copilot_usage.Reading:
        calls.append(1)
        release.wait(10)
        return copilot_usage.Reading("ok", used_credits=30.0, today_credits=1.0)

    db = tmp_path / "session-store.db"
    db.write_bytes(b"")
    def plan_reader(_path: Path, _now) -> copilot_usage.PlanReading:
        return copilot_usage.PlanReading("ok", used_credits=30.0, allowance=300.0)

    snapshot = usage_router.UsageSnapshot(
        db_path=lambda: db, reader=reader, plan_path=lambda: db, plan_reader=plan_reader,
    )
    return snapshot, release, calls


async def test_a_stalled_read_never_holds_the_route_or_the_loop(tmp_path: Path) -> None:
    snapshot, release, calls = _blocked_snapshot(tmp_path)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        webapp_config=SimpleNamespace(copilot_monthly_credits=300), copilot_usage=snapshot,
    )))
    gaps: list[float] = []

    async def ticker() -> None:
        last = time.perf_counter()
        for _ in range(10):
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    try:
        tick = asyncio.ensure_future(ticker())
        for _ in range(3):
            started = time.perf_counter()
            body = await usage_router.get_copilot_usage(request)
            assert time.perf_counter() - started < _FAST_S
            assert body["state"] == "pending" and body["used_pct"] is None
        await tick
        assert max(gaps) < 0.15, f"the loop stalled {max(gaps):.3f}s behind the read"
        assert len(calls) == 1, "a stalled read was stacked instead of single-flight"
    finally:
        release.set()
    await snapshot._task
    body = await usage_router.get_copilot_usage(request)
    assert (body["state"], body["used_pct"]) == ("ok", 10.0)


async def test_the_page_answers_while_the_usage_log_is_locked(webapp_client, tmp_path: Path) -> None:
    _, app, _ = webapp_client
    snapshot, release, calls = _blocked_snapshot(tmp_path)
    app.state.copilot_usage = snapshot
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            for path in ("/api/copilot-usage", "/", "/api/copilot-usage"):
                started = time.perf_counter()
                res = await client.get(path)
                assert res.status_code == 200
                assert time.perf_counter() - started < 2, f"a locked usage log held up {path}"
            assert res.json()["state"] == "pending"
        assert calls == [1]
    finally:
        release.set()
    await snapshot._task


def test_the_allowance_is_a_whole_number_setting(webapp_client) -> None:
    client, _, _ = webapp_client
    assert client.get("/api/config").json()["copilot"]["monthly_credits"] == 300
    assert client.post("/api/config", json={"copilot_monthly_credits": 1500}).status_code == 200
    assert client.get("/api/config").json()["copilot"]["monthly_credits"] == 1500
    for bad in (0, 2.5, "300", True):
        assert client.post("/api/config", json={"copilot_monthly_credits": bad}).status_code == 400
