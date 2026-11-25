"""The Apps tab's port listeners sampler: reads never scan.

``GET /api/ports/probe`` used to run a fresh psutil scan per request. On a slow
box that scan takes seconds, so polls queued behind each other. The sampler runs
the scan on its own one-thread pool at a fixed spacing while someone reads, and
every read returns the cached snapshot with its age. A scan that overruns its
budget, a sampler that has not scanned yet and a scan that failed all read
``unknown`` with a reason, never an empty list. Time is a fake clock and the
slow scanner is held by an event, so nothing here sleeps on a guess.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.webapp.routers import misc
from src import diagnostics
from src.diagnostics import ListenerScanUnavailable, PortOwner
from src.listeners_sampler import ListenersSampler


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Inline:
    """An executor that runs the scan where it is submitted, so a test can step it."""

    def submit(self, fn, *args):
        fn(*args)


def _sampler(scan, clock, executor=None, **kw) -> ListenersSampler:
    return ListenersSampler(scan, executor=executor or _Inline(), clock=clock, **kw)


def test_read_before_any_scan_is_unknown_warming() -> None:
    clock = _Clock()
    reading = _sampler(lambda: ["row"], clock).read()
    assert reading.value is None
    assert reading.reason == "warming"


def test_scan_runs_only_while_someone_reads() -> None:
    clock = _Clock()
    calls: list = []
    s = _sampler(lambda: calls.append(1) or ["row"], clock)
    assert s.maybe_scan() is False  # nobody has read yet
    s.read()
    assert s.maybe_scan() is True
    clock.advance(s.demand_s + 1)  # the reader left
    clock.advance(s.tick_s)
    assert s.maybe_scan() is False
    assert len(calls) == 1


def test_read_returns_the_cached_snapshot_with_its_age() -> None:
    clock = _Clock()
    s = _sampler(lambda: ["row"], clock)
    s.read()
    s.maybe_scan()
    clock.advance(3)
    reading = s.read()
    assert reading.value == ["row"]
    assert reading.reason is None
    assert reading.age_seconds == 3.0


def test_scans_keep_a_fixed_spacing() -> None:
    clock = _Clock()
    calls: list = []
    s = _sampler(lambda: calls.append(1) or [], clock)
    s.read()
    assert s.maybe_scan() is True
    clock.advance(s.tick_s - 1)
    s.read()
    assert s.maybe_scan() is False
    clock.advance(1)
    s.read()
    assert s.maybe_scan() is True
    assert len(calls) == 2


def test_reads_return_the_cache_without_waiting_for_a_slow_scan() -> None:
    """A read never waits on the scan: the scanner is held until the test frees it."""
    clock = _Clock()
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(10)
        return ["fresh"]

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        first = {"n": 0}

        def scan():
            first["n"] += 1
            return ["old"] if first["n"] == 1 else slow()

        s = _sampler(scan, clock, executor=pool)
        s.read()
        s.maybe_scan()
        pool.submit(lambda: None).result(5)  # the first (instant) scan is done
        clock.advance(s.tick_s)
        s.read()
        assert s.maybe_scan() is True
        assert started.wait(5)  # the second scan is running and held
        reading = s.read()  # returns at once, mid-scan
        assert reading.value == ["old"]
        assert reading.reason is None
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_an_overrunning_scan_reads_unknown_with_its_reason() -> None:
    clock = _Clock()
    started, release = threading.Event(), threading.Event()

    def scan():
        started.set()
        release.wait(10)
        return ["late"]

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        s = _sampler(scan, clock, executor=pool)
        s.read()
        s.maybe_scan()
        assert started.wait(5)
        clock.advance(s.budget_s - 1)
        assert s.read().reason == "warming"  # inside the budget: no snapshot yet, not an overrun
        clock.advance(2)  # now over the budget
        reading = s.read()
        assert reading.value is None
        assert reading.reason == "overrun"
        assert "budget" in reading.detail
    finally:
        release.set()
        pool.shutdown(wait=True)
    # The late scan lands afterwards and the reading recovers.
    after = s.read()
    assert after.reason in (None, "stale")


def test_two_concurrent_reads_trigger_at_most_one_scan() -> None:
    clock = _Clock()
    release = threading.Event()
    calls: list = []

    def scan():
        calls.append(1)
        release.wait(10)
        return []

    pool = ThreadPoolExecutor(max_workers=4)
    try:
        s = _sampler(scan, clock, executor=pool)
        s.read()
        s.read()
        results = [s.maybe_scan(), s.maybe_scan(), s.maybe_scan()]
        assert results == [True, False, False]
        clock.advance(s.tick_s * 3)
        assert s.maybe_scan() is False  # still the same scan, never a second
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert len(calls) == 1


def test_a_failed_scan_reads_unknown_error_and_the_next_one_recovers() -> None:
    clock = _Clock()
    outcomes = [RuntimeError("boom"), ["row"]]

    def scan():
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    s = _sampler(scan, clock)
    s.read()
    s.maybe_scan()
    bad = s.read()
    assert (bad.value, bad.reason) == (None, "error")
    assert "boom" in bad.detail
    clock.advance(s.tick_s)
    s.read()
    s.maybe_scan()
    assert s.read().value == ["row"]


def test_a_scan_that_could_not_measure_reads_unavailable_not_empty() -> None:
    clock = _Clock()

    def scan():
        raise ListenerScanUnavailable("socket table not readable")

    s = _sampler(scan, clock)
    s.read()
    s.maybe_scan()
    reading = s.read()
    assert reading.value is None
    assert reading.reason == "unavailable"
    assert "socket table" in reading.detail


def test_a_snapshot_older_than_max_age_reads_stale() -> None:
    clock = _Clock()
    s = _sampler(lambda: ["row"], clock)
    s.read()
    s.maybe_scan()
    clock.advance(s.max_age_s + 1)
    reading = s.read()
    assert reading.value is None
    assert reading.reason == "stale"


# ---------------------------------------------------------------- the route


def _owners():
    return [PortOwner(pid=11, port=8501, name="pythonw.exe"), PortOwner(pid=12, port=8443, name="python.exe")]


def test_route_returns_unknown_before_the_first_scan(webapp_client) -> None:
    client, app, _ = webapp_client
    body = client.get("/api/ports/probe").json()
    assert body["listeners"] is None
    assert body["reason"] == "warming"


def test_route_serves_the_snapshot_and_never_scans(webapp_client, monkeypatch) -> None:
    client, app, _ = webapp_client
    scans: list = []
    monkeypatch.setattr(misc, "list_app_listeners", lambda: scans.append(1) or _owners())
    monkeypatch.setattr(misc, "_LISTENER_SCHEMES", {})
    app.state.app_config = replace(app.state.app_config, tailnet_host="")
    sampler = misc.listeners_sampler(app)
    sampler.executor = _Inline()
    client.get("/api/ports/probe")  # the demand
    assert sampler.maybe_scan() is True
    scans.clear()
    for _ in range(5):
        body = client.get("/api/ports/probe").json()
    assert scans == []
    assert [r["port"] for r in body["listeners"]] == [8501, 8443]
    assert body["reason"] is None
    assert isinstance(body["age_seconds"], float)


# ------------------------------------------------------ the unmeasured scan


def test_list_app_listeners_raises_when_the_socket_table_is_unreadable(monkeypatch) -> None:
    def denied(kind):
        raise PermissionError("denied")

    fake = SimpleNamespace(
        CONN_LISTEN="LISTEN", AccessDenied=PermissionError, NoSuchProcess=LookupError, net_connections=denied
    )
    monkeypatch.setattr(diagnostics, "psutil", fake)
    with pytest.raises(ListenerScanUnavailable):
        diagnostics.list_app_listeners()


def test_list_app_listeners_raises_without_psutil(monkeypatch) -> None:
    monkeypatch.setattr(diagnostics, "psutil", None)
    with pytest.raises(ListenerScanUnavailable):
        diagnostics.list_app_listeners()


# ------------------------------------------------------------------ kill


def _loopback(app):
    """The kill route is gated to the PC and the tailnet; TestClient's default host is neither."""
    from fastapi.testclient import TestClient

    return TestClient(app, client=("127.0.0.1", 50000))


def test_kill_uses_one_socket_table_read_and_spares_this_process(webapp_client, monkeypatch) -> None:
    _, app, _ = webapp_client
    import os

    monkeypatch.setattr(misc, "listener_pids", lambda port: [os.getpid(), 4242])
    killed: list = []
    monkeypatch.setattr(misc, "kill_pids", lambda pids: (killed.extend(pids) or list(pids), []))
    body = _loopback(app).post("/api/ports/8501/kill").json()
    assert killed == [4242]
    assert body["killed"] == [4242]


def test_kill_with_an_unreadable_socket_table_is_not_nothing_listening(webapp_client, monkeypatch) -> None:
    _, app, _ = webapp_client
    monkeypatch.setattr(misc, "listener_pids", lambda port: None)
    resp = _loopback(app).post("/api/ports/8501/kill")
    assert resp.status_code == 503
    assert "could not" in resp.json()["detail"].lower()
