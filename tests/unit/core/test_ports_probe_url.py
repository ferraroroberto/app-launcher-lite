"""The port-listeners probe carries each listener's Open URL (#1129).

Listener rows became action-rows whose tap opens the app, the same as a
running app's Open, so ``GET /api/ports/probe`` now builds that URL server
side: ``<scheme>://<tailnet_host>:<port>/``, with the scheme from a loopback
TLS probe. The probe can take up to a second a port, so the background scan
probes each live (pid, port) once and the route only reads the snapshot.
Without a ``tailnet_host`` there is no URL, and nothing is probed.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from app.webapp.routers import misc
from src.diagnostics import PortOwner


def _owners():
    return [
        PortOwner(pid=11, port=8501, name="pythonw.exe"),
        PortOwner(pid=12, port=8443, name="python.exe"),
    ]


class _Inline:
    def submit(self, fn, *args):
        fn(*args)


def _probe_calls(app, monkeypatch):
    calls = []

    def fake_scheme(port):
        calls.append(port)
        return "https" if port == 8443 else "http"

    monkeypatch.setattr(misc, "detect_local_scheme", fake_scheme)
    monkeypatch.setattr(misc, "list_app_listeners", _owners)
    monkeypatch.setattr(misc, "_LISTENER_SCHEMES", {})
    sampler = misc.listeners_sampler(app)
    sampler.executor = _Inline()
    return calls, sampler


def _scan_now(client, sampler):
    """One background scan, run in place, then the cached read."""
    client.get("/api/ports/probe")  # a read is the demand the scan waits for
    sampler._last_finished = None  # the previous scan's spacing is not under test
    assert sampler.maybe_scan() is True
    return client.get("/api/ports/probe").json()["listeners"]


def test_listeners_carry_their_tailnet_url(webapp_client, monkeypatch):
    client, app, _ = webapp_client
    calls, sampler = _probe_calls(app, monkeypatch)
    app.state.app_config = replace(app.state.app_config, tailnet_host="box.example.ts.net")

    rows = _scan_now(client, sampler)
    urls = {r["port"]: r["url"] for r in rows}
    assert urls == {
        8501: "http://box.example.ts.net:8501/",
        8443: "https://box.example.ts.net:8443/",
    }

    # A second scan reuses each live listener's scheme.
    _scan_now(client, sampler)
    assert sorted(calls) == [8443, 8501], calls


def test_no_tailnet_host_means_no_url_and_no_probe(webapp_client, monkeypatch):
    client, app, _ = webapp_client
    calls, sampler = _probe_calls(app, monkeypatch)
    app.state.app_config = replace(app.state.app_config, tailnet_host="")

    rows = _scan_now(client, sampler)
    assert [r["url"] for r in rows] == [None, None]
    assert calls == []


def test_the_route_answers_from_the_cache_while_a_scan_is_running(webapp_client, monkeypatch):
    """The scan (psutil, ~1 s and several on a slow box) runs on the sampler's own
    thread and the route only reads its snapshot, so a request is never held by it.
    Ordered by events, not a wall-clock budget: the scan holds until released."""
    client, app, _ = webapp_client
    monkeypatch.setattr(misc, "_LISTENER_SCHEMES", {})
    app.state.app_config = replace(app.state.app_config, tailnet_host="")
    scan_started, release = threading.Event(), threading.Event()

    def slow_scan():
        scan_started.set()
        release.wait(5.0)
        return _owners()

    monkeypatch.setattr(misc, "list_app_listeners", slow_scan)
    sampler = misc.listeners_sampler(app)
    pool = sampler.executor = ThreadPoolExecutor(max_workers=1)
    client.get("/api/ports/probe")
    assert sampler.maybe_scan() is True
    try:
        assert scan_started.wait(5.0)
        body = client.get("/api/ports/probe").json()  # returns while the scan is held
        assert body["listeners"] is None and body["reason"] == "warming"
    finally:
        release.set()
        pool.shutdown(wait=True)
