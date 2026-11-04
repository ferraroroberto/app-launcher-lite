"""The Task Scheduler folder is configuration, and it is this fork's own.

Upstream app-launcher registers ``\\AppLauncher\\<id>``; the same job id in
both registries must not be one shared task.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from src import app_config
from src.jobs import schtasks
from src.jobs.config import Job, Schedule


def _job(job_id: str = "demo") -> Job:
    return Job(
        id=job_id,
        name=job_id,
        script_path="x.py",
        schedule=Schedule(type="daily", at="09:00"),
    )


def test_default_namespace_is_not_upstreams():
    assert app_config.DEFAULT_TASK_NAMESPACE == "AppLauncherLite"
    assert app_config.AppConfig().task_namespace != "AppLauncher"


def test_task_names_follow_the_configured_namespace(monkeypatch):
    monkeypatch.setattr(schtasks, "task_namespace", lambda: "WorkPcJobs")
    assert schtasks.task_names_for(_job()) == ["\\WorkPcJobs\\demo"]


def test_foreign_namespace_tasks_are_not_ours_to_list_or_delete():
    # Upstream's task for the same job id sits next to ours.
    stdout = '"\\AppLauncher\\demo","ready"\n"\\AppLauncherLite\\demo","ready"\n'
    calls = []

    def runner(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    assert schtasks.list_known_tasks(runner=runner) == ["\\AppLauncherLite\\demo"]
    deleted = schtasks.delete_schtasks("demo", runner=runner, other_job_ids=[])
    assert deleted == ["\\AppLauncherLite\\demo"]
    assert ["schtasks", "/Delete", "/F", "/TN", "\\AppLauncher\\demo"] not in calls


def test_bulk_records_ignore_other_namespaces():
    records = schtasks._parse_bulk_records(
        "TaskName: \\AppLauncher\\demo\nStatus: Ready\n\n"
        "TaskName: \\AppLauncherLite\\demo\nStatus: Ready\n\n"
    )
    assert list(records) == ["\\AppLauncherLite\\demo"]


def test_config_json_sets_the_namespace(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"task_namespace": "WorkPcJobs"}), encoding="utf-8")
    assert app_config.load_app_config(path).task_namespace == "WorkPcJobs"


def test_missing_key_falls_back_to_default(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    cfg = app_config.load_app_config(path)
    assert cfg.task_namespace == app_config.DEFAULT_TASK_NAMESPACE


@pytest.mark.parametrize("bad", ["Work\\Pc", "a b", '"x"', "..\\x", 7])
def test_unsafe_namespace_is_rejected(tmp_path, bad):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"task_namespace": bad}), encoding="utf-8")
    with pytest.raises(ValueError, match="task_namespace"):
        app_config.load_app_config(path)
