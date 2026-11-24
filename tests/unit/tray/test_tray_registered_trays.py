"""Registered Trays sequential boot orchestration (issue #456 part 2/2).

``app.tray.registered_trays`` holds pure functions with no ``TrayApp``
state (split off ``app/tray/tray.py``, a single-file god-module flagged by
``/codebase-audit``), so every orchestration entry point is exercised
directly at module scope — no ``TrayApp`` construction needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import app.tray.registered_trays as rt_mod
from app.tray.registered_trays import (
    TRAY_ANSWERING,
    TRAY_BOUND_NOT_ANSWERING,
    TRAY_DOWN,
    TrayProbe,
    _fleet_toml_port,
)
from src.registry import AppEntry, Registry, RegistryReadError


@pytest.fixture(autouse=True)
def _isolate_boot_breadcrumb_log(monkeypatch, tmp_path: Path):
    """launch_all() now writes a breadcrumb log under PROJECT_ROOT (#788) —
    redirect it to tmp_path for every test in this module so a test that
    doesn't otherwise care about the log can't write into the real repo."""
    monkeypatch.setattr(rt_mod, "PROJECT_ROOT", tmp_path)


def _stub_tray_registry(monkeypatch, tmp_path: Path, *ids: str, extra=()):
    """Autostart tray rows ``ids`` (each with a real tray.bat under tmp_path),
    plus any ``extra`` rows, served by a stubbed ``load_registry``. Returns
    the bat paths in ``ids`` order."""
    bats = []
    for name in ids:
        bat = tmp_path / f"repo-{name}" / "tray.bat"
        bat.parent.mkdir(parents=True, exist_ok=True)
        bat.write_text("@echo off\r\n", encoding="utf-8")
        bats.append(bat)
    registry = Registry(
        scan_root=str(tmp_path),
        apps=[
            AppEntry(id=n, name=n.upper(), kind="tray", bat_path=str(b), autostart=True)
            for n, b in zip(ids, bats)
        ]
        + list(extra),
    )
    monkeypatch.setattr(rt_mod, "load_registry", lambda strict=False: registry)
    return bats


def _write_fleet_toml(repo_dir: Path, port_line: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".fleet.toml").write_text(
        f'layer = "enabling"\n{port_line}\n', encoding="utf-8"
    )


class TestFleetTomlPort:
    def test_bare_int_port(self, tmp_path: Path):
        _write_fleet_toml(tmp_path, "port = 8447")
        assert _fleet_toml_port(tmp_path) == 8447

    def test_leading_colon_string_port(self, tmp_path: Path):
        _write_fleet_toml(tmp_path, 'port = ":8465"')
        assert _fleet_toml_port(tmp_path) == 8465

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert _fleet_toml_port(tmp_path) is None

    def test_missing_port_field_returns_none(self, tmp_path: Path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / ".fleet.toml").write_text('layer = "enabling"\n', encoding="utf-8")
        assert _fleet_toml_port(tmp_path) is None

    def test_malformed_string_port_returns_none(self, tmp_path: Path):
        _write_fleet_toml(tmp_path, 'port = "not-a-port"')
        assert _fleet_toml_port(tmp_path) is None

    def test_bool_port_returns_none(self, tmp_path: Path):
        """bool is an int subclass in Python — a stray `port = true` must
        not be silently treated as port 1."""
        _write_fleet_toml(tmp_path, "port = true")
        assert _fleet_toml_port(tmp_path) is None

    def test_malformed_toml_returns_none(self, tmp_path: Path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / ".fleet.toml").write_text("not { valid toml", encoding="utf-8")
        assert _fleet_toml_port(tmp_path) is None


class TestLaunchRegisteredTrays:
    def test_no_autostart_entries_is_a_noop(self, monkeypatch, tmp_path: Path):
        registry = Registry(
            scan_root=str(tmp_path),
            apps=[
                AppEntry(
                    id="a", name="A", kind="tray", bat_path=str(tmp_path / "tray.bat"),
                    autostart=False,
                ),
            ],
        )
        monkeypatch.setattr(rt_mod, "load_registry", lambda strict=False: registry)
        spawn = MagicMock()
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", spawn)
        rt_mod.launch_all()
        spawn.assert_not_called()

    def test_launches_autostart_trays_in_registry_order(
        self, monkeypatch, tmp_path: Path
    ):
        bat_a, bat_b = _stub_tray_registry(
            monkeypatch, tmp_path, "a", "b",
            extra=[
                AppEntry(
                    id="c", name="C", kind="streamlit",
                    bat_path=str(tmp_path / "repo-c" / "run.bat"), autostart=True,
                )
            ],
        )
        spawned: list = []
        monkeypatch.setattr(
            rt_mod, "_spawn_tray_bat_detached",
            lambda bat_path: spawned.append(bat_path),
        )
        monkeypatch.setattr(rt_mod, "_wait_for_tray_ready", lambda repo_dir: True)

        rt_mod.launch_all()

        # Only the two kind=="tray" autostart entries, in registry order —
        # the streamlit row (wrong kind) is never touched even though its
        # autostart flag is also True.
        assert spawned == [bat_a, bat_b]

    def test_one_tray_failing_does_not_abort_the_rest(
        self, monkeypatch, tmp_path: Path
    ):
        bat_a, bat_b = _stub_tray_registry(monkeypatch, tmp_path, "a", "b")

        def _spawn(bat_path):
            if bat_path == bat_a:
                raise OSError("boom")

        launched: list = []
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", _spawn)
        monkeypatch.setattr(
            rt_mod, "_wait_for_tray_ready",
            lambda repo_dir: launched.append(repo_dir) or True,
        )

        rt_mod.launch_all()

        # repo-a's spawn raised — its readiness wait is never reached — but
        # repo-b still launches.
        assert launched == [bat_b.parent]

    def test_missing_bat_path_is_skipped_not_fatal(self, monkeypatch, tmp_path: Path):
        registry = Registry(
            scan_root=str(tmp_path),
            apps=[
                AppEntry(id="a", name="A", kind="tray", bat_path=None, autostart=True),
            ],
        )
        monkeypatch.setattr(rt_mod, "load_registry", lambda strict=False: registry)
        spawn = MagicMock()
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", spawn)
        rt_mod.launch_all()  # must not raise
        spawn.assert_not_called()

    def test_nonexistent_bat_file_is_skipped_not_fatal(
        self, monkeypatch, tmp_path: Path
    ):
        registry = Registry(
            scan_root=str(tmp_path),
            apps=[
                AppEntry(
                    id="a", name="A", kind="tray",
                    bat_path=str(tmp_path / "does-not-exist" / "tray.bat"),
                    autostart=True,
                ),
            ],
        )
        monkeypatch.setattr(rt_mod, "load_registry", lambda strict=False: registry)
        spawn = MagicMock()
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", spawn)
        rt_mod.launch_all()
        spawn.assert_not_called()

    def test_registry_load_failure_does_not_raise(self, monkeypatch):
        def _boom(strict=False):
            raise OSError("disk on fire")

        monkeypatch.setattr(rt_mod, "load_registry", _boom)
        rt_mod.launch_all()  # must not raise


class TestBootBreadcrumbLog:
    """Issue #788 — launch_all() runs under pythonw at real boot time (no
    console, no output redirection), so a persistent log is the only trace
    of what happened. Redirects PROJECT_ROOT so nothing is written under the
    real repo during tests."""

    @pytest.mark.parametrize(
        ("ready", "expected"),
        [
            (True, ["launching 1 autostart entry", "A: launching", "A: ready"]),
            (False, ["A: readiness unconfirmed"]),
        ],
        ids=["ready", "timeout"],
    )
    def test_logs_each_launch_outcome(self, monkeypatch, tmp_path: Path, ready, expected):
        (bat_a,) = _stub_tray_registry(monkeypatch, tmp_path, "a")
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", lambda bat_path: None)
        monkeypatch.setattr(rt_mod, "_wait_for_tray_ready", lambda repo_dir: ready)

        rt_mod.launch_all()

        log_text = (tmp_path / "webapp" / "registered_trays.log").read_text(encoding="utf-8")
        for line in expected:
            assert line in log_text
        if ready:
            assert str(bat_a) in log_text

    def test_logs_spawn_failure(self, monkeypatch, tmp_path: Path):
        bat_a = tmp_path / "repo-a" / "tray.bat"
        bat_a.parent.mkdir(parents=True, exist_ok=True)
        bat_a.write_text("@echo off\r\n", encoding="utf-8")
        registry = Registry(
            scan_root=str(tmp_path),
            apps=[AppEntry(id="a", name="A", kind="tray", bat_path=str(bat_a), autostart=True)],
        )
        monkeypatch.setattr(rt_mod, "load_registry", lambda strict=False: registry)

        def _boom(bat_path):
            raise OSError("boom")

        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", _boom)

        rt_mod.launch_all()

        log_text = (tmp_path / "webapp" / "registered_trays.log").read_text(encoding="utf-8")
        assert "A: spawn failed: OSError: boom" in log_text

    def test_registry_load_failure_still_logs(self, monkeypatch, tmp_path: Path):

        def _boom(strict=False):
            raise OSError("disk on fire")

        monkeypatch.setattr(rt_mod, "load_registry", _boom)

        rt_mod.launch_all()

        log_text = (tmp_path / "webapp" / "registered_trays.log").read_text(encoding="utf-8")
        assert "could not load registry: disk on fire" in log_text

    def test_registry_read_error_retries_then_succeeds(self, monkeypatch, tmp_path: Path):
        """Issue #925 — a transient RegistryReadError (file exists but a read
        glitches) is retried, and a later success still launches trays
        normally with no leftover "giving up" breadcrumb."""
        bat_a = tmp_path / "repo-a" / "tray.bat"
        bat_a.parent.mkdir(parents=True, exist_ok=True)
        bat_a.write_text("@echo off\r\n", encoding="utf-8")
        registry = Registry(
            scan_root=str(tmp_path),
            apps=[AppEntry(id="a", name="A", kind="tray", bat_path=str(bat_a), autostart=True)],
        )
        calls = {"n": 0}

        def _flaky(strict=False):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RegistryReadError("transient read glitch")
            return registry

        monkeypatch.setattr(rt_mod, "load_registry", _flaky)
        monkeypatch.setattr(rt_mod.time, "sleep", lambda s: None)
        spawn = MagicMock()
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", spawn)
        monkeypatch.setattr(rt_mod, "_wait_for_tray_ready", lambda repo_dir: True)

        rt_mod.launch_all()

        assert calls["n"] == 3
        spawn.assert_called_once_with(bat_a)
        log_text = (tmp_path / "webapp" / "registered_trays.log").read_text(encoding="utf-8")
        assert "registry read failed (attempt 1/3): transient read glitch" in log_text
        assert "registry read failed (attempt 2/3): transient read glitch" in log_text
        assert "giving up" not in log_text

    def test_registry_read_error_exhausts_retries_and_gives_up(
        self, monkeypatch, tmp_path: Path
    ):
        """A persistent RegistryReadError (not a legitimately empty/missing
        registry) is retried a bounded number of times, then gives up with an
        explicit breadcrumb distinct from the silent "nothing configured"
        no-op — the whole point of #925."""

        def _always_boom(strict=False):
            raise RegistryReadError("disk on fire")

        monkeypatch.setattr(rt_mod, "load_registry", _always_boom)
        monkeypatch.setattr(rt_mod.time, "sleep", lambda s: None)
        spawn = MagicMock()
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", spawn)

        rt_mod.launch_all()  # must not raise

        spawn.assert_not_called()
        log_text = (tmp_path / "webapp" / "registered_trays.log").read_text(encoding="utf-8")
        assert "registry read failed (attempt 1/3): disk on fire" in log_text
        assert "registry read failed (attempt 2/3): disk on fire" in log_text
        assert "registry read failed (attempt 3/3): disk on fire" in log_text
        assert "registry unreadable after 3 attempts — giving up" in log_text


