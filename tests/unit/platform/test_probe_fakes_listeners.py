"""Listener and port probes, fed each platform's answer shape (probe fake ratchet).

Windows reads the machine-wide socket table. macOS refuses it to a non-root user, so each
process is asked instead. Linux answers it, but leaves another user's socket without a pid.
A connect probe gets a different refusal number on each. All rows and numbers are invented.
"""

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import psutil
import pytest

import app.webapp.manager as manager
from app.tray import registered_trays
from src import diagnostics
from src.launching import process_checkout

PLATFORMS = ("win32", "darwin", "linux")
PORT = 4242
ECONNREFUSED = {"win32": 10061, "darwin": 61, "linux": 111}
APP_PROCESS_NAME = {"win32": "pythonw.exe", "darwin": "Python", "linux": "python3.12"}


def _conn(pid: Optional[int], ip: str, port: int = PORT) -> SimpleNamespace:
    return SimpleNamespace(pid=pid, status=psutil.CONN_LISTEN, laddr=SimpleNamespace(ip=ip, port=port))


def _socket_table(monkeypatch, platform: str, rows: List[SimpleNamespace]) -> None:
    """Make psutil answer for ``rows`` the way ``platform`` does for an unprivileged user."""
    def table(**_kw):
        if platform == "darwin":
            raise psutil.AccessDenied()
        return rows

    def processes():
        return iter([
            SimpleNamespace(pid=row.pid, net_connections=lambda kind, row=row: [row])
            for row in rows if row.pid
        ])

    monkeypatch.setattr(psutil, "net_connections", table)
    monkeypatch.setattr(psutil, "process_iter", processes)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_listener_pids_by_platform(monkeypatch, platform: str) -> None:
    rows = [_conn(11, "127.0.0.1"), _conn(22, "::"), _conn(33, "192.0.2.10")]
    if platform == "linux":
        rows.append(_conn(None, "0.0.0.0"))  # another user's socket: never read as pid 0
    _socket_table(monkeypatch, platform, rows)
    assert diagnostics.listener_pids(PORT) == [11, 22, 33]
    assert process_checkout.probe_listener_pids(PORT) == [11, 22]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_app_listeners_by_platform(monkeypatch, platform: str) -> None:
    _socket_table(monkeypatch, platform, [_conn(11, "0.0.0.0"), _conn(None, "0.0.0.0", PORT + 1)])
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(
        name=lambda: APP_PROCESS_NAME[platform], exe=lambda: "", cwd=lambda: "",
        cmdline=lambda: [], ppid=lambda: 1,
    ))
    if platform == "darwin":
        with pytest.raises(diagnostics.ListenerScanUnavailable):  # unknown, not "no apps"
            diagnostics.list_app_listeners()
        return
    assert [(o.pid, o.port, o.name) for o in diagnostics.list_app_listeners()] == [
        (11, PORT, APP_PROCESS_NAME[platform])
    ]


@pytest.mark.parametrize("platform, refusal", [
    ("win32", psutil.AccessDenied(11)),  # an elevated owner
    ("darwin", psutil.ZombieProcess(11)),
    ("linux", PermissionError(13, "permission denied")),  # /proc/<pid>/cwd of another user
], ids=PLATFORMS)
def test_an_unreadable_working_directory_is_unknown_by_platform(
    monkeypatch, tmp_path: Path, platform: str, refusal: BaseException
) -> None:
    def process(pid: int) -> Any:
        def cwd() -> str:
            if pid == 11:
                raise refusal
            return str(tmp_path)
        return SimpleNamespace(cwd=cwd)

    monkeypatch.setattr(psutil, "Process", process)
    _socket_table(monkeypatch, platform, [_conn(22, "127.0.0.1")])
    assert process_checkout.port_owner(PORT, tmp_path).state == process_checkout.OURS
    _socket_table(monkeypatch, platform, [_conn(11, "127.0.0.1"), _conn(22, "::1")])
    assert process_checkout.port_owner(PORT, tmp_path).state == process_checkout.UNKNOWN


class _FakeSocket:
    """A TCP socket whose connect answers with ``errno`` and records who it dialled."""

    def __init__(self, errno: int, dialled: list) -> None:
        self._errno, self._dialled = errno, dialled

    def __enter__(self) -> "_FakeSocket":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def settimeout(self, _seconds: float) -> None:
        return None

    def connect_ex(self, address: tuple) -> int:
        self._dialled.append(address)
        return self._errno


def _fake_socket_module(errno: int, dialled: list) -> SimpleNamespace:
    return SimpleNamespace(
        AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM,
        socket=lambda *_a: _FakeSocket(errno, dialled),
    )


@pytest.mark.parametrize("platform", PLATFORMS)
def test_connect_probes_by_platform(monkeypatch, platform: str) -> None:
    webapp = manager.WebappManager(manager.WebappManagerConfig(host="0.0.0.0", port=PORT))
    for errno, listening in ((0, True), (ECONNREFUSED[platform], False)):
        dialled: list = []
        fake = _fake_socket_module(errno, dialled)
        monkeypatch.setattr(manager, "socket", fake)
        monkeypatch.setattr(registered_trays, "socket", fake)
        assert webapp.is_port_in_use() is listening
        assert registered_trays.port_listening(PORT) is listening
        # The wildcard bind address is not dialable on every platform; loopback is.
        assert dialled == [("127.0.0.1", PORT)] * 2
