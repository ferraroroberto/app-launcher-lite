"""Fixtures for the Playwright smoke suite.

Two run modes:

* **Live (dev loop).** Runs against a live tray the user already has up on
  https://127.0.0.1:8465 — but only with the explicit `LAUNCHER_E2E_LIVE=1`
  opt-in (`scripts/run-e2e.ps1` sets it). Without it the suite *exits* with a
  guard message instead of running: a bare `pytest tests/e2e` used to silently
  load-test the instance the phone was using (issue #386). The autouse
  `_require_live_tray` fixture still skips the whole module with a clear
  message if /healthz isn't reachable, so a forgotten tray fails fast instead
  of hanging in browser.goto for 30 s.
* **Autoboot (pre-ship gate).** Enabled with `--e2e-autoboot` or the
  `LAUNCHER_E2E_AUTOBOOT=1` env var. `_autoboot_server` spawns a disposable
  webapp on a free port (HTTPS, reusing webapp/certificates/) plus a
  session-host on :8466 — adopting an already-listening one (a running tray)
  or spawning its own. In this mode a failure to boot is a hard *failure*,
  never a skip: the whole point of the gate is that a missing server can't
  silently pass. See issue #33.

This app is Android-only, so the suite drives Chromium exclusively — no
WebKit engine, ever (issue #6). The suite still runs two *projections* per
test: desktop (default viewport) and phone (`phone_projection` fixture below,
merging in `playwright.devices["Pixel 8 Pro"]` — touch, mobile viewport, an
actual Android device rather than the old WebKit+iPhone stand-in; picked over
the narrower Pixel 7 so the width headroom already tuned against the old
430px iPhone viewport carries over unchanged). Tests that special-cased
`browser_name == "webkit"` for phone-only behaviour now key off
`phone_projection` instead. `tests/e2e/_browser_sweep.py` stays vendored from
project-scaffolding (`.fleet.toml`) but this conftest no longer calls it: its
sweep targets WebKit-family helper processes only, which this suite never
spawns.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib3
from pathlib import Path
from typing import Callable, IO, Iterator, List, Optional, Tuple

import pytest
import requests
from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page

from src.scanner import dir_ignored
from tests.e2e._shot import CHROMIUM_CAPTURE_ARGS

logger = logging.getLogger(__name__)

# The webapp uses a self-signed cert; silence the urllib3 noise from /healthz.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEBAPP_CONFIG = _REPO_ROOT / "config" / "webapp_config.json"
_SESSIONS_DIR = _REPO_ROOT / "webapp" / "sessions"
_BASE_URL = "https://127.0.0.1:8465"
_TOKEN_KEY = "launcher.token"  # must match TOKEN_KEY in app/webapp/static/state.js:21

# The live tray's loopback PTY session-host port. Autoboot must NEVER adopt a
# host listening here: on a dev box it holds the user's real PTY/Claude
# sessions — including the one running the gate — and the destructive e2e
# tests would kill them (issue #260). Instead autoboot always spawns its own
# disposable session-host on a free port and points the disposable webapp at
# it via LAUNCHER_SESSION_HOST_PORT. This constant is kept only so the
# isolation guarantee can be asserted (never bound/adopted under autoboot).
_LIVE_SESSION_HOST_PORT = 8466
# Env var the webapp honours to override its session-host port (see
# src/webapp_config.py:SESSION_HOST_PORT_ENV) — the injection that isolates
# the gate from the live :8466.
_SESSION_HOST_PORT_ENV = "LAUNCHER_SESSION_HOST_PORT"
# Env var the webapp honours to override its config file *path* (see
# src/webapp_config.py:WEBAPP_CONFIG_PATH_ENV) — the injection that stops a
# Settings-tab e2e Save from ever mutating the user's real
# config/webapp_config.json (issue #441; the #438 port corruption was this
# exact shared-file design biting). Autoboot points the disposable webapp at
# a temp COPY of the real config so it still boots with realistic values.
_WEBAPP_CONFIG_PATH_ENV = "LAUNCHER_WEBAPP_CONFIG"
# Env var the webapp honours to override the boot-autostart Startup directory
# (see src/boot_autostart.py:STARTUP_DIR_ENV) — the injection that stops the
# boot-autostart e2e test from ever reading/writing the real per-user Startup
# folder (issue #698). Without it, `/api/settings/boot-autostart` resolves
# the real folder, so the test could only pass on a host with no
# AppLauncher.bat installed there for real login-time autostart.
_STARTUP_DIR_ENV = "LAUNCHER_STARTUP_DIR"
_AUTOBOOT_ENV = "LAUNCHER_E2E_AUTOBOOT"
# Sentinel flag for the lightweight PTY child (issue #534). Under autoboot the
# disposable session-host's PATH is prepended with a harness-generated
# `copilot.cmd` shim: a launch whose flags are exactly this sentinel runs a
# tiny deterministic Python echo loop instead of the real Copilot CLI (slow
# startup each), while any other flag set falls through to the real `copilot`.
# Purely a harness substitution — no production code knows about it.
_STUB_FLAG = "--e2e-stub"
# Filled by _autoboot_server so the lightweight fixture can create sessions
# directly on the disposable session-host (the sentinel flag can't travel
# through the webapp's launch endpoint, which builds flags from config).
_AUTOBOOT_STATE: dict = {}
# Explicit opt-in for targeting the LIVE tray on :8465 (issue #386). The
# live mode is deliberate (run-e2e.ps1 dev loop), but it drives real login
# flows and PTY sessions against the instance the user's phone is using —
# an *accidental* bare `pytest tests/e2e` must not do that.
_LIVE_ENV = "LAUNCHER_E2E_LIVE"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e-autoboot",
        action="store_true",
        default=False,
        help="Boot a disposable webapp + session-host instead of requiring a "
        "live tray. Equivalent to LAUNCHER_E2E_AUTOBOOT=1.",
    )


def pin_scan_to_checkout(cfg: dict, checkout: Path, keep: tuple = ()) -> dict:
    """Make ``cfg`` scan ``checkout`` (plus ``keep``) and no other project.

    The SPA fetches ``/api/coding/git-status`` on every page boot, and that
    runs ``git`` once per scanned project. With the default ``projects_dir``
    (the checkout's parent, ~150 folders on the dev box) each test's boot
    spawned dozens of git processes, and the disposable webapps spent their
    time in git until unrelated waits timed out under load (#58; the same
    cause as app-launcher#1512). Every sibling of ``checkout`` not named in
    ``keep`` is ignored; a test that needs other project rows mocks
    ``/api/apps`` or git-status. Mutates and returns ``cfg``.
    """
    shown = {checkout.name, *keep}
    cfg["projects_dir"] = str(checkout.parent)
    kept = [
        pattern
        for pattern in (cfg.get("projects_ignore") or [])
        if not any(dir_ignored(name, [pattern]) for name in shown)
    ]
    try:
        siblings = sorted(
            child.name for child in checkout.parent.iterdir()
            if child.name not in shown
        )
    except OSError as exc:
        logger.warning("⚠️  could not list %s, the e2e webapp will scan all of it: %s",
                       checkout.parent, exc)
        siblings = []
    # glob.escape: a sibling name holding [ ] * ? must match only itself.
    cfg["projects_ignore"] = kept + [glob.escape(name) for name in siblings]
    return cfg


def _autoboot_enabled(config: pytest.Config) -> bool:
    return bool(config.getoption("--e2e-autoboot")) or (
        os.environ.get(_AUTOBOOT_ENV, "") == "1"
    )


def stable_read(read: Callable[[], object], attempts: int = 50,
                interval_s: float = 0.1) -> object:
    """Retry a raw DOM measurement past a mid-render stale element handle.

    Playwright's ``locator.evaluate()`` / ``bounding_box()`` resolve the
    selector to an element handle and *then* read from it. Any surface that
    rebuilds its DOM on a timer can invalidate that handle in between, and the
    read silently returns an artifact rather than raising: WebKit yields ``''``
    from ``getComputedStyle`` (shorthand *and* longhand) and ``None`` from
    ``bounding_box()``; ``scrollWidth``/``clientWidth`` both read ``0``.

    The Board is exactly such a surface — ``renderBoard()`` unconditionally
    calls ``list.replaceChildren()`` on every column, and ``fetchBoard()``
    re-renders every ``BOARD_POLL_MS`` (5 s) while the Board tab is up with no
    drawer open. So a board test that runs longer than 5 s *will* eventually
    read across a rebuild (#680: measured 3 bad reads in 700 with every fetch
    stubbed, at the 5 s cadence). Auto-retrying ``expect()`` assertions
    re-resolve and are immune; these raw reads are not.

    Returns the first read that isn't one of those artifacts, so the caller
    asserts on a real measurement. Assertion strength is unchanged — only
    known-invalid readings are skipped, and a genuinely wrong value is
    returned as-is on the first attempt.
    """
    value: object = None
    for _ in range(attempts):
        value = read()
        if value not in ("", None):
            return value
        time.sleep(interval_s)
    return value


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _spawn(
    cmd: List[str],
    log: IO[str],
    extra_env: Optional[dict] = None,
) -> subprocess.Popen:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if extra_env:
        env.update(extra_env)
    kwargs: dict = dict(
        cwd=str(_REPO_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )
    if sys.platform == "win32":
        # New process group so we can deliver CTRL_BREAK for a clean stop;
        # no window so the test run doesn't flash consoles.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    return subprocess.Popen(cmd, **kwargs)


def _terminate(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("CTRL_BREAK_EVENT failed: %s", exc)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("⚠️  autoboot: process teardown failed: %s", exc)


def _wait_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_listening(port):
            return True
        time.sleep(0.3)
    return False


def _wait_healthz(base: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            res = requests.get(f"{base}/healthz", timeout=2, verify=False)
            if res.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.4)
    return False


_STUB_CHILD_SOURCE = '''\
"""Deterministic lightweight PTY child for UI-only e2e tests (issue #534).

Stands in for the real Copilot CLI under the disposable autoboot session-host:
instant startup, echoes each input line (ConPTY cooked mode echoes keystrokes
too), exits on /exit (Copilot's quit command — what the host's graceful stop
types for agent "copilot") so the graceful stop path works.
"""
import sys

print("[e2e-stub] lightweight PTY child ready (issue #534)", flush=True)
while True:
    line = sys.stdin.readline()
    if not line:
        break
    text = line.rstrip("\\r\\n")
    if text.strip() in ("/exit", "/quit"):
        print("[e2e-stub] bye", flush=True)
        break
    print(text, flush=True)
'''


def _write_copilot_shim(shim_dir: Path) -> None:
    """Generate the `copilot.cmd` PATH shim + stub child script (issue #534).

    The session-host spawns agents via ``cmd /c … && copilot <flags>`` with
    the command resolved off its own PATH, so prepending this directory to the
    *disposable* session-host's PATH intercepts every copilot launch: the
    ``--e2e-stub`` sentinel routes to the stub child, anything else falls
    through to the real ``copilot`` resolved at generation time. Where copilot
    isn't installed (the CI runner) the fall-through branch fails loud — but
    it is never reached there, because `launched_copilot_pty_session` skips
    first (same `shutil.which` guard as always).
    """
    stub_py = shim_dir / "e2e_stub_child.py"
    stub_py.write_text(_STUB_CHILD_SOURCE, encoding="utf-8")
    real_copilot = shutil.which("copilot")
    if real_copilot:
        real_branch = f'call "{real_copilot}" %*\nexit /b %ERRORLEVEL%\n'
    else:
        real_branch = (
            "echo [e2e-shim] real copilot is not installed 1>&2\n"
            "exit /b 1\n"
        )
    shim = (
        "@echo off\n"
        f'if "%~1"=="{_STUB_FLAG}" (\n'
        f'  "{sys.executable}" -X utf8 "{stub_py}"\n'
        "  exit /b %ERRORLEVEL%\n"
        ")\n"
        f"{real_branch}"
    )
    # Text-mode write translates \n -> os.linesep, so the .cmd lands with
    # proper CRLF line endings on Windows.
    (shim_dir / "copilot.cmd").write_text(shim, encoding="ascii")


def _prepare_config_copy(logs_dir: Path) -> Tuple[Path, Optional[bytes]]:
    """Copy the real webapp config into logs_dir; return (copy, real bytes)."""
    # Config isolation (issue #441): the disposable webapp gets a temp COPY
    # of the real config — realistic values (projects_dir, auth_token, …)
    # without write access to the real file. Any e2e test that Saves settings
    # mutates only the copy. Snapshot the real file's bytes so the isolation
    # can be *asserted* after the run, not just assumed.
    cfg_copy = logs_dir / "e2e-autoboot-webapp-config.json"
    real_cfg_bytes = (
        _WEBAPP_CONFIG.read_bytes() if _WEBAPP_CONFIG.exists() else None
    )
    if real_cfg_bytes is not None:
        cfg_copy.write_bytes(real_cfg_bytes)
    elif cfg_copy.exists():
        # No real config (fresh checkout) — a stale copy from a prior run
        # must not leak its values into this one.
        cfg_copy.unlink()

    # Scan scope (#58): the temp copy lists only this checkout (and the
    # real-agent launch target), so a page boot's git-status fan-out stays
    # constant instead of growing with the number of sibling repos.
    pinned = pin_scan_to_checkout(
        json.loads(cfg_copy.read_text(encoding="utf-8")) if cfg_copy.exists() else {},
        _REPO_ROOT,
        keep=(_LAUNCH_TARGET_ID,),
    )
    cfg_copy.write_text(json.dumps(pinned, indent=2), encoding="utf-8")
    return cfg_copy, real_cfg_bytes


def _webapp_cmd(port: int, certs: Optional[Tuple[Path, Path]]) -> List[str]:
    """The uvicorn command line for the disposable webapp."""
    from app.webapp.event_loop import LOOP_FACTORY

    wa_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.webapp.server:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
        "--loop",
        LOOP_FACTORY,
    ]
    if certs:
        cert, key = certs
        wa_cmd += ["--ssl-keyfile", str(key), "--ssl-certfile", str(cert)]
    return wa_cmd


def _assert_isolation_intact(
    cfg_copy: Path,
    real_cfg_bytes: Optional[bytes],
    real_wrapper_bat: Path,
    real_wrapper_bytes: Optional[bytes],
    startup_dir: Path,
) -> None:
    """Raise if the run wrote to the real config or the real Startup folder."""
    # Isolation regression check (issue #441): the real config must be
    # byte-identical to the pre-run snapshot. A mismatch means some path
    # wrote to the real file during the gate — the exact class of bug
    # that corrupted session_host_port in #438 — or, rarely, that the
    # user saved settings on the LIVE tray mid-run. Loud either way.
    current = (
        _WEBAPP_CONFIG.read_bytes() if _WEBAPP_CONFIG.exists() else None
    )
    if current != real_cfg_bytes:
        raise RuntimeError(
            f"e2e autoboot isolation breach: {_WEBAPP_CONFIG} changed "
            "during the run. The disposable webapp must only ever write "
            f"its temp copy ({cfg_copy}). If you changed settings on the "
            "live tray while the gate ran, rerun the gate; otherwise a "
            "test wrote to the real config — fix that before shipping."
        )
    # Startup-folder isolation regression check (issue #698): the real
    # wrapper bat must be byte-identical to the pre-run snapshot. A
    # mismatch means some path wrote to the real Startup folder during
    # the gate instead of the temp startup_dir — the owner boots the
    # launcher from this file.
    current_wrapper_bytes = (
        real_wrapper_bat.read_bytes() if real_wrapper_bat.is_file() else None
    )
    if current_wrapper_bytes != real_wrapper_bytes:
        raise RuntimeError(
            f"e2e autoboot isolation breach: {real_wrapper_bat} changed "
            "during the run. The disposable webapp must only ever write "
            f"the temp Startup dir ({startup_dir}) — a test wrote to the "
            "real Startup folder instead. Fix that before shipping."
        )


def _spawn_disposable_session_host(
    tmp_path_factory: pytest.TempPathFactory, log: IO[str]
) -> Tuple[subprocess.Popen, int]:
    """Start the e2e run's own session-host on a free port; return (proc, port)."""
    # Session-host: ALWAYS spawn our own on a free port — never adopt a
    # host already listening on the live :8466, which on a dev box owns
    # the user's real PTY/Claude sessions (issue #260). A free, disposable
    # host starts empty, so the destructive e2e tests can only ever touch
    # sessions this run launched. The disposable webapp is pointed at it
    # via LAUNCHER_SESSION_HOST_PORT below.
    sh_port = _free_tcp_port()
    sh_cmd = [
        sys.executable,
        str(_REPO_ROOT / "launcher.py"),
        "session-host",
        "--port",
        str(sh_port),
    ]
    # Lightweight-child shim (issue #534): only the DISPOSABLE
    # session-host gets the shim on PATH — the pytest process and the
    # live tray keep the real resolution, so `shutil.which("copilot")`
    # in the fixtures below still faithfully predicts the real CLI.
    shim_dir = tmp_path_factory.mktemp("copilot-shim")
    _write_copilot_shim(shim_dir)
    sh_proc = _spawn(
        sh_cmd,
        log,
        extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
    )
    return sh_proc, sh_port


@pytest.fixture(scope="session")
def _autoboot_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Spawn a disposable webapp (+ session-host) and yield its base URL.

    A hard failure (`pytest.fail`) — never a skip — if anything doesn't come
    up: under the pre-ship gate a missing server must not pass silently.
    """
    from app.webapp.manager import cert_paths
    from src import boot_autostart

    logs_dir = _REPO_ROOT / "webapp"  # gitignored runtime dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    handles: List[IO[str]] = []
    sh_proc: Optional[subprocess.Popen] = None
    wa_proc: Optional[subprocess.Popen] = None

    def _open_log(name: str) -> IO[str]:
        handle = (logs_dir / name).open("w", encoding="utf-8", errors="replace")
        handles.append(handle)
        return handle

    def _teardown() -> None:
        _terminate(wa_proc)
        if sh_proc is not None:  # always ours now (never an adopted tray)
            _terminate(sh_proc)
        for handle in handles:
            try:
                handle.close()
            except Exception:  # pragma: no cover
                pass

    cfg_copy, real_cfg_bytes = _prepare_config_copy(logs_dir)

    # Startup-folder isolation (issue #698): give the disposable webapp its
    # own temp Startup dir so `src.boot_autostart.enable()/disable()` (called
    # with no override by `/api/settings/boot-autostart`) never touches the
    # real per-user Startup folder. That lets the boot-autostart e2e test
    # assume it starts OFF regardless of whether this host has
    # AppLauncher.bat installed for real login-time autostart. Snapshot the
    # real wrapper bat (existence + bytes) so the isolation can be *asserted*
    # after the run, not just assumed — mirrors the webapp-config check below.
    startup_dir = tmp_path_factory.mktemp("startup-dir")
    real_wrapper_bat = boot_autostart.wrapper_bat_path()
    real_wrapper_bytes = (
        real_wrapper_bat.read_bytes() if real_wrapper_bat.is_file() else None
    )

    try:
        sh_proc, sh_port = _spawn_disposable_session_host(
            tmp_path_factory, _open_log("e2e-autoboot-session-host.log")
        )
        _AUTOBOOT_STATE["session_host_port"] = sh_port
        if not _wait_port(sh_port, timeout=15):
            _teardown()
            pytest.fail(
                f"autoboot: session-host did not listen on :{sh_port} "
                "within 15s — see webapp/e2e-autoboot-session-host.log"
            )

        # Webapp on a free port. HTTPS when the cert pair exists (mirrors the
        # real phone path); plain HTTP otherwise so a cert-less checkout still
        # runs the gate.
        port = _free_tcp_port()
        certs = cert_paths()
        scheme = "https" if certs else "http"
        wa_cmd = _webapp_cmd(port, certs)
        # Point the disposable webapp at our disposable session-host, not the
        # config's :8466, and at the temp config copy, not the real file —
        # the two env injections that isolate the gate (issues #260, #441).
        wa_proc = _spawn(
            wa_cmd,
            _open_log("e2e-autoboot-webapp.log"),
            extra_env={
                _SESSION_HOST_PORT_ENV: str(sh_port),
                _WEBAPP_CONFIG_PATH_ENV: str(cfg_copy),
                _STARTUP_DIR_ENV: str(startup_dir),
            },
        )

        base = f"{scheme}://127.0.0.1:{port}"
        if not _wait_healthz(base, timeout=20):
            _teardown()
            pytest.fail(
                f"autoboot: webapp did not answer /healthz at {base} within 20s "
                "— see webapp/e2e-autoboot-webapp.log"
            )
        logger.info("✅ autoboot: webapp ready at %s", base)
        yield base
    finally:
        _teardown()
        _assert_isolation_intact(
            cfg_copy, real_cfg_bytes, real_wrapper_bat, real_wrapper_bytes,
            startup_dir,
        )


@pytest.fixture(scope="session")
def base_url(request: pytest.FixtureRequest) -> str:
    if _autoboot_enabled(request.config):
        return request.getfixturevalue("_autoboot_server")
    return _BASE_URL


@pytest.fixture(scope="session")
def webapp_config() -> dict:
    if not _WEBAPP_CONFIG.exists():
        pytest.skip(f"{_WEBAPP_CONFIG} missing — copy webapp_config.sample.json first")
    return json.loads(_WEBAPP_CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def auth_token(webapp_config: dict) -> str:
    # Loopback bypasses the bearer middleware (server.py:267), so an empty
    # token is fine for local-only tests. We still seed it when present so
    # the SPA boot path mirrors a real phone session.
    return (webapp_config.get("auth_token") or "").strip()


@pytest.fixture(scope="session", autouse=True)
def _require_live_tray(request: pytest.FixtureRequest, base_url: str) -> None:
    # Under autoboot the disposable server is already up — `_autoboot_server`
    # hard-fails if it isn't, so the skip-guard below would be wrong there.
    # The guard only protects the default ad-hoc path against a forgotten tray.
    if _autoboot_enabled(request.config):
        return
    if os.environ.get(_LIVE_ENV, "") != "1":
        pytest.exit(
            "Refusing to run the e2e suite against the LIVE tray on :8465 "
            "without explicit opt-in (issue #386) — an ad-hoc run load-tests "
            "the instance the phone is using. Either set LAUNCHER_E2E_LIVE=1 "
            "(scripts/run-e2e.ps1 does) to target the live tray on purpose, "
            "or use the disposable autoboot mode: --e2e-autoboot / "
            "LAUNCHER_E2E_AUTOBOOT=1.",
            returncode=2,
        )
    try:
        res = requests.get(f"{base_url}/healthz", timeout=2, verify=False)
        res.raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"Tray not running on 8465 ({exc.__class__.__name__}) — "
            "start tray.bat first, then re-run the suite."
        )


@pytest.fixture(scope="session", params=[False, True], ids=["desktop", "phone"])
def phone_projection(request: pytest.FixtureRequest) -> bool:
    """True on the Android/phone projection leg, False on desktop.

    Parametrized here (not via `--browser`, which only selects the engine —
    always Chromium now) so every e2e test that depends on it, directly or
    transitively through `context`/`page`, still runs once per projection —
    the same doubling `browser_name in [chromium, webkit]` used to give for
    free, minus the WebKit engine (issue #6).
    """
    return bool(request.param)


@pytest.fixture(scope="session")
def browser_context_args(
    browser_context_args: dict, phone_projection: bool, playwright
) -> dict:
    # Self-signed cert on 8465 — the SPA + service-worker won't load otherwise.
    args = {**browser_context_args, "ignore_https_errors": True}
    if phone_projection:
        # A real Android device profile — touch, mobile viewport, device
        # scale factor — so the phone-only assertions across the suite
        # exercise an actual Android-shaped target, not the WebKit+iPhone
        # stand-in this used to be (issue #6).
        args = {**args, **playwright.devices["Pixel 8 Pro"]}
    return args


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict) -> dict:
    """Accept the loopback self-signed cert for every context, and rasterise each frame whole.

    The screenshot baseline opens its own contexts (``tests/e2e/_shot.py``), which
    do not inherit ``browser_context_args``; the switch lets them load the
    disposable webapp's HTTPS. ``CHROMIUM_CAPTURE_ARGS`` keeps one DOM painting
    one set of pixels.
    """
    extra = list(browser_type_launch_args.get("args") or [])
    return {**browser_type_launch_args, "args": extra + ["--ignore-certificate-errors", *CHROMIUM_CAPTURE_ARGS]}


# Bound the default Playwright action + navigation timeout (issue #186).
# Playwright defaults both to 30 s, so a single auto-waiting action whose
# target never settles on a loaded hosted runner — a `.click()` / `goto` /
# `wait_for_selector` with no explicit `timeout=` — blocks the full 30 s as an
# *opaque* wait, and a few stacking inside one test reach the 120 s
# `pytest-timeout` (#184) as a black box that never names which wait hung.
# Capping them well under that deadline turns any such hang into a fast,
# self-naming `TimeoutError: ... waiting for <locator>` instead — diagnosable
# from the run page without a `-v` archaeology dig. `expect()` web-first
# assertions keep their own 5 s default, and any explicit per-call `timeout=`
# still overrides this. Env-tunable like E2E_LOG_POLL_DEADLINE_MS so a slow
# runner can widen it without a code change.
_DEFAULT_TIMEOUT_MS = int(os.environ.get("E2E_DEFAULT_TIMEOUT_MS", "15000"))


@pytest.fixture(autouse=True)
def _bound_default_timeouts(context: BrowserContext) -> None:
    # Set on the context, not a single page: authed_page / unauthed_page each
    # `context.new_page()`, and the default is consulted at action time, so a
    # context-level cap covers every page they create.
    context.set_default_timeout(_DEFAULT_TIMEOUT_MS)
    context.set_default_navigation_timeout(_DEFAULT_TIMEOUT_MS)


def _seed_token_init_script(token: str) -> str:
    # Seeded *before* the first navigation so app.js reads it on boot rather
    # than going through the ?token=… URL strip dance (which would leak the
    # token into Playwright trace URLs).
    safe = json.dumps(token)
    safe_key = json.dumps(_TOKEN_KEY)
    return f"window.localStorage.setItem({safe_key}, {safe});"


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


def _retry_goto_on_no_buffer_space(page: Page) -> None:
    """Retry a navigation once when Windows runs out of socket buffers.

    ``net::ERR_NO_BUFFER_SPACE`` (WSAENOBUFS) is the hosted runner's TCP stack
    failing a loopback connect, not anything the page did: it reddened
    ``Page.goto`` in two CI runs (#58). Only that exact error is retried, once,
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


_QUIET_SYSTEM_LOAD = json.dumps({
    "level": "normal", "reason": None, "cpu_percent": 12.0,
    "memory": {"used_bytes": 8 * 1024**3, "total_bytes": 32 * 1024**3, "percent": 25.0},
    "top": [], "other_cores": None, "cores": 8, "age_seconds": 1.0,
})


@pytest.fixture
def authed_page(
    context: BrowserContext, base_url: str, auth_token: str
) -> Iterator[Page]:
    if auth_token:
        context.add_init_script(_seed_token_init_script(auth_token))
    # The header's CPU reading (#65) reads the real box, and the gate loads
    # it: a hot reading would make every header-width test depend on the
    # gate's own load. A quiet default; a page.route() in a test takes
    # precedence over this context route.
    context.route(
        re.compile(r".*/api/system/load$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_QUIET_SYSTEM_LOAD
        ),
    )
    page = context.new_page()
    _retry_goto_on_no_buffer_space(page)
    try:
        yield page
    finally:
        page.close()


@pytest.fixture
def unauthed_page(context: BrowserContext) -> Iterator[Page]:
    page = context.new_page()
    try:
        yield page
    finally:
        page.close()


# ---------------------------------------------------------------- session API
# Opt-in fixtures: tests that need state in #sessionsList depend on one of
# these; other tests don't pay any launch + teardown cost. Target is
# `app-launcher` itself (self-launching is harmless — just spawns the agent
# in this repo dir).

_LAUNCH_TARGET_ID = "app-launcher"


def _auth_headers(auth_token: str) -> dict:
    return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}


def _stop_session(base_url: str, headers: dict, sid: str) -> None:
    """Force-kill a PTY session. `mode: "kill"` is unconditional (vs "quit",
    which waits for the agent to process its quit command). Best-effort — a
    swallowed exception here must not mask the actual test failure."""
    try:
        requests.post(
            f"{base_url}/api/coding/sessions/{sid}/stop",
            json={"mode": "kill"},
            headers=headers,
            verify=False,
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  session %s teardown failed: %s", sid, exc)


def _launch_copilot_via_webapp(base_url: str, auth_token: str) -> str:
    """Launch a REAL copilot PTY session through the webapp's launch endpoint.

    Where `copilot` isn't on PATH — notably the CI runner, which never
    installs it — the PTY child exits at once ("'copilot' is not
    recognized…"), the session-host reaps it, and its WS endpoint then 403s
    the webapp's proxy, so every consumer would race a corpse. Skip cleanly
    instead: these tests genuinely gate on a dev box where `copilot` runs.
    The test process shares the live session-host's PATH (same machine), so
    `which` here faithfully predicts whether the session-host can spawn it.
    See #58.
    """
    if shutil.which("copilot") is None:
        pytest.skip(
            "`copilot` is not on PATH — real-Copilot PTY tests need a live "
            "copilot CLI and skip cleanly where it isn't installed (e.g. the "
            "CI runner)"
        )

    headers = _auth_headers(auth_token)
    try:
        res = requests.post(
            f"{base_url}/api/apps/{_LAUNCH_TARGET_ID}/launch",
            json={"mode": "pty"},
            headers=headers,
            verify=False,
            timeout=10,
        )
    except Exception as exc:
        pytest.skip(f"launch request failed: {exc.__class__.__name__}: {exc}")

    if res.status_code != 200:
        # 400 is the expected failure when the project_dir is invalid — skip
        # cleanly rather than fail the suite.
        pytest.skip(f"could not launch PTY session (HTTP {res.status_code}: {res.text[:200]})")

    body = res.json()
    sid = body.get("session", {}).get("session_id")
    if not sid:
        pytest.skip(f"launch response missing session_id: {body}")
    return str(sid)


@pytest.fixture
def launched_pty_session(
    request: pytest.FixtureRequest, base_url: str, auth_token: str
) -> Iterator[str]:
    """A live PTY session for UI-only assertions (issue #534).

    Under autoboot (the pre-ship gate + CI) the child is the deterministic
    lightweight stub, created directly on the disposable session-host with
    the ``--e2e-stub`` sentinel — no real Copilot CLI process per test. The
    launch API never blocked on the agent's bootstrap, so the win is not big
    idle-box wall time (measured ~20 s across the whole gate, #534): it is
    removing ~110 background agent boots whose CPU contention made loaded
    runs balloon, plus CI coverage (the stub needs only Python). The
    production webapp ↔ session-host ↔ ConPTY boundary stays fully real
    (session rows, WS streaming, input forwarding, stop paths).

    Against the LIVE tray (run-e2e.ps1 dev loop) there is no shim on the
    tray's PATH, so this falls back to a real copilot launch — behaviour
    identical to before the split.

    Tests that assert real agent semantics (rendered agent output, agent
    echo, lifecycle) must use `launched_copilot_pty_session` instead.
    """
    headers = _auth_headers(auth_token)
    if _autoboot_enabled(request.config):
        sh_port = _AUTOBOOT_STATE.get("session_host_port")
        if not sh_port:
            pytest.fail("autoboot state missing session_host_port (issue #534)")
        # POST the session-host directly: the sentinel flag can't travel
        # through the webapp's launch endpoint (flags come from config
        # there). The session still surfaces through the webapp normally —
        # its session list proxies this same host.
        res = requests.post(
            f"http://127.0.0.1:{sh_port}/sessions",
            json={
                "project_dir": str(_REPO_ROOT),
                "name": _LAUNCH_TARGET_ID,
                "flags": _STUB_FLAG,
                "agent": "copilot",
            },
            timeout=15,
        )
        # Deterministic path — a failure here is a harness bug, never a
        # missing-dependency skip.
        if res.status_code != 200:
            pytest.fail(
                f"lightweight stub session failed to launch (HTTP "
                f"{res.status_code}: {res.text[:200]})"
            )
        sid = str(res.json().get("session_id") or "")
        if not sid:
            pytest.fail(f"stub session response missing session_id: {res.text[:200]}")
    else:
        sid = _launch_copilot_via_webapp(base_url, auth_token)

    try:
        yield sid
    finally:
        _stop_session(base_url, headers, sid)


@pytest.fixture
def launched_copilot_pty_session(base_url: str, auth_token: str) -> Iterator[str]:
    """A live PTY session running the REAL Copilot CLI (issue #534).

    Only for tests whose assertions depend on the real agent — rendered
    agent output in the xterm buffer, agent input echo, agent lifecycle
    semantics. Spawns a real agent process per test: keep its consumer set
    minimal, and put UI-only assertions on `launched_pty_session`.
    """
    headers = _auth_headers(auth_token)
    sid = _launch_copilot_via_webapp(base_url, auth_token)
    try:
        yield sid
    finally:
        _stop_session(base_url, headers, sid)


# ----------------------------------------------------- input-delivery polling
# Env-aware so the slow hosted CI runner gets headroom without slowing local
# runs (issue #184, finishing #58): the ConPTY round-trip (keystroke → session
# host → log flush) lands well within 5 s locally but can exceed it on a loaded
# windows-2025 runner. e2e.yml sets E2E_LOG_POLL_DEADLINE_MS larger for CI.
_LOG_POLL_DEADLINE_MS = int(os.environ.get("E2E_LOG_POLL_DEADLINE_MS", "5000"))


@pytest.fixture
def wait_for_session_log() -> Callable[..., bool]:
    """Return a poller for the per-session input log.

    ``wait(page, sid, needle, deadline_ms=_LOG_POLL_DEADLINE_MS)`` reads
    ``webapp/sessions/<sid>.log`` every 200 ms until ``needle`` appears or the
    deadline elapses, then returns ``True``/``False``. One source of truth for
    the input-delivery wait that used to be a hardcoded 5 s poll loop copied
    into four test files (issue #58).
    """

    def _wait(
        page: Page,
        sid: str,
        needle: str,
        deadline_ms: int = _LOG_POLL_DEADLINE_MS,
    ) -> bool:
        log_path = _SESSIONS_DIR / f"{sid}.log"

        def _hit() -> bool:
            return log_path.exists() and needle in log_path.read_text(
                encoding="utf-8", errors="replace"
            )

        for _ in range(max(1, deadline_ms // 200)):
            if _hit():
                return True
            page.wait_for_timeout(200)
        # Final read so a hit landing in the last 200 ms interval isn't missed.
        return _hit()

    return _wait
