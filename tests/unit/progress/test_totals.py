"""Daily totals: backfill, incremental refresh, the local-day boundary, cache and degraded states."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.clients import gitlab as gitlab_client
from src.progress import totals
from src.subprocess_flags import NO_WINDOW

UTC = timezone.utc
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class FakeGit:
    """Stands in for ``totals.read_git``; records each call's window and can fail a project."""

    def __init__(self, data: Dict[str, List[tuple]]) -> None:
        self.data = data
        self.calls: List[tuple] = []
        self.fail: set = set()

    def __call__(self, checkout: Path, since: datetime) -> List[tuple]:
        self.calls.append((checkout.name, since))
        if checkout.name in self.fail:
            raise RuntimeError("git log failed")
        return self.data.get(checkout.name, [])


class FakeForge:
    """Stands in for ``gitlab_client.project_history``."""

    def __init__(self, data: Dict[str, Dict[str, Any]]) -> None:
        self.data = data
        self.calls: List[tuple] = []
        self.fail: set = set()

    def __call__(self, path: str, since: datetime, host: str) -> Dict[str, Any]:
        self.calls.append((path, since))
        if path in self.fail:
            raise gitlab_client.GlabError("glab api exited 1: 401 Unauthorized")
        return {"merged": [], "opened": [], "closed": [], "truncated": [], **self.data.get(path, {})}


@pytest.fixture
def projects(monkeypatch, tmp_path):
    """Two projects with a GitLab remote and one without."""
    rows = [
        {"name": "alpha", "checkout": tmp_path / "alpha", "gitlab": "grp/alpha"},
        {"name": "beta", "checkout": tmp_path / "beta", "gitlab": "grp/sub/beta"},
        {"name": "local-only", "checkout": tmp_path / "local-only", "gitlab": None},
    ]
    monkeypatch.setattr(totals, "resolve_projects", lambda *_a: rows)


def _refresh(tmp_path, git, forge, now=NOW, tz=UTC):
    return totals.refresh(
        "projects", [], "", path=tmp_path / "t.json", now=now, tz=tz, git_reader=git, forge_reader=forge,
    )


def test_backfill_then_incremental_replaces_the_window(tmp_path, projects):
    git = FakeGit({
        "alpha": [("2026-10-01T10:00:00Z", 10, 2), ("2026-11-03T09:00:00Z", 5, 1)],
        "beta": [("2026-11-04T01:00:00Z", 7, 7)],
    })
    forge = FakeForge({"grp/alpha": {
        "merged": ["2026-11-03T09:30:00Z"], "opened": ["2026-11-02T08:00:00Z"], "closed": ["2026-11-03T18:00:00Z"],
    }})
    first = _refresh(tmp_path, git, forge)

    # A first read backfills the longest range a sheet can show.
    assert git.calls[0][1] == datetime(2026, 11, 4, tzinfo=UTC) - timedelta(days=totals.BACKFILL_DAYS)
    alpha = first["repos"]["alpha"]
    assert alpha["git"]["state"] == "ok" and alpha["gitlab"]["state"] == "ok"
    assert alpha["git"]["days"]["2026-10-01"] == {"lines_added": 10, "lines_removed": 2}
    assert alpha["git"]["days"]["2026-11-03"] == {"lines_added": 5, "lines_removed": 1}
    assert alpha["gitlab"]["days"]["2026-11-03"] == {"mrs_merged": 1, "issues_closed": 1}
    assert alpha["gitlab"]["days"]["2026-11-02"] == {"issues_opened": 1}

    # The next read starts OVERLAP_DAYS before the cursor and replaces from there on.
    git.data["alpha"] = [("2026-11-03T09:00:00Z", 5, 1), ("2026-11-04T08:00:00Z", 1, 1)]
    git.calls.clear()
    later = _refresh(tmp_path, git, forge, now=NOW + timedelta(days=1))
    assert git.calls[0][1] == datetime(2026, 10, 28, tzinfo=UTC)
    days = later["repos"]["alpha"]["git"]["days"]
    assert days["2026-10-01"] == {"lines_added": 10, "lines_removed": 2}  # before the window: kept
    assert days["2026-11-03"] == {"lines_added": 5, "lines_removed": 1}  # re-read, not doubled
    assert days["2026-11-04"] == {"lines_added": 1, "lines_removed": 1}


def test_a_repeat_refresh_does_not_double_count(tmp_path, projects):
    git = FakeGit({"alpha": [("2026-11-03T09:00:00Z", 5, 1)]})
    forge = FakeForge({"grp/alpha": {"merged": ["2026-11-03T09:30:00Z"]}})
    once = _refresh(tmp_path, git, forge)
    twice = _refresh(tmp_path, git, forge)
    again = _refresh(tmp_path, git, forge)
    for snap in (twice, again):
        assert snap["repos"]["alpha"]["git"]["days"] == once["repos"]["alpha"]["git"]["days"]
        assert snap["repos"]["alpha"]["gitlab"]["days"] == once["repos"]["alpha"]["gitlab"]["days"]