class TestWaitForTrayReady:
    def test_no_fleet_toml_falls_back_to_delay(self, monkeypatch, tmp_path: Path):
        sleeps: list = []
        monkeypatch.setattr(rt_mod.time, "sleep", lambda s: sleeps.append(s))
        ready = rt_mod._wait_for_tray_ready(tmp_path)
        assert ready is False
        assert sleeps == [rt_mod._TRAY_FALLBACK_DELAY_S]

    def test_polls_until_the_tray_answers(self, monkeypatch, tmp_path: Path):
        _write_fleet_toml(tmp_path, "port = 8447")
        calls = {"n": 0}

        def fake_probe(port, repo_dir=None):
            calls["n"] += 1
            assert port == 8447
            return TrayProbe(TRAY_ANSWERING if calls["n"] >= 3 else TRAY_BOUND_NOT_ANSWERING, "")

        monkeypatch.setattr(rt_mod, "probe_tray", fake_probe)
        monkeypatch.setattr(rt_mod.time, "sleep", lambda s: None)
        assert rt_mod._wait_for_tray_ready(tmp_path) is True
        assert calls["n"] == 3

    def test_times_out_when_the_tray_never_answers(self, monkeypatch, tmp_path: Path):
        _write_fleet_toml(tmp_path, "port = 8447")
        monkeypatch.setattr(rt_mod, "probe_tray", lambda port, repo_dir=None: TrayProbe(TRAY_DOWN, "down"))
        # Simulate the timeout clock without a real 30s wall-clock wait.
        clock = {"t": 0.0}
        monkeypatch.setattr(rt_mod.time, "monotonic", lambda: clock["t"])

        def fake_sleep(s):
            clock["t"] += s

        monkeypatch.setattr(rt_mod.time, "sleep", fake_sleep)
        assert rt_mod._wait_for_tray_ready(tmp_path) is False


