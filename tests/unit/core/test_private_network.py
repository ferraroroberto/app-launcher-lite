"""Private-IP discovery behind the PC load sheet's IP row.

Every socket call is mocked, so the result never depends on the host the suite
runs on: route found, route missing (interface fallback), nothing found, and
the cache that a failed re-read must leave alone.
"""

from __future__ import annotations

import pytest

from src import private_network as pn


@pytest.fixture(autouse=True)
def _clean_cache():
    pn.reset()
    yield
    pn.reset()


def _route(monkeypatch, address=None, exc=None):
    def fake():
        if exc is not None:
            raise exc
        return address

    monkeypatch.setattr(pn, "_route_address", fake)


def _interfaces(monkeypatch, addresses=None, exc=None):
    def fake():
        if exc is not None:
            raise exc
        return list(addresses or [])

    monkeypatch.setattr(pn, "_interface_addresses", fake)


class TestDiscover:
    def test_the_active_route_wins(self, monkeypatch):
        _route(monkeypatch, "192.168.1.20")
        _interfaces(monkeypatch, ["10.9.9.9"])
        assert pn.discover() == "192.168.1.20"

    def test_a_missing_route_falls_back_to_the_interfaces(self, monkeypatch):
        _route(monkeypatch, exc=OSError("network is unreachable"))
        _interfaces(monkeypatch, ["127.0.0.1", "169.254.3.4", "10.0.0.7"])
        assert pn.discover() == "10.0.0.7"

    def test_a_non_private_route_address_falls_back_too(self, monkeypatch):
        _route(monkeypatch, "100.101.102.103")
        _interfaces(monkeypatch, ["172.16.4.5"])
        assert pn.discover() == "172.16.4.5"

    def test_nothing_found_is_unknown(self, monkeypatch):
        _route(monkeypatch, exc=OSError("no route"))
        _interfaces(monkeypatch, ["127.0.0.1", "8.8.8.8"])
        assert pn.discover() is None

    def test_both_sources_failing_is_unknown_not_an_error(self, monkeypatch):
        _route(monkeypatch, exc=OSError("no route"))
        _interfaces(monkeypatch, exc=OSError("no hostname"))
        assert pn.discover() is None

    @pytest.mark.parametrize(
        "address, usable",
        [
            ("10.0.0.1", True),
            ("172.31.255.1", True),
            ("192.168.0.9", True),
            ("172.32.0.1", False),
            ("169.254.1.1", False),
            ("127.0.0.1", False),
            ("0.0.0.0", False),
            ("100.64.0.1", False),
            ("8.8.8.8", False),
            ("fe80::1", False),
            ("not an address", False),
        ],
    )
    def test_only_private_ipv4_counts(self, address, usable):
        assert pn._usable(address) is usable


class TestCache:
    def test_nothing_cached_before_a_discovery(self):
        assert pn.current() is None

    def test_a_found_address_is_cached(self, monkeypatch):
        monkeypatch.setattr(pn, "discover", lambda: "192.168.1.20")
        assert pn.refresh() is True
        assert pn.current() == "192.168.1.20"

    def test_a_changed_address_replaces_the_cache(self, monkeypatch):
        monkeypatch.setattr(pn, "discover", lambda: "192.168.1.20")
        pn.refresh()
        monkeypatch.setattr(pn, "discover", lambda: "10.0.0.7")
        assert pn.refresh() is True
        assert pn.current() == "10.0.0.7"

    def test_a_read_that_finds_nothing_keeps_the_cache(self, monkeypatch):
        monkeypatch.setattr(pn, "discover", lambda: "192.168.1.20")
        pn.refresh()
        monkeypatch.setattr(pn, "discover", lambda: None)
        assert pn.refresh() is False
        assert pn.current() == "192.168.1.20"

    def test_a_discovery_that_raises_keeps_the_cache(self, monkeypatch):
        monkeypatch.setattr(pn, "discover", lambda: "192.168.1.20")
        pn.refresh()

        def boom():
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(pn, "discover", boom)
        assert pn.refresh() is False
        assert pn.current() == "192.168.1.20"
