"""``diagnostics.listener_pids``: who holds a LISTEN socket, this process included."""

from __future__ import annotations

import os
import socket

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


def test_an_unreadable_socket_table_is_none_not_empty(monkeypatch):
    def denied(**_kw):
        raise PermissionError("denied")

    monkeypatch.setattr(diagnostics.psutil, "net_connections", denied)
    assert diagnostics.listener_pids(8465) is None