class TestTrayStartLaunchesTraysRegardless:
    """``TrayApp._start`` must launch Registered Trays even when the webapp
    ready-wait fails (#1339). A slow cold-boot uvicorn import used to trip
    ``wait_until_ready``'s timeout, the shared ``try`` skipped
    ``launch_all()``, and nothing reached ``registered_trays.log`` — so no
    sister tray came up and the boot left no trace of why.

    ``_start`` only touches ``self._start_webapp`` and ``self.starter_exc``, so it
    runs off the class against a stub ``self``, the same way
    ``test_tray_session_host_log.py`` drives ``_start_session_host``.
    """

    @pytest.fixture
    def tray(self, monkeypatch):
        from app.tray import tray as tray_mod

        launch_all = MagicMock()
        monkeypatch.setattr(tray_mod.registered_trays, "launch_all", launch_all)
        monkeypatch.setattr(tray_mod, "_notify", lambda title, message: None)
        stub = MagicMock()
        stub.starter_exc = None
        return tray_mod, stub, launch_all

    def test_webapp_wait_failure_still_launches_trays_and_logs_why(self, tray, tmp_path: Path):
        tray_mod, stub, launch_all = tray
        stub._start_webapp.side_effect = RuntimeError("webapp not ready after 15.0s")

        tray_mod.TrayApp._start(stub)

        launch_all.assert_called_once_with(stop=stub.watchdog_stop)
        assert isinstance(stub.starter_exc, RuntimeError)
        log = (tmp_path / "webapp" / "registered_trays.log").read_text(encoding="utf-8")
        assert "webapp not ready after 15.0s" in log

    def test_webapp_ready_launches_trays_once(self, tray, tmp_path: Path):
        tray_mod, stub, launch_all = tray

        tray_mod.TrayApp._start(stub)

        stub._start_webapp.assert_called_once_with(restart=False)
        launch_all.assert_called_once_with(stop=stub.watchdog_stop)
        assert stub.starter_exc is None
        assert not (tmp_path / "webapp" / "registered_trays.log").exists()

    def test_launch_all_raising_never_propagates(self, tray):
        tray_mod, stub, launch_all = tray
        launch_all.side_effect = RuntimeError("unexpected")

        tray_mod.TrayApp._start(stub)  # must not raise

        launch_all.assert_called_once_with(stop=stub.watchdog_stop)


