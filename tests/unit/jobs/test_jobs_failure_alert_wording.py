"""A failed run's push alert: wording per outcome, and the retired per-job flag.

``Job.alert_on_failure`` (a per-job Telegram alert) no longer exists: the only
failure channels are the global Pushover push and the chief mailbox event. A
``jobs.json`` that still carries the key loads without it and is written back
without it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src import jobs as jobs_mod
from src import notifications as notif
from src.jobs import history as jobs_history_mod
from src.jobs.config import Job, job_from_dict


class TestRetiredAlertOnFailureFlag:
    def test_a_legacy_key_is_dropped_on_load_and_never_written_back(self):
        job = job_from_dict(
            {"id": "j", "name": "J", "script_path": "C:/x/s.py", "alert_on_failure": True}
        )
        assert not hasattr(job, "alert_on_failure")
        assert "alert_on_failure" not in job.to_dict()


class TestNotifyUnconfirmedIsNotAFailureAlert:
    """Issue #916 — the loudest instance of the same defect.

    Four healthy weekly jobs exited 122 ("the completion stream was truncated,
    delivery was never verified") and each pushed ``❌ <job> failed`` to the
    phone at error severity. An alert channel that cries failure on healthy
    runs is an alert channel that stops being read, which is what buried the
    genuine 123s beside them.
    """

    def _cfg(self, **kw):
        defaults = dict(
            pushover_api_token="tok",
            pushover_user_key="user",
            notify_on_failure=True,
            notify_failure_streak=0,
            notify_failure_summary=False,
            llm_hub_url="",
        )
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def _job(self):
        return Job(id="demo", name="Demo", script_path=r"C:\ok.py")

    def _fire(self, tmp_path, monkeypatch, *, exit_code, **kwargs):
        monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
        rd = jobs_mod.new_run_dir("demo", "20260524T080000")
        jobs_mod.write_run_json(rd, status="failed", exit_code=exit_code)
        pushover = MagicMock()
        notif.notify_failure(
            self._cfg(), self._job(), rd,
            status="failed", exit_code=exit_code,
            notifier=pushover, **kwargs,
        )
        return pushover

    def test_unconfirmed_run_does_not_say_failed(self, tmp_path, monkeypatch):
        pushover = self._fire(tmp_path, monkeypatch, exit_code=122)
        title, body = pushover.notify.call_args.args[:2]
        assert "failed" not in title.lower()
        assert "not confirmed" in title.lower()
        # It still explains itself, so the ping is actionable on its own.
        assert "never verified" in body

    def test_unconfirmed_run_still_pages_but_not_at_error_severity(
        self, tmp_path, monkeypatch
    ):
        """It must still arrive — nobody established that it delivered — but
        at Pushover priority 0 rather than the quiet-hours-bypassing 1."""
        pushover = self._fire(tmp_path, monkeypatch, exit_code=122)
        pushover.notify.assert_called_once()
        assert pushover.notify.call_args.kwargs["severity"] == "warning"

    def test_genuine_failure_still_says_failed_at_error_severity(
        self, tmp_path, monkeypatch
    ):
        """The failure title stays "❌ Demo". Only the body gains the exit
        code's meaning, and only the new state gains a word."""
        pushover = self._fire(tmp_path, monkeypatch, exit_code=123)
        assert pushover.notify.call_args.args[0] == "❌ Demo"
        assert "not confirmed" not in pushover.notify.call_args.args[0]
        assert pushover.notify.call_args.kwargs["severity"] == "error"
        assert "delivered no work" in pushover.notify.call_args.args[1]

    def test_incomplete_work_118_pages_as_not_confirmed(self, tmp_path, monkeypatch):
        """#959: a run that delivered and then left owned descendants running
        exits 118. It still pages, but as ❓ not confirmed at warning — never
        as a failed delivery assertion that did not exist."""
        pushover = self._fire(tmp_path, monkeypatch, exit_code=118)
        pushover.notify.assert_called_once()
        title, body = pushover.notify.call_args.args[:2]
        assert title == "❓ Demo — not confirmed"
        assert pushover.notify.call_args.kwargs["severity"] == "warning"
        assert "unfinished" in body
        assert "delivered no work" not in body

    def test_watchdog_kill_is_a_failure_whatever_code_it_reported(
        self, tmp_path, monkeypatch
    ):
        """A run this launcher tore down cannot buy an "unconfirmed" with the
        exit code its dying tree happened to report."""
        pushover = self._fire(
            tmp_path, monkeypatch, exit_code=122,
            watchdog_note="watchdog: no output for 68min",
        )
        assert "not confirmed" not in pushover.notify.call_args.args[0]
        assert pushover.notify.call_args.kwargs["severity"] == "error"

    def test_stall_alert_names_the_stall(self, tmp_path, monkeypatch):
        """124 stays a failure, but the ping says "stalled" instead of leaving
        the reader to look up what 124 means."""
        pushover = self._fire(tmp_path, monkeypatch, exit_code=124)
        body = pushover.notify.call_args.args[1]
        assert "stalled" in body
