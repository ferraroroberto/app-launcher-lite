"""Board backend: jobs_attention."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from src import board
from tests.unit.board._support import _seed_job


def test_jobs_attention_failed_today(webapp_client):
    _client, _app, overrides = webapp_client
    # run_job_cmd writes naive local ISO timestamps — mirror that exactly.
    # Anchored at local midday rather than the bare datetime.now() (#323): the
    # 1h/50min offsets below must never straddle a local-midnight calendar-day
    # boundary, which a bare `now()` does whenever the test happens to run in
    # the ~50 minutes after midnight. `now=local_now` is also passed explicitly
    # to jobs_attention() so the assertion is fully clock-independent, matching
    # how the rest of this file injects `now` into board functions.
    local_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_job(overrides, "pipeline", {
        "status": "failed",
        "started_at": (local_now - timedelta(hours=1)).isoformat(timespec="seconds"),
        "finished_at": (local_now - timedelta(minutes=50)).isoformat(timespec="seconds"),
        "exit_code": 1,
    })
    cards = board.jobs_attention(now=local_now)
    assert [(c["job_id"], c["state"]) for c in cards] == [("pipeline", "failed")]


def test_jobs_attention_ignores_yesterdays_failure(webapp_client):
    _client, _app, overrides = webapp_client
    local_now = datetime.now()
    _seed_job(overrides, "old_fail", {
        "status": "failed",
        "finished_at": (local_now - timedelta(days=2)).isoformat(timespec="seconds"),
        "exit_code": 1,
    })
    assert board.jobs_attention() == []


def test_jobs_attention_unconfirmed_run_is_not_a_failed_card(webapp_client):
    """Issue #916: exit 122 is the scheduled-run adapter saying "this may well
    have delivered, but nobody established that". The card stays — an
    unverified run still wants a human's eye — but it must not be drawn as a
    failure, because a Board that cries failure on healthy runs is a Board
    whose failures stop being read."""
    _client, _app, overrides = webapp_client
    local_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_job(overrides, "truncated", {
        "status": "failed",
        "started_at": (local_now - timedelta(hours=1)).isoformat(timespec="seconds"),
        "finished_at": (local_now - timedelta(minutes=50)).isoformat(timespec="seconds"),
        "exit_code": 122,
    }, script_path="C:/fleet/skills/_lib/claude_progress.py")
    cards = board.jobs_attention(now=local_now)
    assert [(c["job_id"], c["state"]) for c in cards] == [
        ("truncated", "unconfirmed")
    ]
    # And it says why, so the card is actionable without a log dive.
    assert "never verified" in cards[0]["error"]


def test_jobs_attention_genuine_failure_still_reads_as_failed(webapp_client):
    """The other half of #916, and the one that makes the fix worth having:
    exit 123 is the run reporting it delivered no work. It must keep the
    failure rendering it had, or the false alarms are simply replaced by
    false comfort."""
    _client, _app, overrides = webapp_client
    local_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_job(overrides, "delivered-nothing", {
        "status": "failed",
        "started_at": (local_now - timedelta(hours=1)).isoformat(timespec="seconds"),
        "finished_at": (local_now - timedelta(minutes=50)).isoformat(timespec="seconds"),
        "exit_code": 123,
    }, script_path="C:/fleet/skills/_lib/claude_progress.py")
    cards = board.jobs_attention(now=local_now)
    assert [(c["job_id"], c["state"]) for c in cards] == [
        ("delivered-nothing", "failed")
    ]
    assert "delivered no work" in cards[0]["error"]


def test_jobs_attention_stuck_run(webapp_client):
    _client, _app, overrides = webapp_client
    local_now = datetime.now()
    # One lone running run, 30 min old: no p95 history, so the stuck floor
    # (300 s) applies and it counts as stuck.
    _seed_job(overrides, "wedged", {
        "status": "running",
        "started_at": (local_now - timedelta(minutes=30)).isoformat(timespec="seconds"),
    })
    cards = board.jobs_attention()
    assert [(c["job_id"], c["state"]) for c in cards] == [("wedged", "stuck")]


def test_jobs_attention_unreadable_history_surfaces_not_skipped(
    webapp_client, monkeypatch, caplog
):
    """#915: a job whose run history can't be read raises an ``unreadable``
    card instead of dropping out of the check, and says so above debug —
    once per job, not once per 5 s poll. A readable quiet job beside it
    still raises nothing, and a recovery clears the card."""
    from src import jobs as jobs_mod

    _client, _app, overrides = webapp_client
    local_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    finished = (local_now - timedelta(minutes=50)).isoformat(timespec="seconds")
    _seed_job(overrides, "quiet", {"status": "success", "finished_at": finished})
    _seed_job(overrides, "locked", {"status": "success", "finished_at": finished})

    real_latest_run = jobs_mod.latest_run

    def _locked_latest_run(job_id):
        if job_id == "locked":
            raise PermissionError(13, "Access is denied", "run.json")
        return real_latest_run(job_id)

    monkeypatch.setattr(jobs_mod, "latest_run", _locked_latest_run)
    with caplog.at_level(logging.DEBUG, logger="src.board"):
        cards = board.jobs_attention(now=local_now)
        board.jobs_attention(now=local_now)  # the next poll must not re-warn

    assert [(c["job_id"], c["state"]) for c in cards] == [("locked", "unreadable")]
    assert "Access is denied" in cards[0]["error"]
    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "locked" in r.getMessage()
    ]
    assert len(warnings) == 1

    monkeypatch.setattr(jobs_mod, "latest_run", real_latest_run)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.board"):
        assert board.jobs_attention(now=local_now) == []
    assert any("readable again" in r.getMessage() for r in caplog.records)

