"""Loopback socket exhaustion in the e2e harness: name a drained port pool, retry a full buffer.

Every closed TCP connection to a local server leaves an (ephemeral port, server
port) pair in TIME_WAIT for minutes, and Windows answers a ``connect()`` that
lands on such a pair with ``net::ERR_ADDRESS_IN_USE`` rather than trying another
port. So a run that makes many connections, or a second lane gating on the same
host, fails a page load that has nothing wrong with it. Playwright's own
Node-side client fails the same way as ``connect ENOBUFS``.

``pytest_runtest_makereport`` relabels a failed report whose text carries either
marker, and ``write_summary`` prints the affected tests in a section of their own
read off the reports themselves, so it works on an xdist controller whose workers
made them.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Generator
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

logger = logging.getLogger(__name__)

PORT_POOL_MARKERS = ("ERR_ADDRESS_IN_USE", "connect ENOBUFS")
PORT_POOL_HEADLINE = (
    "PORT POOL EXHAUSTED - the host ran out of loopback ephemeral ports "
    f"({' / '.join(PORT_POOL_MARKERS)}), not a page failure. Another gate or a recent "
    "run is holding ports in TIME_WAIT; wait a few minutes and re-run rather than "
    "debugging the page."
)


def is_port_pool_failure(text: str) -> bool:
    return any(marker in text for marker in PORT_POOL_MARKERS)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Lead a failed report that names a drained port pool with that cause."""
    report = yield
    if report.failed and is_port_pool_failure(report.longreprtext):
        report.longrepr = f"{PORT_POOL_HEADLINE}\n\n{report.longreprtext}"
    return report


def write_summary(terminalreporter: Any) -> None:
    """Print the section that lists every test this run failed on the port pool."""
    # `in`, not `startswith`: an xdist controller's report text opens with the worker's line.
    reports = [*terminalreporter.stats.get("failed", []), *terminalreporter.stats.get("error", [])]
    hit = [r.nodeid for r in reports if PORT_POOL_HEADLINE in r.longreprtext]
    if not hit:
        return
    terminalreporter.write_sep("=", "PORT POOL EXHAUSTED", red=True, bold=True)
    terminalreporter.write_line(PORT_POOL_HEADLINE)
    for nodeid in dict.fromkeys(hit):
        terminalreporter.write_line(f"  {nodeid}")


def _tcp_state_counts() -> str:
    """One-line TCP state histogram (``TIME_WAIT=412 ESTABLISHED=38 …``)."""
    try:
        out = subprocess.run(
            ["netstat", "-an", "-p", "tcp"], capture_output=True, text=True,
            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
            encoding="oem", errors="replace",
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"netstat unavailable ({exc.__class__.__name__})"
    counts: dict = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[0] == "TCP":
            counts[fields[3]] = counts.get(fields[3], 0) + 1
    return " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no sockets"


def retry_goto_on_no_buffer_space(page: Page) -> None:
    """Retry a navigation once when Windows runs out of socket buffers.

    ``net::ERR_NO_BUFFER_SPACE`` (WSAENOBUFS) is the hosted runner's TCP stack
    failing a loopback connect, not anything the page did: it reddened
    ``Page.goto`` in two CI runs. Only that exact error is retried, once,
    after a short pause; the socket-state histogram is logged so a recurrence
    is diagnosable from the run log. Every other navigation error, and a
    second ``ERR_NO_BUFFER_SPACE``, still fails the test.
    """
    original = page.goto

    def goto(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except PlaywrightError as exc:
            if "ERR_NO_BUFFER_SPACE" not in str(exc):
                raise
            logger.warning("⚠️  goto hit ERR_NO_BUFFER_SPACE (%s); retrying once",
                           _tcp_state_counts())
            time.sleep(2)
            return original(*args, **kwargs)

    page.goto = goto