class _FakeStop:
    """A tray stop event whose ``wait`` never blocks and records each delay."""

    def __init__(self, stop_after: int | None = None) -> None:
        self.waits: list = []
        self._stop_after = stop_after

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self._stop_after is not None and len(self.waits) > self._stop_after


class TestReconcileMissedAutostarts:
    """After the first pass, a tray that was not confirmed ready gets a
    bounded number of retries, each preceded by a readiness and identity
    check so a slow tray is never launched twice."""

    @pytest.fixture
    def world(self, monkeypatch, tmp_path: Path):
        (bat,) = _stub_tray_registry(monkeypatch, tmp_path, "a")
        _write_fleet_toml(bat.parent, "port = 8447")
        state = {"up": False, "spawns": 0, "running_pid": None, "drop_first": False}

        def _spawn(bat_path):
            state["spawns"] += 1
            if not (state["drop_first"] and state["spawns"] == 1):
                state["up"] = state.get("up_on_spawn", False)
            return 4242

        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", _spawn)
        monkeypatch.setattr(
            rt_mod, "probe_tray",
            lambda port, repo_dir=None: TrayProbe(
                TRAY_ANSWERING if state["up"] else state.get("probe", TRAY_DOWN), "probe"
            ),
        )
        monkeypatch.setattr(rt_mod, "_wait_for_tray_ready", lambda repo_dir: state["up"])
        monkeypatch.setattr(rt_mod, "find_running_target", lambda b: state["running_pid"])
        def log() -> str:
            return (tmp_path / "webapp" / "registered_trays.log").read_text(encoding="utf-8")

        return state, log

    def test_timeout_then_ready_on_retry(self, world):
        state, log = world
        state["drop_first"], state["up_on_spawn"] = True, True
        stop = _FakeStop()

        rt_mod.launch_all(stop=stop)

        assert state["spawns"] == 2
        assert stop.waits == [rt_mod._RETRY_DELAYS_S[0]]
        text = log()
        assert "A: readiness timed out" in text
        assert "A: retry 1/3: relaunching" in text
        assert "A: ready on retry 1/3" in text

    def test_repeated_timeouts_stop_after_the_budget(self, world):
        state, log = world
        stop = _FakeStop()

        rt_mod.launch_all(stop=stop)

        assert state["spawns"] == 1 + len(rt_mod._RETRY_DELAYS_S)
        assert stop.waits == list(rt_mod._RETRY_DELAYS_S)
        assert "A: failed after 3 retries" in log()

    def test_spawn_oserror_is_told_apart_from_a_timeout(self, world, monkeypatch):
        _state, log = world

        def _boom(bat_path):
            raise OSError("access denied")

        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", _boom)
        rt_mod.launch_all(stop=_FakeStop())

        text = log()
        assert "A: spawn failed: OSError: access denied" in text
        assert "readiness timed out" not in text
        assert "A: failed after 3 retries" in text

    def test_an_entry_already_answering_is_never_launched(self, world):
        state, log = world
        state["up"] = True
        stop = _FakeStop()

        rt_mod.launch_all(stop=stop)

        assert state["spawns"] == 0
        assert stop.waits == []
        assert "A: :8447 already answering — not launched" in log()

    def test_a_retry_finding_the_tray_running_does_not_relaunch(self, world):
        state, log = world
        state["running_pid"] = 9001
        rt_mod.launch_all(stop=_FakeStop())

        assert state["spawns"] == 1
        assert "A: retry 1/3: already running (pid 9001) — not relaunched" in log()

    def test_a_retry_that_cannot_check_identity_never_launches(self, world, monkeypatch):
        state, log = world

        def _unknown(_bat):
            raise RuntimeError("process table unreadable")

        monkeypatch.setattr(rt_mod, "find_running_target", _unknown)
        rt_mod.launch_all(stop=_FakeStop())

        assert state["spawns"] == 1
        assert "A: retry 1/3: could not check it is not running" in log()

    def test_simulated_missed_launch_ends_answering_with_one_copy(self, world):
        """The first spawn is dropped; the retry brings the port up and no
        later retry launches a second copy."""
        state, _log = world
        state["drop_first"], state["up_on_spawn"] = True, True
        rt_mod.launch_all(stop=_FakeStop())

        assert state["up"] is True
        assert state["spawns"] == 2

    def test_tray_quit_abandons_the_retries(self, world):
        state, log = world
        rt_mod.launch_all(stop=_FakeStop(stop_after=0))

        assert state["spawns"] == 1
        assert "tray stopping — retries abandoned" in log()

    def test_no_port_entry_is_never_retried(self, monkeypatch, tmp_path: Path):
        _stub_tray_registry(monkeypatch, tmp_path, "a")
        spawn = MagicMock(return_value=None)
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", spawn)
        monkeypatch.setattr(rt_mod, "_wait_for_tray_ready", lambda repo_dir: False)
        stop = _FakeStop()

        rt_mod.launch_all(stop=stop)

        spawn.assert_called_once()
        assert stop.waits == []

    def test_reconciles_when_the_tray_webapp_failed_to_start(self, world, monkeypatch):
        """``TrayApp._start`` hands its stop event to the real launch_all, so
        a failed webapp start still gets the retries."""
        from app.tray import tray as tray_mod

        state, log = world
        state["drop_first"], state["up_on_spawn"] = True, True
        monkeypatch.setattr(tray_mod, "_notify", lambda title, message: None)
        stub = MagicMock()
        stub.watchdog_stop = _FakeStop()
        stub._start_webapp.side_effect = RuntimeError("webapp not ready after 15.0s")

        tray_mod.TrayApp._start(stub)

        assert state["spawns"] == 2
        assert "A: ready on retry 1/3" in log()


