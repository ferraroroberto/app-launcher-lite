"""Tests for `chief_ops.py rerun-job`: one re-run per failed run, only for a `rerun_safe: true` job.

Run:  python -m unittest discover -s tests -v

The launcher is a fake `_request` scripted per path, so each verdict is driven
without a webapp; the ledger lives in a temp state dir.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "_lib"))

from chief import lite_rerun  # noqa: E402

JOB = "nightly"
RUN = "20260101-000000-aaaa"


def fake_launcher(*, jobs, run_status="failed", new_run="20260101-000100-bbbb", post_error=None, run_error=None):
    """A `launcher._request` stand-in; `calls` records `(method, path)`."""
    calls = []

    def request(base_url, path, method="GET", body=None, timeout=10.0):
        calls.append((method, path))
        if path == "/api/jobs":
            if isinstance(jobs, Exception):
                raise jobs
            return jobs
        if method == "POST":
            if post_error is not None:
                raise post_error
            return {"run_id": new_run} if new_run else {}
        if run_error is not None:
            raise run_error
        return {"run": {"status": run_status}} if run_status else {"run": {}}

    request.calls = calls
    return request


def jobs_reply(**fields):
    return {"jobs": [{"id": JOB, **fields}]}


class RerunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.state = Path(self._td.name)
        patcher = mock.patch.dict(os.environ, {"COPILOT_HOOKS_STATE_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def rerun(self, request, run: str = RUN) -> tuple:
        args = SimpleNamespace(job_id=JOB, run=run, base_url="http://127.0.0.1:8465")
        out = io.StringIO()
        with mock.patch.object(lite_rerun.launcher, "_request", request), contextlib.redirect_stdout(out):
            code = lite_rerun.cmd_rerun(args)
        return code, out.getvalue()

    def posts(self, request) -> list:
        return [c for c in request.calls if c[0] == "POST"]

    def ledger(self) -> dict:
        return json.loads((self.state / lite_rerun.LEDGER_FILENAME).read_text(encoding="utf-8"))["runs"]

    def test_a_rerun_safe_job_is_rerun_once(self) -> None:
        request = fake_launcher(jobs=jobs_reply(rerun_safe=True))
        code, out = self.rerun(request)
        self.assertEqual(code, 0)
        self.assertIn(f"RERUN=started job={JOB} failed_run={RUN} rerun=20260101-000100-bbbb", out)
        self.assertEqual(self.posts(request), [("POST", f"/api/jobs/{JOB}/run")])
        self.assertEqual(self.ledger()[f"{JOB}/{RUN}"]["rerun_run_id"], "20260101-000100-bbbb")

    def test_a_second_call_for_the_same_failure_is_refused(self) -> None:
        self.rerun(fake_launcher(jobs=jobs_reply(rerun_safe=True)))
        again = fake_launcher(jobs=jobs_reply(rerun_safe=True))
        code, out = self.rerun(again)
        self.assertEqual(code, 2)
        self.assertIn(f"RERUN_REFUSED={JOB} reason=already-rerun", out)
        self.assertEqual(self.posts(again), [])

    def test_the_failure_of_a_rerun_is_not_rerun_again(self) -> None:
        self.rerun(fake_launcher(jobs=jobs_reply(rerun_safe=True)))
        loop = fake_launcher(jobs=jobs_reply(rerun_safe=True))
        code, out = self.rerun(loop, run="20260101-000100-bbbb")
        self.assertEqual(code, 2)
        self.assertIn("reason=is-a-rerun", out)
        self.assertEqual(self.posts(loop), [])

    def test_a_job_without_the_flag_is_not_rerun(self) -> None:
        for fields, reason in (
            ({}, "rerun-safe-absent"),
            ({"rerun_safe": False}, "not-rerun-safe"),
            ({"rerun_safe": "true"}, "rerun-safe-not-boolean"),
            ({"rerun_safe": 1}, "rerun-safe-not-boolean"),
        ):
            with self.subTest(fields=fields):
                request = fake_launcher(jobs=jobs_reply(**fields))
                code, out = self.rerun(request)
                self.assertEqual(code, 2)
                self.assertIn(f"RERUN_REFUSED={JOB} reason={reason}", out)
                self.assertEqual(self.posts(request), [])
        self.assertFalse((self.state / lite_rerun.LEDGER_FILENAME).exists())

    def test_an_unknown_job_is_refused(self) -> None:
        request = fake_launcher(jobs={"jobs": [{"id": "other", "rerun_safe": True}]})
        code, out = self.rerun(request)
        self.assertEqual(code, 2)
        self.assertIn("reason=job-not-found", out)

    def test_an_unreadable_jobs_list_is_unknown_not_safe(self) -> None:
        for jobs in (urllib.error.URLError("down"), {"error": "x"}):
            with self.subTest(jobs=jobs):
                request = fake_launcher(jobs=jobs)
                code, out = self.rerun(request)
                self.assertEqual(code, 1)
                self.assertIn("RERUN=unknown", out)
                self.assertEqual(self.posts(request), [])

    def test_an_unreadable_run_record_is_unknown_not_a_failure(self) -> None:
        for kwargs in ({"run_error": urllib.error.URLError("down")}, {"run_status": None}):
            with self.subTest(kwargs=kwargs):
                request = fake_launcher(jobs=jobs_reply(rerun_safe=True), **kwargs)
                code, out = self.rerun(request)
                self.assertEqual(code, 1)
                self.assertIn("RERUN=unknown", out)
                self.assertIn("run-record-unreadable", out)
                self.assertEqual(self.posts(request), [])

    def test_a_run_that_did_not_fail_is_not_rerun(self) -> None:
        request = fake_launcher(jobs=jobs_reply(rerun_safe=True), run_status="success")
        code, out = self.rerun(request)
        self.assertEqual(code, 2)
        self.assertIn("reason=run-not-failed status=success", out)
        self.assertEqual(self.posts(request), [])

    def test_an_unanswered_post_spends_the_rerun(self) -> None:
        lost = fake_launcher(jobs=jobs_reply(rerun_safe=True), post_error=TimeoutError("slow"))
        code, out = self.rerun(lost)
        self.assertEqual(code, 1)
        self.assertIn("RERUN=unconfirmed", out)
        retry = fake_launcher(jobs=jobs_reply(rerun_safe=True))
        code, out = self.rerun(retry)
        self.assertEqual(code, 2)
        self.assertIn("reason=already-rerun rerun=in-flight", out)
        self.assertEqual(self.posts(retry), [])

    def test_a_reply_without_a_run_id_is_unconfirmed(self) -> None:
        code, out = self.rerun(fake_launcher(jobs=jobs_reply(rerun_safe=True), new_run=None))
        self.assertEqual(code, 1)
        self.assertIn("RERUN=unconfirmed", out)
        self.assertIn("reason=no-run-id", out)

    def test_an_unreadable_ledger_is_unknown_not_a_fresh_start(self) -> None:
        (self.state / lite_rerun.LEDGER_FILENAME).write_text("{not json", encoding="utf-8")
        request = fake_launcher(jobs=jobs_reply(rerun_safe=True))
        code, out = self.rerun(request)
        self.assertEqual(code, 1)
        self.assertIn("RERUN=unknown", out)
        self.assertEqual(request.calls, [])


if __name__ == "__main__":
    unittest.main()
