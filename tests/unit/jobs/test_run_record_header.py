"""Every run-record writer builds its header through ``base_run_meta``."""

from __future__ import annotations

import ast
from pathlib import Path

from src import jobs_history
from src.jobs_config import Job

REPO_ROOT = Path(__file__).resolve().parents[3]
# A header field set no writer may spell out by hand: the helper owns it.
_HEADER_KEYWORDS = {"script_path", "started_at"}


def _job() -> Job:
    return Job(id="demo", name="Demo", script_path="C:\\demo.bat", args="--x")


def test_base_run_meta_carries_the_header_fields():
    meta = jobs_history.base_run_meta(
        _job(), "manual", "2026-10-24T08:00:00", "20261024T080000"
    )

    assert meta == {
        "run_id": "20261024T080000",
        "job_id": "demo",
        "name": "Demo",
        "trigger": "manual",
        "script_path": "C:\\demo.bat",
        "args": "--x",
        "started_at": "2026-10-24T08:00:00",
    }


def test_mark_run_failed_finalises_with_no_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_history, "JOBS_RUNS_DIR", tmp_path)
    run_dir = jobs_history.new_run_dir("demo", "20261024T080000")
    jobs_history.write_run_json(run_dir, run_id=run_dir.name, status="pending")

    jobs_history.mark_run_failed(run_dir, note="boom")

    record = jobs_history.read_run(run_dir)
    assert record["status"] == "failed"
    assert record["exit_code"] == -1
    assert record["note"] == "boom"
    assert record["finished_at"]


def test_mark_run_failed_keeps_a_caller_supplied_finish_time(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_history, "JOBS_RUNS_DIR", tmp_path)
    run_dir = jobs_history.new_run_dir("demo", "20261024T080000")

    jobs_history.mark_run_failed(run_dir, finished_at="2026-10-24T08:00:00")

    assert jobs_history.read_run(run_dir)["finished_at"] == "2026-10-24T08:00:00"


def test_no_writer_spells_the_run_header_by_hand():
    """A new header field is added in ``base_run_meta`` only; a call that
    passes both header keywords inline is a hand-built copy of it."""
    owner = REPO_ROOT / "src" / "jobs_history.py"
    offenders = []
    for root in ("src", "app"):
        for path in (REPO_ROOT / root).rglob("*.py"):
            if path == owner:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                names = {kw.arg for kw in node.keywords}
                if _HEADER_KEYWORDS <= names:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert not offenders, f"build the header with base_run_meta: {offenders}"
