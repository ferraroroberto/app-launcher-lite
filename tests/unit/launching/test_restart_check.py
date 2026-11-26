"""A restart that did not replace the webapp process must fail the check."""

from __future__ import annotations

import json
import subprocess

import pytest

from src.launching import restart_check

HEAD = "86224f1b"


@pytest.fixture(autouse=True)
def _pids_run_from_this_checkout(monkeypatch):
    """The fake pids below stand for processes of this checkout; the two-checkout cases live in
    ``test_process_checkout.py`` with real processes."""
    monkeypatch.setattr(restart_check, "process_checkout", lambda _pid: restart_check._REPO_ROOT)


def _version(pid, sha=HEAD):
    body = {"git_sha": sha}
    if pid is not None:
        body["pid"] = pid
    return body


class TestDecide:
    def test_same_pid_at_head_is_not_a_restart(self):
        ok, reason = restart_check.decide({"pid": 100}, _version(100), HEAD)
        assert not ok
        assert "same process" in reason

    def test_new_pid_at_head_passes(self):
        ok, _ = restart_check.decide({"pid": 100}, _version(200), HEAD)
        assert ok

    def test_nothing_running_before_accepts_any_new_process(self):
        ok, _ = restart_check.decide({"pid": None}, _version(200), HEAD)
        assert ok

    def test_new_pid_on_the_wrong_sha_fails(self):
        ok, reason = restart_check.decide({"pid": 100}, _version(200, sha="deadbeef"), HEAD)
        assert not ok
        assert "expected HEAD" in reason

    def test_nothing_answering_fails(self):
        ok, _ = restart_check.decide({"pid": 100}, None, HEAD)
        assert not ok

    def test_an_answer_without_a_pid_is_not_confirmed(self):
        ok, reason = restart_check.decide({"pid": 100}, _version(None), HEAD)
        assert not ok
        assert "no pid" in reason

    def test_an_owner_whose_checkout_cannot_be_read_is_not_confirmed(self):
        ok, reason = restart_check.decide({"pid": 100}, _version(200), HEAD, checkout_of=lambda _pid: None)
        assert not ok
        assert "cannot confirm pid 200" in reason

    def test_unknown_sha_never_passes(self):
        ok, _ = restart_check.decide({"pid": 100}, _version(200, sha="unknown"), HEAD)
        assert not ok


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(restart_check, "BASELINE_FILE", tmp_path / "baseline.json")
    monkeypatch.setattr(restart_check, "RESTART_LOG", tmp_path / "restart.log")
    monkeypatch.setattr(restart_check, "resolve_git_sha", lambda: HEAD)
    monkeypatch.setattr(restart_check.time, "sleep", lambda _s: None)
    return tmp_path


class TestVerify:
    def test_unchanged_process_exits_nonzero_and_logs(self, files, monkeypatch):
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: _version(100))
        assert restart_check.snapshot(8465) == 0
        assert restart_check.verify(8465, timeout_s=0) == 1
        log = (files / "restart.log").read_text(encoding="utf-8")
        assert "verify FAILED" in log and "pid 100" in log

    def test_replaced_process_passes(self, files, monkeypatch):
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: _version(100))
        restart_check.snapshot(8465)
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: _version(200))
        assert restart_check.verify(8465, timeout_s=0) == 0

    def test_missing_baseline_is_not_confirmed(self, files, monkeypatch):
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: _version(200))
        assert restart_check.verify(8465, timeout_s=0) == 2
        assert "NOT CONFIRMED" in (files / "restart.log").read_text(encoding="utf-8")

    def test_baseline_for_another_port_is_not_confirmed(self, files, monkeypatch):
        (files / "baseline.json").write_text(json.dumps({"port": 9999, "pid": 1}), encoding="utf-8")
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: _version(200))
        assert restart_check.verify(8465, timeout_s=0) == 2

    def test_snapshot_with_nothing_running_records_no_pid(self, files, monkeypatch):
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: None)
        restart_check.snapshot(8465)
        assert json.loads((files / "baseline.json").read_text(encoding="utf-8"))["pid"] is None


class _FakeTray:
    """Stands in for ``subprocess.run`` of tray.bat: records the call, serves ``pid`` after it."""

    def __init__(self, served, returncode=0, raises=None):
        self.served, self.returncode, self.raises = served, returncode, raises
        self.argv = None
        self.kwargs = None

    def __call__(self, argv, **kwargs):
        self.argv, self.kwargs = argv, kwargs
        if self.raises:
            raise self.raises
        self.served["pid"] = 200 if self.returncode == 0 else self.served["pid"]
        return subprocess.CompletedProcess(argv, self.returncode)


