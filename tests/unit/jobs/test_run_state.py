"""The Jobs run-lifecycle machine: one derive function, one table of record shapes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from src.jobs import run_state as rs
from src.jobs.run_state import FailReason, RunState

NOW = datetime(2026, 11, 10, 12, 0, 0)
FRESH = (NOW - timedelta(seconds=5)).isoformat(timespec="seconds")
STALE = (NOW - timedelta(seconds=rs.SPAWN_DEADLINE_SECONDS + 60)).isoformat(
    timespec="seconds"
)


# (record, pid_alive, expected state, expected reason)
ROWS = [
    # reachable shapes
    ({"status": "queued", "started_at": STALE}, None, RunState.QUEUED, None),
    ({"status": "pending", "started_at": FRESH}, None, RunState.PENDING, None),
    ({"status": "running", "started_at": FRESH}, None, RunState.RUNNING, None),
    ({"status": "running", "started_at": STALE, "pid": 7}, True, RunState.RUNNING, None),
    ({"status": "running", "started_at": STALE, "pid": 7}, None, RunState.RUNNING, None),
    ({"status": "success", "exit_code": 0}, None, RunState.SUCCEEDED, None),
    ({"status": "success", "dry_run": True}, None, RunState.SUCCEEDED, None),
    ({"status": "failed", "exit_code": 2}, None, RunState.FAILED, FailReason.EXIT_CODE),
    ({"status": "failed", "exit_code": -1}, None, RunState.FAILED, FailReason.SPAWN_ERROR),
    (
        {"status": "failed", "exit_code": -9, "watchdog": True},
        None,
        RunState.FAILED,
        FailReason.WATCHDOG,
    ),
    ({"status": "failed", "exit_code": -9, "killed": True}, None, RunState.KILLED, None),
    (
        {"status": "failed", "reaped": True},
        None,
        RunState.REAPED,
        FailReason.EXECUTOR_LOST,
    ),
    (
        {"status": "failed", "fail_reason": "executor_never_started"},
        None,
        RunState.FAILED,
        FailReason.EXECUTOR_NEVER_STARTED,
    ),
    ({"status": "skipped"}, None, RunState.SKIPPED, None),
    ({"status": "dry_run_success"}, None, RunState.DRY_RUN_SUCCEEDED, None),
    ({"status": "dry_run_failed"}, None, RunState.DRY_RUN_FAILED, None),
    # impossible shapes: the record contradicts itself
    ({"status": "success", "killed": True}, None, RunState.KILLED, None),
    ({"status": "running", "killed": True, "pid": 7}, True, RunState.KILLED, None),
    (
        {"status": "running", "pid": 7, "started_at": FRESH},
        False,
        RunState.REAPED,
        FailReason.EXECUTOR_LOST,
    ),
    (
        {"status": "pending", "pid": 7, "started_at": FRESH},
        False,
        RunState.REAPED,
        FailReason.EXECUTOR_LOST,
    ),
    (
        {"status": "pending", "started_at": STALE},
        None,
        RunState.FAILED,
        FailReason.EXECUTOR_NEVER_STARTED,
    ),
    (
        {"status": "running", "started_at": STALE},
        None,
        RunState.FAILED,
        FailReason.EXECUTOR_NEVER_STARTED,
    ),
    # a live record whose age cannot be told stays live, never failed
    ({"status": "pending"}, None, RunState.PENDING, None),
    ({"status": "pending", "started_at": "not-a-date"}, None, RunState.PENDING, None),
    # unknown is its own state, never folded into a negative
    ({"status": "exploded"}, None, RunState.UNKNOWN, None),
    ({"status": None}, None, RunState.UNKNOWN, None),
    ({"status": 7}, None, RunState.UNKNOWN, None),
    ({}, None, RunState.UNKNOWN, None),
]


@pytest.mark.parametrize("record,pid_alive,state,reason", ROWS)
def test_derive_table(record, pid_alive, state, reason):
    got = rs.derive_run_state(record, now=NOW, pid_alive=pid_alive)
    assert got.state is state
    assert got.reason == reason


def test_unknown_is_neither_open_nor_terminal_nor_failure():
    s = RunState.UNKNOWN
    assert not rs.is_open(s)
    assert not rs.is_live(s)
    assert not rs.is_terminal(s)
    assert not rs.is_killable(s)
    assert not rs.is_failure(s)


def test_helper_sets_partition_the_states():
    for s in RunState:
        assert not (rs.is_open(s) and rs.is_terminal(s)), s
    assert {s for s in RunState if rs.is_open(s)} == {
        RunState.QUEUED,
        RunState.PENDING,
        RunState.RUNNING,
    }
    assert {s for s in RunState if rs.is_live(s)} == {
        RunState.PENDING,
        RunState.RUNNING,
    }
    assert {s for s in RunState if rs.is_killable(s)} == {
        s for s in RunState if rs.is_live(s)
    }
    assert {s for s in RunState if rs.is_failure(s)} == {
        RunState.FAILED,
        RunState.KILLED,
        RunState.REAPED,
    }


# state-machine-proof: run-lifecycle
def test_illegal_transition_refused_killed_run_stays_killed(tmp_path):
    """A finalise that tries to turn a killed run back into a success is refused,
    and the derived state is KILLED whatever the record's status string says."""
    from src.jobs.history import read_run, write_run_json

    run_dir = tmp_path / "20260101T000000"
    run_dir.mkdir()
    write_run_json(run_dir, status="failed", exit_code=-9, killed=True)
    write_run_json(run_dir, status="success", exit_code=0)
    record = read_run(run_dir)
    assert record["status"] == "failed"
    assert rs.derive_run_state(record, now=NOW).state is RunState.KILLED
    # and a contradictory on-disk shape still derives as killed
    contradictory = {"status": "success", "killed": True}
    assert rs.derive_run_state(contradictory, now=NOW).state is RunState.KILLED
    assert rs.is_terminal(RunState.KILLED) and not rs.is_open(RunState.KILLED)


