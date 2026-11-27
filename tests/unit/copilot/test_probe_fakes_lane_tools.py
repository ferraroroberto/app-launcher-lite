"""The lane tools' pid, listener and port probes, fed each platform's answer (probe fake ratchet).

The tools run on Windows. The macOS and Linux cases pin that another platform's answer reads
as it should or as unknown, never as dead or as "nothing listening". All output is invented.
"""

from __future__ import annotations

import ctypes
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Optional

import pytest

_REPO = Path(__file__).resolve().parents[3]
PLATFORMS = ("win32", "darwin", "linux")
PORT = 4242


def _load(rel: str, name: str) -> ModuleType:
    """Load a lane tool by its repo path, leaving ``sys.path`` as it was."""
    saved = list(sys.path)
    sys.path.insert(0, str(_REPO / "copilot" / "skills" / "_lib"))
    try:
        spec = importlib.util.spec_from_file_location(name, _REPO / rel)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = saved


active_issue = _load("copilot/skills/_lib/active_issue.py", "lite_probe_active_issue")
service_probe = _load("copilot/skills/_lib/worktree/service_probe.py", "lite_probe_service_probe")
worktree_config = _load("copilot/skills/_lib/worktree/worktree_config.py", "lite_probe_worktree_config")


# ---------------------------------------------------------------- pid probe


@pytest.mark.parametrize("error, exit_code, expected", [
    (87, None, False), (5, None, True), (1450, None, None), (None, 259, True), (None, 0, False),
])
def test_pid_state_on_windows(monkeypatch, error: Optional[int], exit_code: Optional[int], expected) -> None:
    def exit_code_of(_handle, out) -> int:
        out.value = exit_code
        return 1

    kernel32 = SimpleNamespace(
        # Plain functions: the probe sets restype and argtypes on them, as on ctypes ones.
        OpenProcess=lambda *_a: 0 if error else 77,
        GetExitCodeProcess=exit_code_of,
        CloseHandle=lambda _h: 1,
    )
    monkeypatch.setattr(active_issue, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(active_issue, "ctypes", SimpleNamespace(
        WinDLL=lambda *_a, **_k: kernel32, get_last_error=lambda: error,
        byref=lambda obj: obj, POINTER=ctypes.POINTER,
    ))
    assert active_issue.pid_state(4321) is expected


@pytest.mark.parametrize("platform", ("darwin", "linux"))
@pytest.mark.parametrize("raised, expected", [
    (None, True), (ProcessLookupError(), False), (PermissionError(), True), (OSError(22, "invalid"), None),
])
def test_pid_state_on_posix(monkeypatch, platform: str, raised: Optional[OSError], expected) -> None:
    def kill(_pid: int, _signal: int) -> None:
        if raised is not None:
            raise raised

    monkeypatch.setattr(active_issue, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(active_issue, "os", SimpleNamespace(kill=kill))
    assert active_issue.pid_state(4321) is expected


# ---------------------------------------------------------------- listener table

NETSTAT = {
    "win32": (
        "\nActive Connections\n\n"
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    0.0.0.0:4242           0.0.0.0:0              LISTENING       101\n"
        "  TCP    [::]:4243              [::]:0                 ABHÖREN         102\n"
        "  TCP    127.0.0.1:50000        127.0.0.1:4244         ESTABLISHED     103\n"
    ),
    "darwin": (
        "Active Internet connections (including servers)\n"
        "Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)\n"
        "tcp4       0      0  127.0.0.1.4242         *.*                    LISTEN\n"
    ),
    "linux": (
        "Active Internet connections (servers and established)\n"
        "Proto Recv-Q Send-Q Local Address           Foreign Address         State\n"
        "tcp        0      0 0.0.0.0:4242            0.0.0.0:*               LISTEN\n"
    ),
}


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_listener_table_by_platform(monkeypatch, platform: str) -> None:
    answer = SimpleNamespace(returncode=0, stdout=NETSTAT[platform])
    monkeypatch.setattr(service_probe.subprocess, "run", lambda *_a, **_k: answer)
    if platform == "win32":
        assert service_probe.listening_ports() == {4242, 4243}
        return
    # A table in another shape is unreadable, never "nothing is serving".
    assert service_probe.listening_ports() is None
    assert service_probe.probe_service(PORT, None)[0] == service_probe.SERVICE_UNKNOWN


# ---------------------------------------------------------------- port probe


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_worktree_port_is_free_only_when_refused_by_platform(monkeypatch, platform: str) -> None:
    refused = {"win32": 10061, "darwin": 61, "linux": 111}[platform]
    for errno, free in ((0, False), (refused, True)):
        class Sock:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            def settimeout(self, _seconds):
                return None

            def connect_ex(self, _address, errno=errno):
                return errno

        monkeypatch.setattr(worktree_config, "socket", SimpleNamespace(
            AF_INET=2, SOCK_STREAM=1, socket=lambda *_a: Sock(),
        ))
        assert worktree_config._port_is_free(PORT) is free
