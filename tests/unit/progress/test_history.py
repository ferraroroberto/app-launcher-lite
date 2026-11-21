"""GET /api/progress: bucketing, cache-first reads and each source failing on its own."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

import pytest

from src.clients import gitlab as gitlab_client
from src.progress import buckets, history, totals

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-11-04T10:00:00Z"


def _source(days: Dict[str, Dict[str, int]], *, cursor: Optional[str] = STAMP, error: Optional[str] = None,
            skipped: Optional[str] = None, truncated=()) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"days": days, "cursor": cursor, "read_at": cursor, "error": error,
                             "truncated": list(truncated)}
    if skipped:
        entry["skipped"] = skipped
    return entry


def _write(tmp_path, monkeypatch, repos: Dict[str, Dict[str, Any]], **extra: Any):
    store = {
        "version": 1, "git_refreshed_at": STAMP, "gitlab_refreshed_at": STAMP, "error": None,
        "repos": repos, "skipped": [], **extra,
    }
    target = tmp_path / "t.json"
    target.write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setattr(totals, "TOTALS_FILE", target)


def _read(bucket="day", first="2026-11-01", last="2026-11-04", refresh=False):
    return history.read(
        bucket=bucket, first=date.fromisoformat(first), last=date.fromisoformat(last),
        projects_dir="projects", now=NOW, refresh=refresh,
    )


@pytest.fixture(autouse=True)
def no_forge(monkeypatch):
    """A read of the cache must never reach git or GitLab."""
    def refused(*_a, **_k):
        raise AssertionError("a read must not run git or glab")

    monkeypatch.setattr(totals, "read_git", refused)
    monkeypatch.setattr(gitlab_client, "project_history", refused)
    monkeypatch.setattr(totals, "start_refresh", refused)


# ------------------------------------------------------------------ range and buckets


def test_parse_range_defaults_and_refuses_a_bad_range():
    first, last = buckets.parse_range(None, None, today=date(2026, 11, 4))
    assert (first, last) == (date(2026, 10, 6), date(2026, 11, 4))
    with pytest.raises(ValueError):
        buckets.parse_range("2026-11-05", "2026-11-04")
    with pytest.raises(ValueError):
        buckets.parse_range("2024-01-01", "2026-11-04")
    with pytest.raises(ValueError):
        buckets.parse_range("nope", None)


def test_bucket_plan_cuts_the_first_and_last_bucket_to_the_range():
    # Sat 31 Oct .. Tue 3 Nov: the week of the 26th (Mon) holds the 31st and 1st; the 2nd starts a new one.
    plan = buckets.bucket_plan(date(2026, 10, 31), date(2026, 11, 3), "week")
    assert [(b["start"], b["end"], b["days"]) for b in plan] == [
        ("2026-10-26", "2026-11-01", ["2026-10-31", "2026-11-01"]),
        ("2026-11-02", "2026-11-08", ["2026-11-02", "2026-11-03"]),
    ]
    months = buckets.bucket_plan(date(2026, 10, 31), date(2026, 11, 3), "month")
    assert [(b["start"], b["end"], len(b["days"])) for b in months] == [
        ("2026-10-01", "2026-10-31", 1), ("2026-11-01", "2026-11-30", 3),
    ]
    assert len(buckets.bucket_plan(date(2026, 10, 31), date(2026, 11, 3), "day")) == 4


def test_day_week_and_month_buckets_sum_the_local_days_across_a_boundary(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"alpha": {
        "git": _source({
            "2026-10-31": {"lines_added": 1, "lines_removed": 1},
            "2026-11-01": {"lines_added": 10, "lines_removed": 0},
            "2026-11-02": {"lines_added": 100, "lines_removed": 5},
        }),
        "gitlab": _source({
            "2026-10-31": {"mrs_merged": 1},
            "2026-11-02": {"mrs_merged": 2, "issues_opened": 1},
        }),
    }})
    day = _read("day", "2026-10-31", "2026-11-02")["buckets"]
    assert [b["git"]["lines_added"] for b in day] == [1, 10, 100]
    assert [b["gitlab"]["mrs_merged"] for b in day] == [1, 0, 2]  # a read day with nothing is a real zero

    week = _read("week", "2026-10-31", "2026-11-02")["buckets"]
    assert [(b["start"], b["git"]["lines_added"], b["gitlab"]["mrs_merged"]) for b in week] == [
        ("2026-10-26", 11, 1), ("2026-11-02", 100, 2),
    ]
    month = _read("month", "2026-10-31", "2026-11-02")["buckets"]
    assert [(b["start"], b["git"]["lines_added"], b["gitlab"]["issues_opened"]) for b in month] == [
        ("2026-10-01", 1, 0), ("2026-11-01", 110, 1),
    ]


def test_a_range_cut_sums_only_the_days_inside_it(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"alpha": {
        "git": _source({"2026-10-20": {"lines_added": 500}, "2026-11-02": {"lines_added": 3}}),
        "gitlab": _source({}),
    }})
    out = _read("month", "2026-11-01", "2026-11-04")
    assert out["buckets"][0]["git"]["lines_added"] == 3
    assert out["repos"][0]["lines_added"] == 3


# ------------------------------------------------------------------ honest states


def test_nothing_read_yet_is_unknown_never_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(totals, "TOTALS_FILE", tmp_path / "never.json")
    out = _read()
    assert out["git"]["state"] == "unknown" and out["gitlab"]["state"] == "unknown"
    assert out["git"]["age_seconds"] is None
    assert all(b["git"] is None and b["gitlab"] is None for b in out["buckets"])
    assert out["repos"] == [] and out["refreshing"] is False


def test_each_source_has_its_own_state_and_age(tmp_path, monkeypatch):
    _write(
        tmp_path, monkeypatch,
        {"alpha": {
            "git": _source({"2026-11-03": {"lines_added": 4}}),
            "gitlab": _source({"2026-11-03": {"mrs_merged": 1}}, error="GlabError: 401", cursor="2026-11-01T10:00:00Z"),
        }},
        git_refreshed_at="2026-11-04T11:00:00Z", gitlab_refreshed_at="2026-11-01T10:00:00Z",
    )
    out = _read()
    assert out["git"]["state"] == "ok" and out["git"]["age_seconds"] == 3600.0
    assert out["gitlab"]["state"] == "stale" and out["gitlab"]["age_seconds"] == 3 * 86400 + 2 * 3600
    # A stale source still draws what it kept.
    assert out["buckets"][-2]["gitlab"]["mrs_merged"] == 1


def test_a_project_without_a_gitlab_remote_is_skipped_and_its_cells_are_none(tmp_path, monkeypatch):
    _write(
        tmp_path, monkeypatch,
        {
            "alpha": {"git": _source({"2026-11-03": {"lines_added": 4}}), "gitlab": _source({"2026-11-03": {"mrs_merged": 2}})},
            "local-only": {"git": _source({"2026-11-03": {"lines_added": 9}}), "gitlab": _source({}, cursor=None, skipped="no GitLab remote")},
        },
        skipped=[{"repo": "local-only", "reason": "no GitLab remote"}],
    )
    out = _read()
    rows = {r["repo"]: r for r in out["repos"]}
    assert rows["local-only"]["gitlab_state"] == "skipped"
    assert rows["local-only"]["mrs_merged"] is None and rows["local-only"]["issues_closed"] is None
    assert rows["local-only"]["lines_added"] == 9  # its lines still count
    assert rows["alpha"]["mrs_merged"] == 2
    assert out["gitlab"]["skipped"] == [{"repo": "local-only", "reason": "no GitLab remote"}]
    # Its lines are in the total; it adds nothing to the MR total.
    assert out["buckets"][-2]["git"]["lines_added"] == 13
    assert out["buckets"][-2]["gitlab"]["mrs_merged"] == 2


def test_every_project_skipped_says_so_and_no_bucket_is_zero(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"a": {
        "git": _source({"2026-11-03": {"lines_added": 1}}), "gitlab": _source({}, cursor=None, skipped="no GitLab remote"),
    }})
    out = _read()
    assert out["gitlab"]["state"] == "unknown" and "GitLab remote" in out["gitlab"]["reason"]
    assert all(b["gitlab"] is None for b in out["buckets"])
    assert out["git"]["state"] == "ok"


def test_a_project_never_read_is_unknown_in_its_row_and_listed(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {
        "alpha": {"git": _source({"2026-11-03": {"lines_added": 4}}), "gitlab": _source({})},
        "fresh": {"git": _source({}, cursor=None), "gitlab": _source({}, cursor=None)},
    })
    out = _read()
    fresh = {r["repo"]: r for r in out["repos"]}["fresh"]
    assert fresh["git_state"] == "unknown" and fresh["lines_added"] is None and fresh["mrs_merged"] is None
    assert out["git"]["unknown_repos"] == ["fresh"] and out["gitlab"]["unknown_repos"] == ["fresh"]


def test_a_full_page_is_reported_per_project(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {
        "alpha": {"git": _source({}), "gitlab": _source({}, truncated=["merged"])},
        "beta": {"git": _source({}), "gitlab": _source({})},
    })
    assert _read()["gitlab"]["truncated"] == ["alpha"]


def test_an_unreadable_store_answers_unavailable_not_zero(tmp_path, monkeypatch):
    bad = tmp_path / "t.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(totals, "TOTALS_FILE", bad)
    out = _read()
    assert out["git"]["state"] == "unavailable" and "could not be read" in out["git"]["reason"]
    assert all(b["git"] is None for b in out["buckets"])


def test_refresh_starts_a_background_refresh_and_still_answers_from_the_cache(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"alpha": {"git": _source({"2026-11-03": {"lines_added": 4}}), "gitlab": _source({})}})
    started = []
    monkeypatch.setattr(totals, "start_refresh", lambda *a, **k: started.append(a) or True)
    out = _read(refresh=True)
    assert started == [("projects", (), "")]
    assert out["refreshing"] is True
    assert out["buckets"][-2]["git"]["lines_added"] == 4
    assert _read(refresh=False)["refreshing"] is False
