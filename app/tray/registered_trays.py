"""Registered Trays and Mini apps sequential autostart boot sequence.

Split off ``app/tray/tray.py`` (a single-file god-module flagged by
``/codebase-audit``). These are pure functions with no ``TrayApp`` state —
``port_listening`` is also reused by ``tray.py`` for its own session-host
adoption check, since it's a general-purpose loopback-port probe.

Diagnosability: ``launch_all()`` runs at real boot time under
``pythonw.exe`` (no console), spawned by ``tray_lifecycle.ps1``'s
``Start-Process`` — which never redirects stdout/stderr anywhere. So the
``logger.info``/``logger.warning`` calls below are silently discarded in
production; a sister tray failing to launch, or taking an unusually long
time to become reachable, left no trace. Mirrors the breadcrumb log
``src/boot_autostart.py`` already writes for app-launcher's own Startup
wrapper: every attempt appends a timestamped line to a
gitignored ``webapp/registered_trays.log``, so a login-time failure is
diagnosable from that log alone instead of requiring a live re-run.
"""

from __future__ import annotations

import datetime
import logging
import socket
import subprocess
import threading
import time
import tomllib
from pathlib import Path
from typing import Optional

from src import runtime_files
from src.launching.bat_running import find_running_target
from src.registry import Registry, RegistryReadError, load_registry
from src.scanner import KIND_MINI, KIND_TRAY
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_TRAY_READY_TIMEOUT_S = 30.0
_TRAY_READY_POLL_S = 0.5
# Fallback wait when a tray's repo has no readable .fleet.toml port — still
# gives it a head start before the next tray launches, without a real signal.
_TRAY_FALLBACK_DELAY_S = 5.0

# A genuine config/apps.json read failure (as opposed to "no
# autostart trays configured") is retried a small, bounded number of times —
# the suspected trigger is a transient early-boot I/O hiccup (this repo lives
# on a second drive that may still be settling right at login on a full
# restart), likely to clear within a second or two. Kept small on purpose:
# this runs synchronously in TrayApp._start()'s thread on every boot and must
# not meaningfully delay the webapp becoming usable.
_REGISTRY_LOAD_ATTEMPTS = 3
_REGISTRY_LOAD_RETRY_DELAY_S = 1.0

# Retries for an autostart tray the first pass could not confirm ready: a
# login-time port-bind race clears within seconds to a couple of minutes, and
# three spaced attempts cover that without a loop that outlives the boot.
_RETRY_DELAYS_S = (30.0, 60.0, 120.0)


def _log_path() -> Path:
    """The gitignored boot breadcrumb log this repo writes at each autostart.

    Reads ``PROJECT_ROOT`` off the module (not a bound default argument) so
    tests can redirect it via ``monkeypatch.setattr(rt_mod, "PROJECT_ROOT", ...)``.
    """
    return PROJECT_ROOT / "webapp" / "registered_trays.log"


