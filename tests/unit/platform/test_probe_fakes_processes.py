"""Process-table probes, fed each platform's process rows (probe fake ratchet).

The platforms differ in what a row says: Windows spells paths with any case and keeps a
child's parent pid after the parent exits; macOS moves a framework Python's image path and
refuses other users' details; Linux and macOS re-parent an orphan to pid 1. A refused
attribute arrives as ``None``. All rows are invented.
"""

from __future__ import annotations

import ntpath
import posixpath
from collections import namedtuple
from types import SimpleNamespace
from typing import Any, Dict, List

import psutil
import pytest

from app.cli.commands import run_job_supervision
from src import app_runtime, health_snapshot, performance_health, system_load
from src.launching import bat_running

PLATFORMS = ("win32", "darwin", "linux")
AGENT_NAME = {"win32": "Copilot.exe", "darwin": "copilot", "linux": "copilot"}
CpuTimes = namedtuple("CpuTimes", "user system")


def _table(monkeypatch, *rows: Dict[str, Any]) -> None:
    """Answer ``psutil.process_iter`` with processes carrying ``rows`` as their ``info``."""
    monkeypatch.setattr(
        psutil, "process_iter",
        lambda *_a, **_k: iter([SimpleNamespace(pid=row["pid"], info=row) for row in rows]),
    )


# ---------------------------------------------------------------- bat_running

# (path module, bat folder, bat body, the running process's cmdline, cwd, exe)
BAT_CASES = {
    "win32": (
        ntpath, r"C:\apps\mini", '@echo off\r\nstart "" pythonw "%~dp0worker.py"\r\n',
        [r"C:\Python\pythonw.exe", r'"C:\Apps\Mini\Worker.py"'], r"C:\Windows", r"C:\Python\pythonw.exe",
    ),
    "darwin": (
        posixpath, "/Users/dev/mini", "python worker.py\n",
        ["/Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python",
         "worker.py"],
        "/Users/dev/mini",
        "/Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python",
    ),
    "linux": (
        posixpath, "/home/dev/mini", "python3 worker.py\n",
        ["python3", "./worker.py"], "/home/dev/mini", "/usr/bin/python3.12",
    ),
}


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_running_target_is_found_by_platform(monkeypatch, platform: str) -> None:
    paths, folder, body, cmdline, cwd, exe = BAT_CASES[platform]
    monkeypatch.setattr(bat_running, "os", SimpleNamespace(path=paths, sep=paths.sep, getpid=lambda: 1))
    bat = SimpleNamespace(read_text=lambda **_kw: body, parent=folder)
    refused = {"pid": 7, "cmdline": None, "cwd": None, "exe": None}
    _table(monkeypatch, refused, {"pid": 8, "cmdline": cmdline, "cwd": cwd, "exe": exe})
    assert bat_running.find_running_target(bat) == 8
    _table(monkeypatch, refused)
    assert bat_running.find_running_target(bat) is None


# ---------------------------------------------------------------- app_runtime


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_child_is_found_by_its_parent_pid_by_platform(monkeypatch, platform: str) -> None:
    refused = {"pid": 7, "ppid": 100, "name": None, "create_time": None}
    # Once the parent has exited, only Windows still records it on the child.
    orphan_ppid = 100 if platform == "win32" else 1
    _table(monkeypatch, refused, {"pid": 8, "ppid": orphan_ppid, "name": AGENT_NAME[platform], "create_time": 50.0})
    found = app_runtime.find_child_process(100, AGENT_NAME[platform].lower(), created_after=50.0)
    assert found == (8 if platform == "win32" else None)

    def denied(_pid: int) -> Any:
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "Process", denied)
    assert app_runtime._create_time(8) is None


# ---------------------------------------------------------------- system_load


@pytest.mark.parametrize("platform", PLATFORMS)
def test_process_times_by_platform(monkeypatch, platform: str) -> None:
    monkeypatch.setattr(system_load, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(system_load, "_windows_process_times", lambda: {(4, 1): ("one-call", 1.0)})
    _table(
        monkeypatch,
        {"pid": 0, "name": "kernel_task", "cpu_times": CpuTimes(9.0, 9.0), "create_time": 0.0},
        {"pid": 7, "name": "other-user", "cpu_times": None, "create_time": None},
        {"pid": 8, "name": "worker", "cpu_times": CpuTimes(1.5, 0.5), "create_time": 2.0},
    )
    # 64-bit Windows reads the table in one call; the others go through psutil, row by row.
    expected = {(4, 1): ("one-call", 1.0)} if platform == "win32" else {(8, 2_000_000): ("worker", 2.0)}
    assert system_load._process_times() == expected

    def denied(_pid: int) -> Any:
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "Process", denied)
    assert system_load._cmdline(7) == [] and system_load._cwd(7) == ""


# ------------------------------------------------- health_snapshot, performance


class _Proc:
    """A process whose memory read is refused when ``rss`` is ``None``."""

    def __init__(self, pid: int, name: str, rss: Any) -> None:
        self.pid, self.info, self._rss = pid, {"name": name}, rss

    def cpu_percent(self, _interval: Any) -> float:
        return 50.0 if self.pid else 700.0

    def memory_info(self) -> SimpleNamespace:
        if self._rss is None:
            raise psutil.AccessDenied(self.pid)
        return SimpleNamespace(rss=self._rss)


def _health_rows(platform: str) -> List[_Proc]:
    idle = {"win32": "System Idle Process", "darwin": "kernel_task", "linux": "swapper"}[platform]
    return [_Proc(0, idle, 0), _Proc(7, "other-user", None), _Proc(8, AGENT_NAME[platform], 3 * 1024 * 1024)]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_health_and_agent_counts_by_platform(monkeypatch, platform: str) -> None:
    rows = _health_rows(platform)
    monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_k: iter(rows))
    sampled = health_snapshot.sample_processes(sample_s=0)
    assert [row[1] for row in sampled] == [0, 8]
    report = "\n".join(health_snapshot._process_sections(sampled))
    assert "pid 0" not in report and "agent processes: 1" in report

    monkeypatch.setattr(performance_health, "_agent_cache", None)
    assert performance_health.agent_processes(now=0.0) == 1

    def refused(*_a: Any, **_k: Any) -> Any:
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "process_iter", refused)
    assert performance_health.agent_processes(now=100.0) is None  # unknown, never zero


# ---------------------------------------------------------- run_job_supervision


@pytest.mark.parametrize("platform, refusal", [
    ("win32", psutil.AccessDenied(9)),
    ("darwin", psutil.ZombieProcess(9)),
    ("linux", psutil.NoSuchProcess(9)),
], ids=PLATFORMS)
def test_the_resource_sampler_skips_an_unreadable_child_by_platform(
    monkeypatch, platform: str, refusal: BaseException
) -> None:
    def gone() -> Any:
        raise refusal

    child = SimpleNamespace(pid=9, memory_info=gone, cpu_times=gone)
    parent = SimpleNamespace(
        pid=8, children=lambda recursive: [child],
        memory_info=lambda: SimpleNamespace(rss=2048), cpu_times=lambda: CpuTimes(1.0, 0.25),
    )
    monkeypatch.setattr(psutil, "Process", lambda _pid: parent)
    sampler = run_job_supervision.ResourceSampler(8)
    monkeypatch.setattr(sampler._stop, "wait", lambda _timeout: True)  # one tick
    sampler._loop()
    assert (sampler.peak_rss_bytes, sampler.cpu_seconds) == (2048, 1.25)