class TestMiniAppsAutostart:
    """A ``mini`` row with autostart on boots with the trays, once.

    The walk runs on every tray start, so a target already up is skipped, and
    one that cannot be checked is skipped too rather than doubled.
    """

    def _registry(self, monkeypatch, tmp_path: Path, *, kind: str = "mini"):
        bat = tmp_path / "mini" / "helper.bat"
        bat.parent.mkdir(parents=True)
        bat.write_text("@echo off\r\n", encoding="utf-8")
        registry = Registry(
            scan_root=str(tmp_path),
            apps=[AppEntry(id="mini-helper", name="Helper", kind=kind,
                           bat_path=str(bat), autostart=True)],
        )
        monkeypatch.setattr(rt_mod, "load_registry", lambda strict=False: registry)
        return bat

    def test_starts_a_mini_app_without_waiting_for_a_port(self, monkeypatch, tmp_path: Path):
        bat = self._registry(monkeypatch, tmp_path)
        monkeypatch.setattr(rt_mod, "find_running_target", lambda b: None)
        spawn, wait = MagicMock(), MagicMock()
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", spawn)
        monkeypatch.setattr(rt_mod, "_wait_for_tray_ready", wait)

        rt_mod.launch_all()

        spawn.assert_called_once_with(bat)
        wait.assert_not_called()

    def test_skips_a_mini_app_that_is_already_running(self, monkeypatch, tmp_path: Path):
        self._registry(monkeypatch, tmp_path)
        monkeypatch.setattr(rt_mod, "find_running_target", lambda b: 777)
        spawn = MagicMock()
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", spawn)

        rt_mod.launch_all()

        spawn.assert_not_called()
        log_text = (tmp_path / "webapp" / "registered_trays.log").read_text(encoding="utf-8")
        assert "Helper: already running (pid 777) — skipped" in log_text

    def test_skips_a_mini_app_it_cannot_check(self, monkeypatch, tmp_path: Path):
        self._registry(monkeypatch, tmp_path)

        def _unknown(_bat):
            raise RuntimeError("process table unreadable")

        monkeypatch.setattr(rt_mod, "find_running_target", _unknown)
        spawn = MagicMock()
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", spawn)

        rt_mod.launch_all()

        spawn.assert_not_called()
        log_text = (tmp_path / "webapp" / "registered_trays.log").read_text(encoding="utf-8")
        assert "could not check it is not running" in log_text

    def test_a_tray_is_never_checked_for_a_running_target(self, monkeypatch, tmp_path: Path):
        self._registry(monkeypatch, tmp_path, kind="tray")

        def _checked(_bat):
            raise AssertionError("a tray row must not be process-checked")

        monkeypatch.setattr(rt_mod, "find_running_target", _checked)
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", MagicMock())
        monkeypatch.setattr(rt_mod, "_wait_for_tray_ready", lambda repo_dir: True)

        rt_mod.launch_all()


