"""The kill route stamps ``killed`` before it kills the process tree."""

from __future__ import annotations

import asyncio

from app.webapp.routers import jobs as jobs_router
from src import jobs as jobs_mod
from src.jobs import history as jobs_history
from src.jobs.config import Job, JobsConfig


def test_marks_killed_before_killing_the_tree(tmp_path, monkeypatch):
    """The executor wakes the instant the child dies; it must already find
    ``killed`` on the record to skip its own finalisation."""
    monkeypatch.setattr(jobs_history, "JOBS_RUNS_DIR", tmp_path)
    job = Job(id="demo", name="Demo", script_path="C:\demo.bat")
    monkeypatch.setattr(jobs_router, "load_jobs", lambda: JobsConfig(jobs=[job]))
    run_dir = jobs_mod.new_run_dir("demo", "20260524T080100")
    jobs_mod.write_run_json(
        run_dir, run_id=run_dir.name, status="running",
        started_at="2026-05-24T08:00:00", pid=4242,
    )
    seen = {}

    def fake_kill(pid, timeout):
        seen["killed"] = jobs_mod.read_run(run_dir).get("killed")
        return [pid]

    monkeypatch.setattr(jobs_router, "kill_process_tree", fake_kill)

    body = asyncio.run(jobs_router.kill_job_run("demo", run_dir.name))

    assert body["signalled"] == [4242]
    assert seen["killed"] is True