def test_an_event_is_bucketed_on_the_local_day_not_the_utc_day(tmp_path, projects):
    # 22:30 UTC on the 3rd is 00:30 on the 4th at UTC+2.
    plus_two = timezone(timedelta(hours=2))
    git = FakeGit({"alpha": [("2026-11-03T22:30:00Z", 3, 0), ("2026-11-03T21:59:00Z", 4, 0)]})
    forge = FakeForge({"grp/alpha": {"merged": ["2026-11-03T22:30:00Z"], "closed": ["2026-11-03T21:59:00Z"]}})
    snap = _refresh(tmp_path, git, forge, tz=plus_two)
    assert snap["repos"]["alpha"]["git"]["days"] == {
        "2026-11-03": {"lines_added": 4, "lines_removed": 0},
        "2026-11-04": {"lines_added": 3, "lines_removed": 0},
    }
    assert snap["repos"]["alpha"]["gitlab"]["days"]["2026-11-04"] == {"mrs_merged": 1}
    assert snap["repos"]["alpha"]["gitlab"]["days"]["2026-11-03"] == {"issues_closed": 1}


def test_a_project_without_a_gitlab_remote_is_skipped_and_listed_not_zero(tmp_path, projects):
    git = FakeGit({"local-only": [("2026-11-03T09:00:00Z", 8, 2)]})
    forge = FakeForge({})
    snap = _refresh(tmp_path, git, forge)
    local = snap["repos"]["local-only"]
    assert local["gitlab"]["state"] == totals.SKIPPED
    assert local["gitlab"]["reason"] == totals.NO_REMOTE
    assert local["gitlab"]["days"] == {}
    assert local["git"]["state"] == "ok"  # its lines still count
    assert snap["skipped"] == [{"repo": "local-only", "reason": totals.NO_REMOTE}]
    assert [c[0] for c in forge.calls] == ["grp/alpha", "grp/sub/beta"]  # no forge call for it


def test_a_failed_read_keeps_the_previous_days_and_reads_stale(tmp_path, projects):
    git = FakeGit({"alpha": [("2026-11-03T09:00:00Z", 5, 1)]})
    forge = FakeForge({"grp/alpha": {"merged": ["2026-11-03T09:30:00Z"]}})
    _refresh(tmp_path, git, forge)

    git.fail.add("alpha")
    forge.fail.add("grp/alpha")
    snap = _refresh(tmp_path, git, forge, now=NOW + timedelta(days=1))
    alpha = snap["repos"]["alpha"]
    assert alpha["git"]["state"] == totals.STALE and alpha["gitlab"]["state"] == totals.STALE
    assert "401 Unauthorized" in alpha["gitlab"]["error"]
    assert alpha["git"]["days"] == {"2026-11-03": {"lines_added": 5, "lines_removed": 1}}
    assert alpha["gitlab"]["days"] == {"2026-11-03": {"mrs_merged": 1}}
    # A source that failed was not refreshed: its age stays at the last good read.
    assert snap["repos"]["beta"]["git"]["state"] == totals.OK


def test_a_source_never_read_is_unknown_and_each_source_ages_on_its_own(tmp_path, projects):
    git = FakeGit({})
    forge = FakeForge({})
    git.fail.update({"alpha", "beta", "local-only"})
    snap = _refresh(tmp_path, git, forge)
    assert snap["repos"]["alpha"]["git"]["state"] == totals.UNKNOWN
    assert snap["repos"]["alpha"]["gitlab"]["state"] == totals.OK
    # Every git read failed, so git has no refresh time; GitLab does.
    assert snap["refreshed_at"]["git"] is None
    assert snap["refreshed_at"]["gitlab"] == "2026-11-04T12:00:00Z"


def test_a_full_page_is_carried_as_truncated(tmp_path, projects):
    forge = FakeForge({"grp/alpha": {"merged": ["2026-11-03T09:30:00Z"], "truncated": ["merged"]}})
    snap = _refresh(tmp_path, FakeGit({}), forge)
    assert snap["repos"]["alpha"]["gitlab"]["truncated"] == ["merged"]
    assert snap["repos"]["beta"]["gitlab"]["truncated"] == []


def test_snapshot_never_runs_git_or_glab(tmp_path, monkeypatch):
    def refused(*_a, **_k):
        raise AssertionError("a read of the cache must not run git or glab")

    monkeypatch.setattr(totals, "read_git", refused)
    monkeypatch.setattr(gitlab_client, "project_history", refused)
    monkeypatch.setattr(totals, "run_git", refused)
    snap = totals.snapshot(tmp_path / "missing.json")
    assert snap["repos"] == {} and snap["refreshed_at"] == {"git": None, "gitlab": None}


