"""The hub's machine health beside the header's CPU reading.

The hub judges and measures; Lite only reads its cached answer. What matters here:

* nothing Lite cannot read ever comes out as ``ok`` (a missing, unreachable, hung,
  stale or unreadable hub is ``unknown`` with a reason);
* a request never waits on the hub: the route reads the sampler's cache, so a hub
  that hangs delays no request, and the sampler's own fetch is bounded;
* the fetch is one read per 30 s, success or failure, single flight, and only
  while the page asks.

Time is a fake clock and the hung hub is a fake held by an event, so nothing here
sleeps on a guess.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.webapp.routers import misc
from src.clients import hub_health as hh


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Inline:
    """Runs the fetch where it is submitted, so a test can step it."""

    def submit(self, fn, *args):
        fn(*args)


def _serve(monkeypatch, body=None, raises=None):
    calls = []

    def fake(method, url, **kwargs):
        calls.append((url, kwargs.get("timeout")))
        if raises is not None:
            raise raises
        return body

    monkeypatch.setattr(hh._loopback_http, "request", fake)
    return calls


@pytest.mark.parametrize("verdict", ["ok", "watch", "act"])
def test_a_recognised_verdict_passes_through_with_its_reason_and_fix(monkeypatch, verdict):
    _serve(monkeypatch, {"verdict": verdict, "reason": "Camera Hub  has 4M handles", "fix": "Restart Camera Hub"})
    got = hh.fetch("http://hub.test")
    assert got == {"verdict": verdict, "why": None, "reason": "Camera Hub has 4M handles", "fix": "Restart Camera Hub"}


def test_it_reads_the_hubs_machines_health_route_with_a_short_timeout(monkeypatch):
    calls = _serve(monkeypatch, {"verdict": "ok"})
    hh.fetch("http://hub.test/")
    assert [url for url, _ in calls] == ["http://hub.test/admin/api/machines/health"]
    assert 0 < calls[0][1] <= 3.0


@pytest.mark.parametrize(
    "body, why",
    [
        ({"verdict": "unknown", "reason": "no sample yet"}, "hub"),
        ({"verdict": "fine"}, "unreadable"),
        ({"reason": "no verdict key"}, "unreadable"),
        (["ok"], "unreadable"),
        (None, "unreadable"),
    ],
)
def test_an_answer_that_is_not_a_verdict_is_unknown_never_ok(monkeypatch, body, why):
    _serve(monkeypatch, body)
    got = hh.fetch("http://hub.test")
    assert got["verdict"] == "unknown"
    assert got["why"] == why


def test_a_hub_that_does_not_answer_is_unknown_with_the_reason(monkeypatch):
    _serve(monkeypatch, raises=hh.HubHealthError("local-llm-hub unreachable", status=503))
    got = hh.fetch("http://hub.test")
    assert (got["verdict"], got["why"]) == ("unknown", "unreachable")
    assert "unreachable" in got["reason"]


def test_no_hub_url_is_unknown_and_never_asks(monkeypatch):
    calls = _serve(monkeypatch, {"verdict": "ok"})
    got = hh.fetch("  ")
    assert (got["verdict"], got["why"]) == ("unknown", "disabled")
    assert calls == []


def test_a_hub_that_accepts_and_never_answers_is_cut_off_at_the_timeout(monkeypatch):
    """A real socket that takes the connection and stays silent: the fetch ends at its timeout."""
    monkeypatch.setattr(hh, "_TIMEOUT", 0.3)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))  # port-host-exempt: an ephemeral loopback port, the test's own listener
    srv.listen(1)
    try:
        started = time.monotonic()
        got = hh.fetch("http://127.0.0.1:%d" % srv.getsockname()[1])  # port-host-exempt: the listener above
        took = time.monotonic() - started
    finally:
        srv.close()
    assert (got["verdict"], got["why"]) == ("unknown", "unreachable")
    assert took < 2.0


# ------------------------------------------------------- the background sampler


def _sampler(monkeypatch, body=None, raises=None, url="http://hub.test", **kw):
    calls = _serve(monkeypatch, body, raises)
    clock = _Clock()
    sampler = hh.build_sampler(lambda: url, executor=_Inline(), clock=clock, **kw)
    return sampler, clock, calls


def test_a_read_never_fetches_and_the_tick_does(monkeypatch):
    sampler, clock, calls = _sampler(monkeypatch, {"verdict": "watch", "reason": "r"})
    first = sampler.read()
    assert (first.value, first.reason) == (None, "warming")
    assert calls == []
    assert sampler.maybe_scan() is True
    for _ in range(5):
        got = sampler.read()
    assert got.value["verdict"] == "watch"
    assert len(calls) == 1


def test_the_fetch_waits_for_demand(monkeypatch):
    sampler, clock, calls = _sampler(monkeypatch, {"verdict": "ok"})
    assert sampler.maybe_scan() is False  # nobody has asked
    assert calls == []


def test_a_good_answer_and_a_failed_one_are_both_cached_for_thirty_seconds(monkeypatch):
    sampler, clock, calls = _sampler(monkeypatch, {"verdict": "act", "reason": "r"})
    sampler.read()
    sampler.maybe_scan()
    clock.advance(hh.CACHE_SECONDS - 1)
    sampler.read()
    assert sampler.maybe_scan() is False
    clock.advance(2)
    assert sampler.maybe_scan() is True
    assert len(calls) == 2

    sampler, clock, calls = _sampler(monkeypatch, raises=hh.HubHealthError("down", status=503))
    sampler.read()
    sampler.maybe_scan()
    clock.advance(hh.CACHE_SECONDS - 1)
    sampler.read()
    assert sampler.maybe_scan() is False
    clock.advance(2)
    assert sampler.maybe_scan() is True
    assert len(calls) == 2


def test_a_hub_that_hangs_reads_overrun_and_blocks_no_read(monkeypatch):
    """The fetch is held open by an event: a read still answers at once, unknown with the reason."""
    release = threading.Event()
    started = threading.Event()

    def hung(method, url, **kwargs):
        started.set()
        release.wait(10)
        return {"verdict": "ok"}

    monkeypatch.setattr(hh._loopback_http, "request", hung)
    clock = _Clock()
    pool = ThreadPoolExecutor(max_workers=1)
    sampler = hh.build_sampler(lambda: "http://hub.test", executor=pool, clock=clock)
    try:
        sampler.read()
        assert sampler.maybe_scan() is True
        assert started.wait(5)
        assert sampler.maybe_scan() is False  # single flight
        clock.advance(hh.FETCH_BUDGET_SECONDS + 1)
        t0 = time.monotonic()
        got = sampler.read()
        assert time.monotonic() - t0 < 0.5
        assert (got.value, got.reason) == (None, "overrun")
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_going_unknown_and_coming_back_each_leave_one_breadcrumb(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="launcher.slowreq")
    sampler, clock, _ = _sampler(monkeypatch, raises=hh.HubHealthError("down", status=503))
    sampler.read()
    sampler.maybe_scan()
    clock.advance(hh.CACHE_SECONDS + 1)
    sampler.read()
    sampler.maybe_scan()  # still down: no second line
    _serve(monkeypatch, {"verdict": "ok"})
    clock.advance(hh.CACHE_SECONDS + 1)
    sampler.read()
    sampler.maybe_scan()
    lines = [r.getMessage() for r in caplog.records if r.name == "launcher.slowreq"]
    assert len(lines) == 2
    assert "hub health unknown (unreachable)" in lines[0]
    assert "readable again: ok" in lines[1]


# ------------------------------------------------------------------ the route


def test_the_route_serves_the_cached_verdict_with_its_age(webapp_client, monkeypatch):
    client, app, _ = webapp_client
    app.state.webapp_config.llm_hub_url = "http://hub.test"
    calls = _serve(monkeypatch, {"verdict": "watch", "reason": "r", "fix": "f"})
    sampler = misc.hub_health_sampler(app)
    sampler.executor = _Inline()
    assert client.get("/api/system/health").json()["why"] == "warming"  # the demand; nothing fetched yet
    assert calls == []
    sampler.maybe_scan()
    body = client.get("/api/system/health").json()
    assert body["verdict"] == "watch"
    assert isinstance(body["age_seconds"], float)
    assert [url for url, _ in calls] == ["http://hub.test/admin/api/machines/health"]


def test_the_route_says_unknown_when_the_hub_is_unset(webapp_client, monkeypatch):
    client, app, _ = webapp_client
    app.state.webapp_config.llm_hub_url = ""
    calls = _serve(monkeypatch, {"verdict": "ok"})
    body = client.get("/api/system/health").json()
    assert (body["verdict"], body["why"]) == ("unknown", "disabled")
    assert calls == []


def test_the_route_answers_while_the_hub_hangs(webapp_client, monkeypatch):
    client, app, _ = webapp_client
    app.state.webapp_config.llm_hub_url = "http://hub.test"
    release = threading.Event()
    monkeypatch.setattr(hh._loopback_http, "request", lambda *a, **k: release.wait(10) and {"verdict": "ok"})
    sampler = misc.hub_health_sampler(app)
    pool = ThreadPoolExecutor(max_workers=1)
    sampler.executor = pool
    try:
        client.get("/api/system/health")
        assert sampler.maybe_scan() is True
        t0 = time.monotonic()
        for _ in range(3):
            body = client.get("/api/system/health").json()
        assert time.monotonic() - t0 < 2.0
        assert (body["verdict"], body["why"]) == ("unknown", "warming")
    finally:
        release.set()
        pool.shutdown(wait=True)


@pytest.mark.parametrize("reason", ["warming", "overrun", "stale", "error"])
def test_every_sampler_state_without_a_verdict_is_unknown_with_that_reason(reason):
    got = hh.unavailable(reason, "detail text")
    assert (got["verdict"], got["why"], got["reason"]) == ("unknown", reason, "detail text")


_PORTS_BODY = {
    "verdict": "act", "reason": "ports", "fix": None, "host_id": "example-host",
    "findings": [
        {"level": "act", "metric": "tcp_percent", "value": 84.5},
        {"level": "watch", "metric": "tcp_time_wait", "value": 5000},
    ],
    "system": {"commit_percent": 68.5},
    "offenders": {"memory_mb": [{"name": "llama-server.exe", "pid": 7, "value": 24998.8}] * 4},
    "ports": {
        "stale": False,
        "tcp": {"percent": 84.5}, "udp": {"percent": 0.6},
        "top_holders": [{"name": f"p{i}.exe", "pid": i, "value": 10 - i, "tcp": 10 - i, "udp": 0} for i in range(7)],
        "last_event": {"ts": 1794766557.8, "event_id": 4231, "protocol": "tcp", "reason": "TCP ran out of ephemeral ports"},
    },
}


def test_the_machine_block_carries_the_worst_ports_percent_commit_holders_and_event():
    machine = hh.parse(_PORTS_BODY)["machine"]
    assert machine["host"] == "example-host"
    assert machine["ports_percent"] == 84.5  # tcp, the worse of tcp and udp
    assert machine["commit_percent"] == 68.5
    assert [h["name"] for h in machine["holders"]] == ["p0.exe", "p1.exe", "p2.exe", "p3.exe", "p4.exe"]
    assert len(machine["memory_holders"]) == 3
    assert machine["last_event"]["event_id"] == 4231


def test_a_stale_port_block_leaves_ports_unmeasured_never_zero():
    body = dict(_PORTS_BODY, ports=dict(_PORTS_BODY["ports"], stale=True))
    assert hh.parse(body)["machine"]["ports_percent"] is None


def test_a_payload_with_no_machine_data_has_no_machine_block():
    assert "machine" not in hh.parse({"verdict": "ok", "reason": "fine"})


_DISK_BODY = {
    "verdict": "act", "reason": "disk", "fix": None, "host_id": "example-host",
    "findings": [{"level": "act", "metric": "disk_load", "value": 98.0}],
    "disks": [
        {"disk": "PhysicalDrive2", "drives": ["C:"], "state": "alert",
         "busy_percent": 98.0, "queue": 7.5, "read_ms": 120.4, "write_ms": 310.0},
        {"disk": "PhysicalDrive0", "drives": ["D:", "F:"], "state": "watch",
         "busy_percent": 82.0, "queue": 2.0, "read_ms": None, "write_ms": 40.0},
        {"disk": "PhysicalDrive1", "drives": ["E:"], "state": "ok",
         "busy_percent": 1.0, "queue": 0.0, "read_ms": 0.1, "write_ms": 0.3},
    ],
}


def test_the_disks_are_passed_through_with_their_states():
    machine = hh.parse(_DISK_BODY)["machine"]
    assert [(d["drives"], d["state"], d["busy_percent"]) for d in machine["disks"]] == [
        (["C:"], "alert", 98.0), (["D:", "F:"], "watch", 82.0), (["E:"], "ok", 1.0)]
    assert machine["disks"][0]["queue"] == 7.5 and machine["disks"][0]["write_ms"] == 310.0
    assert machine["disks"][1]["read_ms"] is None  # no latency sample is not 0 ms


def test_a_hub_that_sends_no_disks_reads_as_before():
    body = {k: v for k, v in _DISK_BODY.items() if k != "disks"} | {"system": {"commit_percent": 40.0}}
    machine = hh.parse(body)["machine"]
    assert machine["disks"] is None
    assert hh.parse(_PORTS_BODY)["machine"]["disks"] is None


@pytest.mark.parametrize("disks", ["busy", {"disk": "x"}, [], [None, 3, {}], [{"disk": "  ", "drives": []}]])
def test_a_malformed_disks_block_is_not_shown_never_healthy(disks):
    body = {"verdict": "ok", "reason": "fine", "system": {"commit_percent": 40.0}, "disks": disks}
    assert hh.parse(body)["machine"]["disks"] is None


def test_a_disk_with_a_missing_or_unrecognised_state_reads_unknown():
    disks = [{"disk": "d0", "drives": ["C:"], "busy_percent": 1.0},
             {"disk": "d1", "drives": ["D:"], "state": "fine", "busy_percent": True}]
    got = hh.parse({"verdict": "ok", "disks": disks})["machine"]["disks"]
    assert [d["state"] for d in got] == ["unknown", "unknown"]
    assert got[1]["busy_percent"] is None  # a bool is not a number


def test_the_disk_block_is_bounded():
    disks = [{"disk": f"d{i}", "drives": [f"{i}:"] * 9, "state": "ok", "busy_percent": 1.0} for i in range(20)]
    got = hh.parse({"verdict": "ok", "disks": disks})["machine"]["disks"]
    assert len(got) == 8 and all(len(d["drives"]) == 4 for d in got)
