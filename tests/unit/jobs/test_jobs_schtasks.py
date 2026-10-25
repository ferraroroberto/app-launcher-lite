"""Jobs tab plumbing — schtasks argv mapping, detached spawn and sync (issue #47). Every schtasks call goes through a mocked runner."""

from __future__ import annotations

import json
import re
import subprocess
from typing import List
from unittest.mock import MagicMock

from src import jobs as jobs_mod
from src.jobs import schtasks as jobs_schtasks_mod
from src.jobs.argv import decode_params_token, encode_params_token
from src.jobs.config import (
    Job,
    Schedule,
)


def _mk_completed(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


# ================================================================= schtasks


class TestScheduleArgvParts:
    def test_none_empty(self):
        assert jobs_mod.schedule_argv_parts(Schedule(type="none")) == []

    def test_daily(self):
        parts = jobs_mod.schedule_argv_parts(Schedule(type="daily", at="06:00"))
        assert parts == [["/SC", "DAILY", "/ST", "06:00"]]

    def test_daily_times_fans_out(self):
        parts = jobs_mod.schedule_argv_parts(
            Schedule(type="daily_times", at=["06:00", "12:00", "18:00"])
        )
        assert parts == [
            ["/SC", "DAILY", "/ST", "06:00"],
            ["/SC", "DAILY", "/ST", "12:00"],
            ["/SC", "DAILY", "/ST", "18:00"],
        ]

    def test_minutes(self):
        parts = jobs_mod.schedule_argv_parts(Schedule(type="minutes", every=5))
        assert parts == [["/SC", "MINUTE", "/MO", "5"]]

    def test_hourly(self):
        parts = jobs_mod.schedule_argv_parts(Schedule(type="hourly", every=6))
        assert parts == [["/SC", "HOURLY", "/MO", "6"]]

    def test_weekly(self):
        parts = jobs_mod.schedule_argv_parts(
            Schedule(type="weekly", day="FRI", at="06:00")
        )
        assert parts == [["/SC", "WEEKLY", "/D", "FRI", "/ST", "06:00"]]


class TestTaskNamesFor:
    def test_bare_for_daily(self):
        job = Job(
            id="reporting",
            name="Reporting",
            script_path="C:\\x.bat",
            schedule=Schedule(type="daily", at="06:00"),
        )
        assert jobs_mod.task_names_for(job) == ["\\AppLauncher\\reporting"]

    def test_suffixed_for_daily_times(self):
        job = Job(
            id="linkedin-scrape",
            name="LinkedIn",
            script_path="C:\\x.py",
            schedule=Schedule(type="daily_times", at=["06:00", "12:00", "18:00"]),
        )
        assert jobs_mod.task_names_for(job) == [
            "\\AppLauncher\\linkedin-scrape-1",
            "\\AppLauncher\\linkedin-scrape-2",
            "\\AppLauncher\\linkedin-scrape-3",
        ]


class TestSpawnRunJobDetached:
    """issue #416: DETACHED_PROCESS alone does not escape ``taskkill /T`` —
    the spawn must re-parent via ``cmd /c start`` like the session-host does.
    """

    def test_wraps_argv_in_cmd_start_and_drops_detached_process_flag(
        self, monkeypatch
    ):
        captured = {}

        class _FakeProc:
            pid = 4242

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeProc()

        monkeypatch.setattr(jobs_schtasks_mod.subprocess, "Popen", fake_popen)

        pid = jobs_schtasks_mod.spawn_run_job_detached("demo", "run-1", "manual")

        assert pid == 4242
        argv = captured["argv"]
        assert argv[:5] == ["cmd", "/c", "start", "", "/b"]
        # The real launcher invocation follows the cmd/start wrapper untouched.
        assert "run-job" in argv
        assert "demo" in argv
        assert "run-1" in argv
        # DETACHED_PROCESS must NOT be set — it doesn't escape taskkill /T
        # (verified empirically, see app/tray/tray.py::_start_session_host).
        detached_flag = getattr(subprocess, "DETACHED_PROCESS", 0)
        creationflags = captured["kwargs"].get("creationflags", 0)
        if detached_flag:
            assert not (creationflags & detached_flag)

    def test_params_and_dry_run_still_appended_before_the_wrapper(
        self, monkeypatch
    ):
        captured = {}

        class _FakeProc:
            pid = 1

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return _FakeProc()

        monkeypatch.setattr(jobs_schtasks_mod.subprocess, "Popen", fake_popen)

        jobs_schtasks_mod.spawn_run_job_detached(
            "demo", "run-2", "manual", params={"x": 1}, dry_run=True
        )

        argv = captured["argv"]
        assert argv[:5] == ["cmd", "/c", "start", "", "/b"]
        token = argv[argv.index("--params-b64") + 1]
        assert json.loads(decode_params_token(token)) == {"x": 1}
        assert "--dry-run" in argv

    def test_spawn_argv_after_the_interpreter_is_plain_tokens(self, monkeypatch):
        """cmd.exe re-reads the spawn's command line, so every token after
        the interpreter and script paths must come through it unchanged:
        letters, digits and ``-_.:=`` only, whatever the param values hold."""
        captured = {}

        class _FakeProc:
            pid = 1

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return _FakeProc()

        monkeypatch.setattr(jobs_schtasks_mod.subprocess, "Popen", fake_popen)
        values = {"msg": 'a & b | c > d < e ^ f % g ! h " i ñ€'}

        jobs_schtasks_mod.spawn_run_job_detached(
            "demo", "run-3", "manual", params=values
        )

        tail = captured["argv"][captured["argv"].index("run-job"):]
        for token in tail:
            assert re.fullmatch(r"[A-Za-z0-9._:=\-]+", token), token
        decoded = decode_params_token(tail[tail.index("--params-b64") + 1])
        assert json.loads(decoded) == values

    def test_executor_parser_reads_the_encoded_params(self):
        from app.cli.main import _build_parser

        values = {"msg": 'x & y "z"'}
        args = _build_parser().parse_args([
            "run-job", "demo", "--run-id", "r1", "--trigger", "manual",
            "--params-b64", encode_params_token(values),
        ])
        assert json.loads(decode_params_token(args.params_b64)) == values


class TestSyncSchtasks:
    def test_daily_creates_one_task(self):
        job = Job(
            id="demo",
            name="Demo",
            script_path="C:\\stub\\demo.py",
            schedule=Schedule(type="daily", at="06:00"),
        )
        # Runner: list_known_tasks (Query) returns empty; deletes blind;
        # creates succeed.
        calls: List[List[str]] = []

        def runner(argv):
            calls.append(argv)
            # First call from list_known_tasks: empty stdout → no known tasks.
            if argv[:2] == ["schtasks", "/Query"]:
                return _mk_completed(stdout="", rc=0)
            return _mk_completed(rc=0)

        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == ["\\AppLauncher\\demo"]
        # Last call is the /Create for the daily task.
        last = calls[-1]
        assert last[:5] == ["schtasks", "/Create", "/F", "/TN", "\\AppLauncher\\demo"]
        assert "/SC" in last and "DAILY" in last and "06:00" in last

    def test_daily_times_creates_three_tasks(self):
        job = Job(
            id="ls",
            name="LinkedIn",
            script_path="C:\\stub\\scrape.py",
            schedule=Schedule(type="daily_times", at=["06:00", "12:00", "18:00"]),
        )
        runner = MagicMock(return_value=_mk_completed(rc=0))
        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == [
            "\\AppLauncher\\ls-1",
            "\\AppLauncher\\ls-2",
            "\\AppLauncher\\ls-3",
        ]

    def test_none_schedule_only_deletes(self):
        job = Job(
            id="demo",
            name="Demo",
            script_path="C:\\stub\\demo.py",
            schedule=Schedule(type="none"),
        )
        runner = MagicMock(return_value=_mk_completed(rc=0))
        created = jobs_mod.sync_schtasks(job, runner=runner)
        assert created == []
        # No /Create call should have been issued.
        create_calls = [
            c for c in runner.call_args_list
            if c.args[0][:2] == ["schtasks", "/Create"]
        ]
        assert create_calls == []


class TestDeleteSchtasks:
    def test_uses_query_results_when_available(self):
        # Query lists three tasks under \AppLauncher\ — two for our job, one foreign.
        query_stdout = (
            '"\\AppLauncher\\ls-1","ready","..."\n'
            '"\\AppLauncher\\ls-2","ready","..."\n'
            '"\\AppLauncher\\other","ready","..."\n'
        )
        deletes: List[str] = []

        def runner(argv):
            if argv[:2] == ["schtasks", "/Query"]:
                return _mk_completed(stdout=query_stdout, rc=0)
            if argv[:2] == ["schtasks", "/Delete"]:
                deletes.append(argv[4])  # /TN value
                return _mk_completed(rc=0)
            return _mk_completed(rc=0)

        result = jobs_mod.delete_schtasks("ls", runner=runner)
        assert sorted(result) == ["\\AppLauncher\\ls-1", "\\AppLauncher\\ls-2"]
        # The foreign task is left alone.
        assert "\\AppLauncher\\other" not in result


class TestBulkParser:
    """``src.jobs.schtasks._parse_bulk_records`` — the bulk Next Run Time parse."""

    def test_filters_by_prefix(self):
        stdout = (
            "TaskName: \\AppLauncher\\demo\nNext Run Time: 2026-06-01 06:00:00\n\n"
            "TaskName: \\Foreign\\task\nNext Run Time: 2026-06-01 12:00:00\n\n"
        )
        out = jobs_schtasks_mod._parse_bulk_records(stdout)
        assert "\\AppLauncher\\demo" in out
        assert out["\\AppLauncher\\demo"]["next_run"] == "2026-06-01 06:00:00"
        # Foreign task is filtered out.
        assert "\\Foreign\\task" not in out

    def test_n_a_collapses_to_none(self):
        stdout = (
            "TaskName: \\AppLauncher\\demo-1\nNext Run Time: N/A\n\n"
            "TaskName: \\AppLauncher\\demo-2\nNext Run Time: Disabled\n\n"
        )
        out = jobs_schtasks_mod._parse_bulk_records(stdout)
        assert out["\\AppLauncher\\demo-1"]["next_run"] is None
        assert out["\\AppLauncher\\demo-2"]["next_run"] is None
