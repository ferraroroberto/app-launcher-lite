"""``diagnostics.listener_pids``: who holds a LISTEN socket, this process included."""

from __future__ import annotations

import os
import socket
from types import SimpleNamespace

import psutil
import pytest

from src import diagnostics


def _socket_table_readable() -> bool:
    try:
        psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError, OSError):
        return False
    return True


# macOS lets only root read the system-wide socket table, so a non-root run can
# only ever answer "unknown" there (pinned by the denied-table test below).
needs_socket_table = pytest.mark.skipif(
    not _socket_table_readable(), reason="system-wide socket table needs root on this host"
)


@needs_socket_table
def test_names_this_process_for_a_port_it_listens_on():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert os.getpid() in (diagnostics.listener_pids(port) or [])


@needs_socket_table
def test_a_port_nobody_listens_on_is_an_empty_list():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert diagnostics.listener_pids(port) == []


def _scan_per_process(monkeypatch, *procs):
    """Deny the machine-wide scan and answer from ``procs`` (pid, listening port or an error)."""
    def denied(**_kw):
        raise PermissionError("denied")

    def proc(pid, port):
        def conns(kind="inet"):
            if isinstance(port, Exception):
                raise port
            if not port:
                return []
            return [SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=port))]
        return SimpleNamespace(pid=pid, net_connections=conns)

    monkeypatch.setattr(diagnostics.psutil, "net_connections", denied)
    monkeypatch.setattr(diagnostics.psutil, "process_iter", lambda: iter([proc(*p) for p in procs]))


def test_a_denied_system_scan_falls_back_to_asking_each_process(monkeypatch):
    _scan_per_process(monkeypatch, (11, psutil.AccessDenied(11)), (22, 8465), (33, psutil.NoSuchProcess(33)))
    assert diagnostics.listener_pids(8465) == [22]


def test_no_listener_is_unknown_when_a_process_refused_to_answer(monkeypatch):
    _scan_per_process(monkeypatch, (11, psutil.AccessDenied(11)), (22, None))
    assert diagnostics.listener_pids(8465) is None


def test_no_listener_is_empty_when_every_process_answered(monkeypatch):
    _scan_per_process(monkeypatch, (11, None), (22, 9000))
    assert diagnostics.listener_pids(8465) == []
