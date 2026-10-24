"""Task Scheduler task ownership: a job's bare task plus its ``-<digits>``
``daily_times`` slots, never a sibling job whose id merely extends it."""

from __future__ import annotations

import subprocess
from typing import List

from src import jobs as jobs_mod
from src import jobs_schtasks
from src.jobs_config import make_job_id

_FOLDER = "\\AppLauncher\\"


def _completed(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


def _csv(*names: str) -> str:
    return "".join(f'"{_FOLDER}{n}","ready","..."\n' for n in names)


def test_delete_leaves_sibling_job_whose_id_extends_ours():
    # Job ``backup`` and job ``backup-2`` both live in Task Scheduler;
    # editing ``backup`` must not delete ``backup-2``'s entry.
    query_stdout = _csv("backup", "backup-2", "backup-nightly")
    deletes: List[str] = []

    def runner(argv):
        if argv[:2] == ["schtasks", "/Query"]:
            return _completed(stdout=query_stdout)
        deletes.append(argv[4])
        return _completed()

    result = jobs_mod.delete_schtasks(
        "backup", runner=runner, other_job_ids=["backup-2", "backup-nightly"]
    )
    assert result == [_FOLDER + "backup"]
    assert deletes == [_FOLDER + "backup"]


def test_slot_suffix_is_digits_only():
    query_stdout = _csv("backup-1", "backup-nightly")

    def runner(argv):
        if argv[:2] == ["schtasks", "/Query"]:
            return _completed(stdout=query_stdout)
        return _completed()

    result = jobs_mod.delete_schtasks("backup", runner=runner, other_job_ids=[])
    assert result == [_FOLDER + "backup-1"]


def test_query_next_run_ignores_sibling_job_task():
    bulk = (
        f"TaskName: {_FOLDER}backup\nNext Run Time: 2026-06-02 06:00:00\n\n"
        f"TaskName: {_FOLDER}backup-2\nNext Run Time: 2026-06-01 06:00:00\n\n"
    )
    jobs_mod.invalidate_next_run_cache()
    try:
        got = jobs_mod.query_next_run(
            "backup", runner=lambda argv: _completed(stdout=bulk),
            other_job_ids=["backup-2"],
        )
    finally:
        jobs_mod.invalidate_next_run_cache()
    assert got == "2026-06-02 06:00:00"


def test_make_job_id_collision_suffix_is_not_a_slot_suffix():
    job_id = make_job_id("Backup", ["backup"])
    assert not jobs_schtasks._SLOT_SUFFIX_RE.fullmatch(job_id[len("backup"):])