# state-machine-proof: run-lifecycle
def test_never_started_run_is_not_live_past_the_deadline():
    """A pending run with no pid is live only until the spawn deadline: past it the
    machine refuses to keep calling it live."""
    rec = {"status": "pending", "started_at": STALE}
    got = rs.derive_run_state(rec, now=NOW)
    assert not rs.is_live(got.state)
    assert not rs.is_killable(got.state)
    assert got.reason is FailReason.EXECUTOR_NEVER_STARTED


def test_run_view_is_the_client_payload():
    view = rs.run_view({"status": "queued"}, now=NOW)
    assert view == {
        "state": "queued",
        "live": False,
        "open": True,
        "fail_reason": None,
    }
    running = rs.run_view({"status": "running", "started_at": FRESH}, now=NOW)
    assert running["live"] is True and running["open"] is True
    dead = rs.run_view({"status": "pending", "started_at": STALE}, now=NOW)
    assert dead["live"] is False
    assert dead["fail_reason"] == "executor_never_started"


def test_run_json_status_strings_are_unchanged(tmp_path):
    """The on-disk contract: the same status strings reach run.json byte-for-byte."""
    from src.jobs.history import write_run_json

    run_dir = tmp_path / "r"
    run_dir.mkdir()
    write_run_json(run_dir, status=rs.STATUS_RUNNING, run_id="r")
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8")) == {
        "status": "running",
        "run_id": "r",
    }
    assert [
        rs.STATUS_QUEUED,
        rs.STATUS_PENDING,
        rs.STATUS_RUNNING,
        rs.STATUS_SUCCESS,
        rs.STATUS_FAILED,
        rs.STATUS_SKIPPED,
        rs.STATUS_DRY_RUN_SUCCESS,
        rs.STATUS_DRY_RUN_FAILED,
    ] == [
        "queued",
        "pending",
        "running",
        "success",
        "failed",
        "skipped",
        "dry_run_success",
        "dry_run_failed",
    ]