def test_an_unreadable_store_is_an_error_with_no_figures(tmp_path):
    bad = tmp_path / "t.json"
    bad.write_text("{not json", encoding="utf-8")
    snap = totals.snapshot(bad)
    assert "could not be read" in snap["error"] and snap["repos"] == {}
    bad.write_text('["a list"]', encoding="utf-8")
    assert "unknown shape" in totals.snapshot(bad)["error"]


def test_a_refresh_already_running_is_a_plain_snapshot(tmp_path, projects):
    assert totals._run_lock.acquire(blocking=False)
    try:
        snap = _refresh(tmp_path, FakeGit({}), FakeForge({}))
    finally:
        totals._run_lock.release()
    assert snap["repos"] == {}


# ------------------------------------------------------------------ remotes and git


@pytest.mark.parametrize("url,host,expected", [
    ("https://gitlab.com/grp/tool.git", "", "grp/tool"),
    ("https://user@gitlab.com/grp/sub/tool", "", "grp/sub/tool"),
    ("git@gitlab.com:grp/tool.git", "", "grp/tool"),
    ("ssh://git@gitlab.com/grp/tool.git", "", "grp/tool"),
    ("https://github.com/me/tool.git", "", None),
    ("https://git.example.org/grp/tool.git", "", None),
    ("https://git.example.org/grp/tool.git", "https://git.example.org", "grp/tool"),
    ("https://gitlab.com/grp/tool.git", "git.example.org", None),
    ("", "", None),
])
def test_parse_remote(url, host, expected):
    assert totals.parse_remote(url, host) == expected


def test_parse_numstat_sums_per_commit_and_skips_binary_files():
    text = (
        "@@2026-11-03T09:00:00+01:00\n\n3\t1\ta.py\n-\t-\timage.png\n2\t0\tb.py\n"
        "@@2026-11-02T09:00:00+01:00\n\n"  # a merge: no numstat lines, still a commit
        "@@2026-11-01T09:00:00+01:00\n\n10\t4\tc.py\n"
    )
    assert totals.parse_numstat(text) == [
        ("2026-11-03T09:00:00+01:00", 5, 1),
        ("2026-11-02T09:00:00+01:00", 0, 0),
        ("2026-11-01T09:00:00+01:00", 10, 4),
    ]
    assert totals.parse_numstat("") == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_read_git_and_remotes_against_a_real_repository(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()

    def git(*args: str, env=None) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
            creationflags=NO_WINDOW, timeout=30, env=env,
        )

    git("init", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    git("remote", "add", "origin", "https://github.com/me/proj.git")
    git("remote", "add", "mirror", "https://gitlab.com/grp/proj.git")
    (repo / "a.txt").write_text("1\n2\n3\n", encoding="utf-8")
    git("add", "a.txt")
    # --no-verify: the dev box inherits a global author-email pre-commit hook the throwaway repo must not obey.
    when = {**os.environ, "GIT_COMMITTER_DATE": "2026-11-03T09:00:00+00:00", "GIT_AUTHOR_DATE": "2026-11-03T09:00:00+00:00"}
    git("commit", "--no-verify", "-m", "first", env=when)
    (repo / "a.txt").write_text("1\n2\n", encoding="utf-8")
    git("commit", "--no-verify", "-am", "second", env={**when, "GIT_COMMITTER_DATE": "2026-11-04T09:00:00+00:00"})

    commits = totals.read_git(repo, datetime(2026, 11, 1, tzinfo=UTC))
    assert [(c[1], c[2]) for c in commits] == [(0, 1), (3, 0)]  # newest first
    assert totals.read_git(repo, datetime(2026, 11, 4, tzinfo=UTC))[0][1:] == (0, 1)
    assert len(totals.read_git(repo, datetime(2026, 12, 1, tzinfo=UTC))) == 0
    assert totals._gitlab_path(repo, "") == "grp/proj"  # the GitLab remote, whatever its name
    assert totals._gitlab_path(repo, "git.example.org") is None

    # Only a folder with a .git directory is a project; a linked worktree (a .git file) is not.
    (tmp_path / "plain").mkdir()
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    names = [p["name"] for p in totals.resolve_projects(str(tmp_path), [], "")]
    assert names == ["proj"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_read_git_raises_when_git_cannot_answer(tmp_path):
    (tmp_path / "empty").mkdir()
    subprocess.run(
        ["git", "-C", str(tmp_path / "empty"), "init", "-b", "main"], check=True, capture_output=True,
        creationflags=NO_WINDOW, timeout=30,
    )
    with pytest.raises(RuntimeError):
        totals.read_git(tmp_path / "empty", datetime(2026, 11, 1, tzinfo=UTC))
