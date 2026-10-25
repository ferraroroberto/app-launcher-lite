"""Unit tests for ``list_app_listeners``' loopback-ephemeral filter (d564114).

Every pywinpty ``PtyProcess.spawn()`` opens a 127.0.0.1 listener on a random
port in the IANA dynamic range (>= 49152) that lingers after the PTY ends, so
the Running-apps panel used to list the session-host PID under bogus ports.
Driven with a fake ``psutil`` so it's deterministic and needs no real sockets.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List

import pytest

import src.diagnostics as diagnostics_mod


class _NoSuchProcess(Exception):
    pass


class _AccessDenied(Exception):
    pass


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def name(self) -> str:
        return "python.exe"

    def exe(self) -> str:
        return r"C:\python\python.exe"

    def cwd(self) -> str:
        return ""

    def cmdline(self) -> List[str]:
        return ["python.exe"]

    def ppid(self) -> int:
        return 1


class _FakePsutil:
    NoSuchProcess = _NoSuchProcess
    AccessDenied = _AccessDenied
    CONN_LISTEN = "LISTEN"

    def __init__(self, listeners: List[tuple]) -> None:
        # (pid, ip, port) — each its own python process, all LISTEN.
        self._conns = [
            SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(ip=ip, port=port), pid=pid)
            for pid, ip, port in listeners
        ]

    def net_connections(self, kind: str = "inet"):
        return list(self._conns)

    def Process(self, pid: int) -> _FakeProc:
        return _FakeProc(pid)


def _listed_ports(monkeypatch, listeners: List[tuple]) -> List[int]:
    monkeypatch.setattr(diagnostics_mod, "psutil", _FakePsutil(listeners))
    return [owner.port for owner in diagnostics_mod.list_app_listeners()]


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1"])
def test_loopback_ephemeral_listener_hidden(monkeypatch, ip: str) -> None:
    ports = _listed_ports(monkeypatch, [(100, "127.0.0.1", 8501), (200, ip, 49152), (300, ip, 61234)])
    assert ports == [8501]


def test_loopback_port_below_dynamic_range_kept(monkeypatch) -> None:
    assert _listed_ports(monkeypatch, [(100, "127.0.0.1", 49151)]) == [49151]


def test_non_loopback_high_port_kept(monkeypatch) -> None:
    # Only loopback is filtered: an app bound on all interfaces is real.
    assert _listed_ports(monkeypatch, [(100, "0.0.0.0", 50000)]) == [50000]
