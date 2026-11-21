"""``private_ip`` in /api/status, ``POST /api/status/refresh``, and a startup
that survives discovery raising. Discovery is mocked, so no test depends on
the host's network."""

from __future__ import annotations

import threading

import pytest

from src import private_network


@pytest.fixture(autouse=True)
def _clean_cache():
    private_network.reset()
    yield
    private_network.reset()


def _discovery_threads():
    return [t for t in threading.enumerate() if t.name == "private-ip-discovery"]


class TestStatusField:
    def test_status_carries_the_cached_address(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        monkeypatch.setattr(private_network, "discover", lambda: "192.168.1.20")
        private_network.refresh()
        assert client.get("/api/status").json()["private_ip"] == "192.168.1.20"

    def test_status_is_null_when_nothing_was_ever_found(self, webapp_client):
        client, _, _ = webapp_client
        for thread in _discovery_threads():
            thread.join(timeout=5)
        private_network.reset()
        assert client.get("/api/status").json()["private_ip"] is None


class TestRefresh:
    def test_refresh_reads_again_and_reports_ok(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        monkeypatch.setattr(private_network, "discover", lambda: "10.0.0.7")
        resp = client.post("/api/status/refresh")
        assert resp.status_code == 200
        assert resp.json() == {"private_ip": "10.0.0.7", "discovery_ok": True}
        assert client.get("/api/status").json()["private_ip"] == "10.0.0.7"

    def test_a_failed_refresh_keeps_the_cached_address(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        monkeypatch.setattr(private_network, "discover", lambda: "10.0.0.7")
        client.post("/api/status/refresh")

        def boom():
            raise OSError("adapter vanished")

        monkeypatch.setattr(private_network, "discover", boom)
        resp = client.post("/api/status/refresh")
        assert resp.status_code == 200
        assert resp.json() == {"private_ip": "10.0.0.7", "discovery_ok": False}

    def test_a_refresh_with_nothing_found_and_no_cache_is_unknown(self, webapp_client, monkeypatch):
        client, _, _ = webapp_client
        for thread in _discovery_threads():
            thread.join(timeout=5)
        private_network.reset()
        monkeypatch.setattr(private_network, "discover", lambda: None)
        resp = client.post("/api/status/refresh")
        assert resp.json() == {"private_ip": None, "discovery_ok": False}


class TestStartup:
    def test_the_app_builds_when_discovery_raises(self, webapp_client, monkeypatch):
        from app.webapp import server as server_mod

        # The fixture's own startup thread may already have cached the host's
        # real address; only a discovery that raises must leave it empty.
        for thread in _discovery_threads():
            thread.join(timeout=5)
        private_network.reset()

        def boom():
            raise RuntimeError("no network stack")

        monkeypatch.setattr(private_network, "discover", boom)
        app = server_mod.create_app()
        assert app is not None
        for thread in _discovery_threads():
            thread.join(timeout=5)
        assert private_network.current() is None