class TestReadinessIsHealthNotAnOpenPort:
    """A sister tray whose port is bound is not ready until it answers: autostart never
    launches over it, never counts it ready, and says which state it found."""

    @pytest.fixture
    def world(self, monkeypatch, tmp_path: Path):
        (bat,) = _stub_tray_registry(monkeypatch, tmp_path, "a")
        _write_fleet_toml(bat.parent, "port = 8447")
        spawn = MagicMock(return_value=4242)
        monkeypatch.setattr(rt_mod, "_spawn_tray_bat_detached", spawn)
        monkeypatch.setattr(rt_mod, "find_running_target", lambda b: None)

        def log() -> str:
            return (tmp_path / "webapp" / "registered_trays.log").read_text(encoding="utf-8")

        return spawn, log

    def test_a_bound_port_that_does_not_answer_is_reported_and_retried_not_launched_over(
        self, world, monkeypatch
    ):
        spawn, log = world
        monkeypatch.setattr(
            rt_mod, "probe_tray",
            lambda port, repo_dir=None: TrayProbe(TRAY_BOUND_NOT_ANSWERING, ":8447 accepts connections but no answer"),
        )
        stop = _FakeStop()

        rt_mod.launch_all(stop=stop)

        spawn.assert_not_called()
        assert stop.waits == list(rt_mod._RETRY_DELAYS_S)
        text = log()
        assert "A: bound_not_answering: :8447 accepts connections" in text
        assert "A: failed after 3 retries" in text
        assert "already answering" not in text

    def test_a_stale_build_is_reported_and_never_counted_ready(self, world, monkeypatch):
        spawn, log = world
        monkeypatch.setattr(
            rt_mod, "probe_tray",
            lambda port, repo_dir=None: TrayProbe(rt_mod.TRAY_STALE, "https answering but serves git_sha aaa, HEAD is bbb"),
        )
        stop = _FakeStop()

        rt_mod.launch_all(stop=stop)

        spawn.assert_not_called()
        assert stop.waits == []
        text = log()
        assert "A: stale: https answering but serves git_sha aaa, HEAD is bbb — not launched" in text
        assert "already answering" not in text

    def test_a_bound_port_that_starts_answering_ends_ready_without_a_launch(self, world, monkeypatch):
        spawn, log = world
        answers = iter([TrayProbe(TRAY_BOUND_NOT_ANSWERING, "starting"), TrayProbe(TRAY_ANSWERING, "up")])
        monkeypatch.setattr(rt_mod, "probe_tray", lambda port, repo_dir=None: next(answers))

        rt_mod.launch_all(stop=_FakeStop())

        spawn.assert_not_called()
        assert "A: retry 1/3: :8447 answering — ready, not relaunched" in log()
