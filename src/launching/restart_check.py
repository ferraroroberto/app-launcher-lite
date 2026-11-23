r"""Proof that a ``tray.bat --restart`` replaced the webapp process.

A matching ``git_sha`` does not prove a restart: a restart that never ran
leaves the old process serving, and when that process already loaded ``HEAD``
the sha matches anyway. The webapp's ``pid`` changes only with a new process,
so the check records it before the restart (``snapshot``) and, afterwards
(``verify``), passes only when the answer carries a different ``pid`` and a
``git_sha`` that matches ``HEAD``. Anything that cannot be established
(no baseline, no pid in the answer, nothing answering) is its own failure,
never a pass.

``restart`` is the one command to run, identical from Git Bash, PowerShell and cmd::

    .venv\Scripts\python.exe -m src.launching.restart_check restart

It takes the snapshot, runs this checkout's ``tray.bat --restart`` by absolute path, then verifies,
and exits 0 only on a new ``pid`` serving ``HEAD``. A bare ``tray.bat`` name is not
reachable from any shell on a machine that sets ``NoDefaultCurrentDirectoryInExePath``, and
``cmd.exe /c "tray.bat --restart"`` from Git Bash never enters the script yet exits 0: the
check is what turns either of those into a failure. The three steps also run on their own::

    python -m src.launching.restart_check snapshot
    .\tray.bat --restart        # PowerShell; ./tray.bat in Git Bash
    python -m src.launching.restart_check verify

``session-host`` is the wait ``scripts/restart-session-host.ps1`` runs after the
restart: it polls the webapp's ``/api/version`` ``session_host`` block and ends in one of three
distinct states (live, answering but not provably on the merged code, not answering).

Every step appends a line to ``webapp/restart.log``.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import requests

from src import runtime_files
from src.build_info import resolve_git_sha
from src.clients._loopback_http import pooled_request
from src.subprocess_flags import NO_WINDOW

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WEBAPP_DIR = _REPO_ROOT / "webapp"
RESTART_LOG = _WEBAPP_DIR / "restart.log"
BASELINE_FILE = _WEBAPP_DIR / "restart-baseline.json"
TRAY_BAT = _REPO_ROOT / "tray.bat"
DEFAULT_VERIFY_TIMEOUT_S = 60.0
# tray_lifecycle.ps1 stops the old tray, waits up to 5 s per held port, sleeps 2 s, then polls the
# new build for 30 s; 180 s is that chain with margin, and a tray.bat past it is hung, not slow.
TRAY_RESTART_TIMEOUT_S = 180.0
# A cold session-host answers /healthz about 2.3 s after its spawn (measured on this box); the
# webapp then reports it only from its /api/version snapshot, which can be up to 15 s old
# (3 x VERSION_TICK_S), and the tray restart before the poll adds its own start-up. 45 s covers
# that chain with margin without hiding a host that never comes up.
SESSION_HOST_WAIT_S = 45.0
# `freshness` values that mean the host runs the merged session-host code.
_LIVE_FRESHNESS = ("fresh", "stale-irrelevant")
_PROBE_TIMEOUT_S = 3.0
_POLL_INTERVAL_S = 1.0


def fetch_version(port: int) -> Optional[Dict[str, Any]]:
    """``GET /api/version`` on the loopback webapp, or ``None`` when nothing answers."""
    for scheme in ("https", "http"):
        try:
            resp = pooled_request(
                "GET", f"{scheme}://127.0.0.1:{port}/api/version",
                timeout=_PROBE_TIMEOUT_S, verify=False,
            )
            if resp.status_code == 200:
                body = resp.json()
                return body if isinstance(body, dict) else None
        except (requests.RequestException, ValueError) as exc:
            _log.debug("version probe %s failed: %s", scheme, exc)
    return None


def decide(
    before: Dict[str, Any], after: Optional[Dict[str, Any]], head_sha: str
) -> Tuple[bool, str]:
    """``(replaced, reason)``: whether ``after`` is a new process serving ``head_sha``.

    ``before`` is the snapshot (``{"pid": int | None}``): a ``None`` pid means
    no webapp was answering, so any answering process is new.
    """
    if after is None:
        return False, "no webapp answering on the port"
    served = str(after.get("git_sha") or "")
    if not served or served == "unknown":
        return False, f"the webapp reports no usable git_sha ({served or 'empty'})"
    if not (head_sha.startswith(served) or served.startswith(head_sha)):
        return False, f"webapp serves git_sha {served}, expected HEAD {head_sha}"
    pid = after.get("pid")
    if not isinstance(pid, int):
        return False, "the webapp reports no pid, so a restart cannot be confirmed"
    before_pid = before.get("pid")
    if before_pid is not None and pid == before_pid:
        return False, f"same process still serving (pid {pid}): the restart did not replace it"
    return True, f"new process pid {pid} (was {before_pid}) serves git_sha {served}"


def _log_line(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} restart-check {message}"
    RESTART_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RESTART_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line)


def snapshot(port: int) -> int:
    body = fetch_version(port)
    pid = body.get("pid") if body else None
    runtime_files.make_dirs(BASELINE_FILE.parent)
    runtime_files.save_text(
        BASELINE_FILE,
        json.dumps({"port": port, "pid": pid if isinstance(pid, int) else None}),
    )
    state = f"pid {pid}" if isinstance(pid, int) else "nothing answering (or no pid reported)"
    _log_line(f"snapshot :{port} {state}")
    return 0


def verify(port: int, timeout_s: float = DEFAULT_VERIFY_TIMEOUT_S) -> int:
    try:
        before = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        if not isinstance(before, dict) or before.get("port") != port:
            raise ValueError("baseline is for another port")
    except (OSError, ValueError):
        _log_line("verify NOT CONFIRMED: no baseline for this port, run `snapshot` before the restart")
        return 2
    head = resolve_git_sha()
    deadline = time.monotonic() + timeout_s
    reason = "no attempt made"
    while True:
        ok, reason = decide(before, fetch_version(port), head)
        if ok:
            _log_line(f"verify OK :{port} {reason}")
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL_S)
    _log_line(f"verify FAILED :{port} {reason}")
    return 1


def _std_handle(stream: Any) -> Any:
    """The caller's own std handle for the child, or DEVNULL when it has none.

    The child runs with NO_WINDOW, so a handle left to default would be a hidden console and
    tray.bat's output would never reach the caller.
    """
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL


def restart(
    port: int,
    timeout_s: float = DEFAULT_VERIFY_TIMEOUT_S,
    *,
    run: Callable[..., "subprocess.CompletedProcess[Any]"] = subprocess.run,
) -> int:
    """Snapshot, run this checkout's ``tray.bat --restart`` by absolute path, then verify.

    ``0`` only when ``verify`` sees a new process on ``HEAD``. The bat is started with its
    full path, never the bare name, so no shell has to find it on its search path or
    rewrite its arguments; a bat that exits non-zero or hangs fails here with its own
    message instead of leaving ``verify`` to time out on the old process.
    """
    if not TRAY_BAT.is_file():
        _log_line(f"restart NOT CONFIRMED: {TRAY_BAT} does not exist")
        return 2
    snapshot(port)
    _log_line(f"restart running {TRAY_BAT} --restart")
    sys.stdout.flush()
    try:
        done = run(
            [str(TRAY_BAT), "--restart"],
            cwd=str(TRAY_BAT.parent),
            stdin=subprocess.DEVNULL,
            stdout=_std_handle(sys.stdout),
            stderr=_std_handle(sys.stderr),
            timeout=TRAY_RESTART_TIMEOUT_S,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log_line(f"restart FAILED: tray.bat did not complete ({exc})")
        return 1
    if done.returncode != 0:
        _log_line(f"restart FAILED: tray.bat exited {done.returncode}, so the old process was not replaced")
        return 1
    return verify(port, timeout_s)


def classify_session_host(body: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """``(state, detail)`` for one ``/api/version`` answer.

    ``state`` is ``live`` (fresh, or stale only in files the host never loads),
    ``unconfirmed`` (the host answers but ``freshness`` is not a pass: it may run older
    session-host code, or the verdict could not be resolved), ``host-silent`` (the webapp
    answers and reports the session-host unreachable) or ``webapp-silent`` (no webapp
    answer at all). Only ``live`` is a success.
    """
    if body is None:
        return "webapp-silent", "no webapp answering"
    host = body.get("session_host")
    if not isinstance(host, dict) or host.get("reachable") is not True:
        return "host-silent", "the webapp answers but reports the session-host unreachable"
    detail = (
        f"git_sha={host.get('git_sha')} started_at={host.get('started_at')} "
        f"stale={host.get('stale')} stale_relevant={host.get('stale_relevant')} "
        f"freshness={host.get('freshness')}"
    )
    if host.get("freshness") in _LIVE_FRESHNESS:
        return "live", detail
    return "unconfirmed", detail


def wait_session_host(
    port: int,
    timeout_s: float = SESSION_HOST_WAIT_S,
    *,
    fetch: Callable[[int], Optional[Dict[str, Any]]] = fetch_version,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll until the session-host is live; ``0`` when it is, ``1`` otherwise.

    A success is reported the moment it is seen, however long the cold start took. A
    failure names what the last answer showed and how long was waited, so "answering on
    an older build" and "never answered" are not the same message.
    """
    started = clock()
    deadline = started + timeout_s
    while True:
        state, detail = classify_session_host(fetch(port))
        elapsed = clock() - started
        if state == "live":
            _log_line(f"session-host live :{port} after {elapsed:.1f}s {detail}")
            return 0
        if clock() >= deadline:
            break
        sleep(_POLL_INTERVAL_S)
    waited = f"after waiting {clock() - started:.0f}s"
    if state == "unconfirmed":
        _log_line(
            f"session-host NOT CONFIRMED :{port} {waited}: it answers but is not provably running "
            f"the merged session-host code ({detail}); check it by hand"
        )
    elif state == "host-silent":
        _log_line(f"session-host NOT ANSWERING :{port} {waited}: {detail}; check it by hand")
    else:
        _log_line(f"session-host NOT CONFIRMED :{port} {waited}: {detail}, so the host was not read")
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.launching.restart_check", description=__doc__)
    parser.add_argument("action", choices=("snapshot", "verify", "restart", "session-host"))
    parser.add_argument("--port", type=int, default=None, help="webapp port (default: the configured one)")
    parser.add_argument("--timeout", type=float, default=None)
    args = parser.parse_args(argv)
    port = args.port
    if port is None:
        from src.app_config import resolve_webapp_port

        port = resolve_webapp_port()
    if args.action == "snapshot":
        return snapshot(port)
    if args.action == "session-host":
        return wait_session_host(port, SESSION_HOST_WAIT_S if args.timeout is None else args.timeout)
    timeout = DEFAULT_VERIFY_TIMEOUT_S if args.timeout is None else args.timeout
    if args.action == "restart":
        return restart(port, timeout)
    return verify(port, timeout)


if __name__ == "__main__":
    sys.exit(main())
