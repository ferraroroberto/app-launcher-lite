"""Done lane's earlier days: the local-day window and GET /api/board/done."""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta, timezone, tzinfo

from src.clients import gitlab as forge

_ZERO = timedelta(0)
_HOUR = timedelta(hours=1)


class _Cet(tzinfo):
    """Central Europe with the EU DST rule (the zone database is not installed
    on Windows): CEST from 01:00 UTC on the last Sunday of March until 01:00
    UTC on the last Sunday of October."""

    @staticmethod
    def _last_sunday(year: int, month: int) -> datetime:
        d = datetime(year, month, 31, 1)
        return d - timedelta(days=(d.weekday() + 1) % 7)

    def _is_dst_utc(self, utc: datetime) -> bool:
        naive = utc.replace(tzinfo=None)
        return self._last_sunday(naive.year, 3) <= naive < self._last_sunday(naive.year, 10)

    def utcoffset(self, dt):
        return 2 * _HOUR if self._is_dst_utc(dt.replace(tzinfo=None) - _HOUR) else _HOUR

    def dst(self, dt):
        return _HOUR if self.utcoffset(dt) == 2 * _HOUR else _ZERO

    def tzname(self, dt):
        return "CEST" if self.dst(dt) else "CET"


def _window(day: date):
    start, end = forge.local_day_window(day, _Cet())
    return start.isoformat(), end.isoformat()


def test_local_day_window_is_the_local_calendar_day_in_utc():
    # Summer: local midnight is 22:00 UTC the evening before.
    assert _window(date(2026, 7, 2)) == (
        "2026-07-01T22:00:00+00:00", "2026-07-02T21:59:59+00:00")
    # Winter: one hour apart from UTC.
    assert _window(date(2026, 12, 2)) == (
        "2026-12-01T23:00:00+00:00", "2026-12-02T22:59:59+00:00")


def test_local_day_window_spans_25_and_23_hours_across_the_dst_changes():
    start, end = forge.local_day_window(date(2026, 10, 25), _Cet())
    assert end - start == timedelta(hours=25) - timedelta(seconds=1)
    assert start.isoformat() == "2026-10-24T22:00:00+00:00"   # still CEST at midnight
    assert end.isoformat() == "2026-10-25T22:59:59+00:00"     # next midnight is CET

    start, end = forge.local_day_window(date(2026, 3, 29), _Cet())
    assert end - start == timedelta(hours=23) - timedelta(seconds=1)
    assert start.isoformat() == "2026-03-28T23:00:00+00:00"
    assert end.isoformat() == "2026-03-29T21:59:59+00:00"


def test_consecutive_local_days_leave_no_gap_and_no_overlap():
    for first in (date(2026, 10, 24), date(2026, 10, 25), date(2026, 3, 28), date(2026, 3, 29)):
        _, end = forge.local_day_window(first, _Cet())
        next_start, _ = forge.local_day_window(first + timedelta(days=1), _Cet())
        assert next_start - end == timedelta(seconds=1), first


def test_search_done_on_asks_glab_from_the_days_start(monkeypatch):
    paths = []

    def fake_run(argv, **kwargs):
        paths.append(argv[argv.index("api") + 1])
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(forge.subprocess, "run", fake_run)
    forge.search_done_on("owner", date(2026, 10, 25), _Cet())
    since = "updated_after=2026-10-24T22%3A00%3A00Z"
    issues = [p for p in paths if "/issues?" in p]
    merged = [p for p in paths if "/merge_requests?" in p]
    assert issues and since in issues[0] and "state=closed" in issues[0]
    # The MR read that links each issue to its proof report starts at the same moment.
    assert merged and since in merged[0] and "state=merged" in merged[0]


def test_api_done_returns_one_earlier_local_day(webapp_client, monkeypatch):
    client, app, _overrides = webapp_client
    app.state.webapp_config.gitlab_group = "o"
    day = date.today() - timedelta(days=2)
    closed = datetime.combine(day, datetime.min.time()).astimezone().replace(hour=12)
    stamp = closed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    calls = []
    row = {
        "iid": 5, "title": "shipped", "web_url": "u5", "updated_at": stamp, "closed_at": stamp,
        "labels": [], "references": {"full": "o/app-launcher#5"},
    }

    def fake_run(argv, **kwargs):
        path = argv[argv.index("api") + 1]
        calls.append(path)
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps([row] if "/issues?" in path else []), stderr="")

    monkeypatch.setattr(forge.subprocess, "run", fake_run)
    body = client.get("/api/board/done?days_back=2").json()
    assert body["days_back"] == 2
    assert body["day"] == day.isoformat()
    assert [(i["repo"], i["number"], i["closed_at"]) for i in body["items"]] == [
        ("app-launcher", 5, stamp)]
    assert len(calls) == 2   # a constant two glab calls per request: issues, then MRs


def test_api_done_rejects_a_day_outside_the_week_without_calling_glab(webapp_client, monkeypatch):
    client, _app, _overrides = webapp_client

    def no_glab(argv, **kwargs):
        raise AssertionError("glab must not run")

    monkeypatch.setattr(forge.subprocess, "run", no_glab)
    for bad in ("0", "8", "-1", "x"):
        assert client.get(f"/api/board/done?days_back={bad}").status_code in (400, 422), bad


def test_api_done_surfaces_a_glab_failure_and_sits_behind_auth(webapp_client, monkeypatch):
    client, app, _overrides = webapp_client
    app.state.webapp_config.gitlab_group = "o"

    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="rate limited")

    monkeypatch.setattr(forge.subprocess, "run", failing)
    res = client.get("/api/board/done?days_back=1")
    assert res.status_code == 502 and "rate limited" in res.json()["detail"]

    app.state.webapp_config.auth_token = "secret-token"
    assert client.get("/api/board/done?days_back=1").status_code == 401


def test_the_default_zone_is_the_hosts():
    start, end = forge.local_day_window(date(2026, 7, 2))
    assert start.tzinfo == timezone.utc and end - start in (
        timedelta(hours=24) - timedelta(seconds=1),
        timedelta(hours=23) - timedelta(seconds=1),
        timedelta(hours=25) - timedelta(seconds=1),
    )