def _log_breadcrumb(msg: str) -> None:
    """Best-effort append of a timestamped line to the breadcrumb log.

    Never raises — a log-write failure must not take down the boot
    sequence it exists to make diagnosable.
    """
    try:
        path = _log_path()
        runtime_files.make_dirs(path.parent)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with runtime_files.open_for_write(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {msg}\n")
    except OSError:
        pass


def port_listening(port: int) -> bool:
    """True if something is listening on 127.0.0.1:<port> (loopback)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _fleet_toml_port(repo_dir: Path) -> Optional[int]:
    """Read the ``port`` field out of ``repo_dir/.fleet.toml``.

    This is the fleet-wide anti-staleness-enforced convention every
    tray-owning repo already keeps current —
    the only per-repo-agnostic readiness signal available here. PID-tree
    port discovery (as the Running Apps panel uses for launcher-spawned
    bats) does NOT work for a Registered Trays entry: every sister
    ``tray.bat`` hands off via ``tray_lifecycle.ps1``'s ``Start-Process``
    (verified in that shared script), which detaches the real long-lived
    tray process from the invoking ``tray.bat``'s own process — that
    invoking process exits within about a second, well before any webapp
    binds its port, so a PID-tree walk rooted there finds nothing.

    Accepts both shapes seen across the fleet: a bare int (``port =
    8447``) or a leading-colon string (``port = ":8465"``). Returns
    ``None`` on a missing file, missing field, or an unrecognised shape —
    the caller falls back to a fixed delay.
    """
    toml_path = repo_dir / ".fleet.toml"
    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    raw = data.get("port")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.lstrip(":"))
        except ValueError:
            return None
    return None


def _spawn_tray_bat_detached(bat_path: Path) -> Optional[int]:
    """Quiet-launch a Registered Trays entry's ``tray.bat``; return the pid of
    the ``cmd`` that starts it, for the breadcrumb.

    Detached the same way as the main tray's own session-host spawn —
    re-parented out of this tray's process subtree via ``cmd /c start`` —
    so a later ``tray.bat --restart`` on THIS machine never touches it (it
    isn't this repo's process to manage beyond starting it). That ``cmd``
    exits within a second, so its pid identifies the attempt, not the tray.
    """
    cmd = ["cmd", "/c", "start", "", "/b", str(bat_path)]
    kw: dict = dict(
        cwd=str(bat_path.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    return subprocess.Popen(cmd, **kw).pid


def _wait_for_tray_ready(repo_dir: Path) -> bool:
    """Best-effort readiness wait for a just-launched sister tray.

    See :func:`_fleet_toml_port` for why this is `.fleet.toml`-port
    based rather than PID-tree port discovery. No/malformed port
    declaration → a fixed delay stand-in, returning ``False`` (not
    confirmed ready, but gave it a head start).
    """
    port = _fleet_toml_port(repo_dir)
    if port is None:
        time.sleep(_TRAY_FALLBACK_DELAY_S)
        return False
    deadline = time.monotonic() + _TRAY_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if port_listening(port):
            return True
        time.sleep(_TRAY_READY_POLL_S)
    return False


def _note(level: int, name: str, msg: str) -> None:
    """One outcome line for an entry, to the logger and the breadcrumb log."""
    logger.log(level, f"Registered Trays: {name}: {msg}")
    _log_breadcrumb(f"{name}: {msg}")


def _spawn_and_wait(name: str, bat_path: Path, port: Optional[int], label: str) -> bool:
    """Spawn ``bat_path`` and wait for its port; True only when it answered.

    The breadcrumb tells the three misses apart: the spawn itself raised, the
    port never answered, or there is no port to confirm readiness with.
    """
    _note(logging.INFO, name, f"{label}{bat_path}")
    try:
        pid = _spawn_tray_bat_detached(bat_path)
    except OSError as exc:
        _note(logging.WARNING, name, f"spawn failed: {type(exc).__name__}: {exc}")
        return False
    _note(logging.INFO, name, f"spawned (launcher pid {pid})" if pid else "spawned")
    if _wait_for_tray_ready(bat_path.parent):
        return True
    if port is None:
        _note(logging.WARNING, name, "readiness unconfirmed (no .fleet.toml port) — continuing")
    else:
        _note(
            logging.WARNING, name,
            f"readiness timed out (:{port} not answering after {_TRAY_READY_TIMEOUT_S:.0f}s)",
        )
    return False


def _load_registry_with_retry() -> Optional[Registry]:
    """The registry, or ``None`` after logging why it could not be read."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, _REGISTRY_LOAD_ATTEMPTS + 1):
        try:
            return load_registry(strict=True)
        except RegistryReadError as exc:
            last_exc = exc
            logger.warning(
                f"⚠️  Registered Trays: registry read failed "
                f"(attempt {attempt}/{_REGISTRY_LOAD_ATTEMPTS}): {exc}"
            )
            _log_breadcrumb(
                f"registry read failed (attempt {attempt}/{_REGISTRY_LOAD_ATTEMPTS}): {exc}"
            )
            if attempt < _REGISTRY_LOAD_ATTEMPTS:
                time.sleep(_REGISTRY_LOAD_RETRY_DELAY_S)
        except Exception as exc:  # noqa: BLE001 — unexpected, not retried
            logger.warning(f"⚠️  Registered Trays: could not load registry: {exc}")
            _log_breadcrumb(f"could not load registry: {exc}")
            return None
    logger.warning(
        f"⚠️  Registered Trays: registry unreadable after "
        f"{_REGISTRY_LOAD_ATTEMPTS} attempts — giving up: {last_exc}"
    )
    _log_breadcrumb(
        f"registry unreadable after {_REGISTRY_LOAD_ATTEMPTS} attempts "
        f"— giving up: {last_exc}"
    )
    return None


def launch_all(stop: Optional[threading.Event] = None) -> None:
    """Walk autostart-enabled Registered Trays entries one at a time,
    waiting for each to report ready before starting the next — avoids
    a boot-time CPU/disk spike from launching several sister
    Python/Streamlit processes concurrently. The registry's existing
    alphabetical order (see :func:`src.registry.persist_additions`) is
    the "fixed order" the issue accepts for v1 — no UI reordering.

    A tray with a ``.fleet.toml`` port that the first pass could not confirm
    ready then gets the bounded retries of :func:`_reconcile`, which ``stop``
    (the tray's Quit event) cuts short.

    One entry failing to launch, or its readiness check timing out,
    is logged and does NOT abort the rest of the sequence — this
    function itself must never raise into its caller (``TrayApp._start``).
    """
    registry = _load_registry_with_retry()
    if registry is None:
        return
    trays = [
        a for a in registry.apps if a.kind in (KIND_TRAY, KIND_MINI) and a.autostart
    ]
    if not trays:
        return
    logger.info(f"🧭 Registered Trays: launching {len(trays)} autostart entr{'y' if len(trays) == 1 else 'ies'}")
    _log_breadcrumb(f"launching {len(trays)} autostart entr{'y' if len(trays) == 1 else 'ies'}")
    pending: list = []
    for entry in trays:
        if not entry.bat_path:
            _note(logging.WARNING, entry.name, "no bat_path — skipped")
            continue
        bat_path = Path(entry.bat_path)
        if not bat_path.is_file():
            _note(logging.WARNING, entry.name, f"bat not found at {bat_path} — skipped")
            continue
        try:
            if entry.kind == KIND_MINI:
                # A mini app declares no port, so there is nothing to wait on.
                if not _already_running(entry.name, bat_path):
                    _note(logging.INFO, entry.name, f"launching {bat_path}")
                    _spawn_tray_bat_detached(bat_path)
                continue
            port = _fleet_toml_port(bat_path.parent)
            if port is not None and port_listening(port):
                _note(logging.INFO, entry.name, f":{port} already answering — not launched")
                continue
            if _spawn_and_wait(entry.name, bat_path, port, "launching "):
                _note(logging.INFO, entry.name, "ready")
            elif port is not None:
                pending.append((entry.name, bat_path, port))
        except Exception as exc:  # noqa: BLE001
            _note(logging.WARNING, entry.name, f"failed to launch: {exc}")
    if pending:
        _reconcile(pending, stop if stop is not None else threading.Event())


def _reconcile(pending: list, stop: threading.Event) -> None:
    """Retry each ``(name, bat_path, port)`` not confirmed ready after the
    first pass, on the :data:`_RETRY_DELAYS_S` backoff, then give up.

    Every retry checks first, and launches only when the port is silent and
    no process is running the entry: a duplicate tray is worse than a missed
    one, so an unreadable process table means no launch that round.
    """
    total = len(_RETRY_DELAYS_S)
    for n, delay in enumerate(_RETRY_DELAYS_S, start=1):
        if stop.wait(delay):
            _log_breadcrumb(f"tray stopping — retries abandoned for {len(pending)} pending")
            return
        pending = [p for p in pending if not _retry_one(*p, f"retry {n}/{total}")]
        if not pending:
            return
    for name, _bat, _port in pending:
        _note(logging.WARNING, name, f"failed after {total} retries")


def _retry_one(name: str, bat_path: Path, port: int, label: str) -> bool:
    """One retry for one entry; True once it needs no further retry."""
    if port_listening(port):
        _note(logging.INFO, name, f"{label}: :{port} answering — ready, not relaunched")
        return True
    try:
        pid = find_running_target(bat_path)
    except RuntimeError as exc:
        _note(logging.WARNING, name, f"{label}: could not check it is not running ({exc}) — not relaunched")
        return False
    if pid is not None:
        _note(logging.INFO, name, f"{label}: already running (pid {pid}) — not relaunched")
        return True
    if _spawn_and_wait(name, bat_path, port, f"{label}: relaunching "):
        _note(logging.INFO, name, f"ready on {label}")
        return True
    return False


def _already_running(name: str, bat_path: Path) -> bool:
    """True when a mini app's target is up already, or cannot be ruled out.

    An unreadable process table skips the launch: a missed autostart is one
    tap on the row, a duplicate is a second copy of something that may hold a
    port, a lock or a tray icon.
    """
    try:
        pid = find_running_target(bat_path)
    except RuntimeError as exc:
        logger.warning(f"⚠️  Registered Trays: {name} not started, could not check it is not running: {exc}")
        _log_breadcrumb(f"{name}: not started, could not check it is not running: {exc}")
        return True
    if pid is None:
        return False
    logger.info(f"ℹ️  Registered Trays: {name} already running (pid {pid}) — skipped")
    _log_breadcrumb(f"{name}: already running (pid {pid}) — skipped")
    return True