class TestRestart:
    @pytest.fixture
    def tray(self, files, monkeypatch):
        bat = files / "tray.bat"
        bat.write_text("@echo off\n", encoding="utf-8")
        monkeypatch.setattr(restart_check, "TRAY_BAT", bat)
        served = {"pid": 100}
        monkeypatch.setattr(restart_check, "fetch_version", lambda _p: _version(served["pid"]))
        return served

    def test_new_pid_after_the_bat_passes_and_runs_it_by_absolute_path(self, files, tray):
        fake = _FakeTray(tray)
        assert restart_check.restart(8465, timeout_s=0, run=fake) == 0
        assert fake.argv == [str(files / "tray.bat"), "--restart"]
        assert fake.kwargs["creationflags"] == restart_check.NO_WINDOW
        assert fake.kwargs["timeout"] == restart_check.TRAY_RESTART_TIMEOUT_S

    def test_replace_other_checkout_is_passed_to_the_bat(self, files, tray):
        fake = _FakeTray(tray)
        assert restart_check.restart(8465, timeout_s=0, run=fake, replace_other_checkout=True) == 0
        assert fake.argv == [str(files / "tray.bat"), "--restart", "--replace-other-checkout"]

    def test_a_bat_that_exits_zero_without_replacing_the_process_fails(self, files, tray):
        # The reported defect: the call exits 0, nothing restarts, the old pid keeps serving.
        def noop(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 0)

        assert restart_check.restart(8465, timeout_s=0, run=noop) == 1
        assert "same process still serving" in (files / "restart.log").read_text(encoding="utf-8")

    def test_a_nonzero_bat_fails_without_waiting_on_verify(self, files, tray):
        assert restart_check.restart(8465, timeout_s=0, run=_FakeTray(tray, returncode=3)) == 1
        log = (files / "restart.log").read_text(encoding="utf-8")
        assert "tray.bat exited 3" in log and "verify" not in log

    def test_a_hung_bat_fails_with_its_own_message(self, files, tray):
        hung = _FakeTray(tray, raises=subprocess.TimeoutExpired("tray.bat", 1))
        assert restart_check.restart(8465, timeout_s=0, run=hung) == 1
        assert "did not complete" in (files / "restart.log").read_text(encoding="utf-8")

    def test_a_missing_bat_is_not_confirmed(self, files, monkeypatch):
        monkeypatch.setattr(restart_check, "TRAY_BAT", files / "nope.bat")
        assert restart_check.restart(8465, timeout_s=0, run=_FakeTray({"pid": 1})) == 2


def _host(reachable=True, freshness="fresh"):
    return {"session_host": {
        "reachable": reachable, "git_sha": "c516e60", "started_at": "t",
        "stale": False, "stale_relevant": False, "freshness": freshness,
    }}


class _Clock:
    """Virtual time: ``sleep`` advances it, so a 45 s wait costs no wall time."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TestWaitSessionHost:
    def _run(self, files, answers, timeout_s=45.0):
        clock = _Clock()
        calls = []

        def fetch(_port):
            calls.append(clock.now)
            return answers(clock.now)

        code = restart_check.wait_session_host(
            8465, timeout_s, fetch=fetch, clock=clock, sleep=clock.sleep
        )
        return code, calls, (files / "restart.log").read_text(encoding="utf-8")

    def test_slow_but_successful_cold_start_reads_as_success(self, files):
        # Unreachable for 30 s (the old deadline), then fresh: a success, not a timeout.
        code, calls, log = self._run(
            files, lambda t: _host(reachable=False) if t < 31 else _host()
        )
        assert code == 0
        assert calls[-1] >= 31
        assert "session-host live" in log and "NOT" not in log

    def test_stale_irrelevant_is_a_success(self, files):
        code, _, _ = self._run(files, lambda _t: _host(freshness="stale-irrelevant"))
        assert code == 0

    def test_never_answering_fails_with_the_waited_time(self, files):
        code, _, log = self._run(files, lambda _t: _host(reachable=False))
        assert code == 1
        assert "NOT ANSWERING" in log and "after waiting 45s" in log

    def test_answering_on_an_older_build_is_a_distinct_failure(self, files):
        code, _, log = self._run(files, lambda _t: _host(freshness="stale-relevant"))
        assert code == 1
        assert "NOT CONFIRMED" in log and "freshness=stale-relevant" in log
        assert "NOT ANSWERING" not in log

    def test_an_unresolved_verdict_is_not_a_pass(self, files):
        code, _, log = self._run(files, lambda _t: _host(freshness="unknown"))
        assert code == 1 and "NOT CONFIRMED" in log

    def test_no_webapp_answer_is_its_own_failure(self, files):
        code, _, log = self._run(files, lambda _t: None)
        assert code == 1
        assert "no webapp answering" in log and "NOT ANSWERING" not in log


class TestFetchVersionFallsBackToHttp:
    def test_http_only_webapp_is_read(self, monkeypatch):
        # The tray serves plain http without a TLS cert; the restart script used to probe https only.
        import requests

        class _Resp:
            status_code = 200

            def json(self):
                return {"pid": 1}

        def fake(_method, url, **_kw):
            if url.startswith("https"):
                raise requests.ConnectionError("no TLS here")
            return _Resp()

        monkeypatch.setattr(restart_check, "pooled_request", fake)
        assert restart_check.fetch_version(8465) == {"pid": 1}
