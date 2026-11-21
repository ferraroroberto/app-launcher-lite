"""src.clients.gitlab — the Board's ``glab`` fetch-and-write layer.

Every subprocess call is mocked (``glab`` is not assumed installed). The canned
rows follow GitLab's REST v4 shapes: ``iid`` rather than ``number``, plain
string ``labels``, ``web_url``, ``references.full`` carrying the (possibly
subgroup-nested) project path, ``draft`` on a merge request, ``merged_at``
and ``description`` on a merged one. The issue shapes (open and closed) and
the issue write, note and ``user`` responses were read live from gitlab.com;
the merge-request shapes are the documented API and are not yet proven
against a live instance (the live group had no merge request to read).
"""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta, timezone

import pytest

from src.clients import gitlab as forge

_ISSUE = {
    "iid": 2323,
    "project_id": 5261717,
    "title": "Editor slash commands do not work",
    "state": "opened",
    "updated_at": "2026-07-30T17:30:59.585Z",
    "closed_at": None,
    "web_url": "https://gitlab.com/grp/tool/-/work_items/2323",
    "labels": ["type::bug", "category:vs code"],
    "references": {"short": "#2323", "relative": "#2323", "full": "grp/tool#2323"},
}
_SUBGROUP_ISSUE = {
    **_ISSUE, "iid": 1079, "labels": [],
    "references": {"full": "grp/quality/ci-health#1079"},
}
_MR = {
    "iid": 67, "title": "fix chunk merge", "web_url": "https://gitlab.com/grp/tool/-/merge_requests/67",
    "updated_at": "2026-07-02T08:00:00Z", "draft": True, "labels": [],
    "references": {"full": "grp/tool!67"},
}


def _gl_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _FakeGlab:
    """subprocess.run stand-in: answers by the first matching substring of the api path."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        path = argv[argv.index("api") + 1]
        body = []
        for needle, canned in self.answers.items():
            if needle in path:
                body = canned
                break
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")


def _install(monkeypatch, answers):
    fake = _FakeGlab(answers)
    monkeypatch.setattr(forge.subprocess, "run", fake)
    return fake


# ---------------------------------------------------------- refresh / normalisation


def test_refresh_normalises_issues_and_merge_requests(monkeypatch):
    _install(monkeypatch, {
        "merge_requests?state=opened": [_MR],
        "merge_requests?state=merged": [],
        "issues?state=opened": [_ISSUE, _SUBGROUP_ISSUE],
        "issues?state=closed": [],
    })
    snap = forge.refresh("grp")
    assert snap["error"] is None and snap["fetched_at"]
    assert [(i["repo"], i["number"]) for i in snap["issues"]] == [("tool", 2323), ("ci-health", 1079)]
    assert snap["issues"][0]["labels"] == ["type::bug", "category:vs code"]
    assert snap["issues"][0]["url"].endswith("/work_items/2323")
    [mr] = snap["prs"]
    assert (mr["kind"], mr["repo"], mr["number"], mr["is_draft"]) == ("pr", "tool", 67, True)


def test_audit_meta_issues_become_records(monkeypatch):
    ledger = {**_ISSUE, "iid": 9, "labels": ["audit-meta"]}
    _install(monkeypatch, {"issues?state=opened": [_ISSUE, ledger]})
    snap = forge.refresh("grp")
    assert [i["number"] for i in snap["issues"]] == [2323]
    assert [i["number"] for i in snap["records"]] == [9]


def test_a_full_page_is_flagged_truncated(monkeypatch):
    page = [{**_ISSUE, "iid": n} for n in range(forge._PAGE)]
    _install(monkeypatch, {"issues?state=opened": page})
    assert forge.refresh("grp")["truncated"] is True


def test_the_group_path_is_one_encoded_segment(monkeypatch):
    fake = _install(monkeypatch, {})
    forge.refresh("grp/sub")
    paths = [argv[argv.index("api") + 1] for argv, _ in fake.calls]
    assert all(p.startswith("groups/grp%2Fsub/") for p in paths)


def test_empty_group_never_spawns_glab(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("glab must not run without a group")

    monkeypatch.setattr(forge.subprocess, "run", boom)
    snap = forge.refresh("  ")
    assert snap["error"] == "set gitlab_group in Settings"
    assert snap["fetched_at"] is None


def test_a_failed_refresh_keeps_the_previous_data(monkeypatch):
    _install(monkeypatch, {"issues?state=opened": [_ISSUE]})
    good = forge.refresh("grp")

    def failing(argv, **_k):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="401 Unauthorized\n")

    monkeypatch.setattr(forge.subprocess, "run", failing)
    snap = forge.refresh("grp")
    assert "401 Unauthorized" in snap["error"]
    assert snap["issues"] == good["issues"] and snap["fetched_at"] == good["fetched_at"]


def test_a_missing_glab_is_an_error_not_a_crash(monkeypatch):
    def missing(*_a, **_k):
        raise FileNotFoundError("glab")

    monkeypatch.setattr(forge.subprocess, "run", missing)
    snap = forge.refresh("grp")
    assert snap["fetched_at"] is None and "glab" in snap["error"]


def test_host_rides_gitlab_host(monkeypatch):
    fake = _install(monkeypatch, {})
    forge.refresh("grp", host="gitlab.example.test")
    assert all(kw["env"]["GITLAB_HOST"] == "gitlab.example.test" for _, kw in fake.calls)


def test_recently_filled_reads_the_cache_stamp(monkeypatch):
    assert forge.recently_filled() is False
    _install(monkeypatch, {})
    forge.refresh("grp")
    assert forge.recently_filled() is True


def test_glab_cmd_env_must_be_a_json_argv(monkeypatch):
    monkeypatch.setenv(forge.GLAB_CMD_ENV, "not json")
    with pytest.raises(forge.GlabError):
        forge.issue_title("grp", "tool", 1)


# ---------------------------------------------------------- done today


def test_done_keeps_only_issues_closed_inside_the_day_and_pairs_the_mr(monkeypatch):
    day = date(2026, 7, 2)
    start, end = forge.local_day_window(day, timezone.utc)
    inside = {**_ISSUE, "iid": 5, "state": "closed", "closed_at": _gl_iso(start + timedelta(hours=3))}
    before = {**_ISSUE, "iid": 6, "state": "closed", "closed_at": _gl_iso(start - timedelta(hours=1))}
    never = {**_ISSUE, "iid": 7, "state": "closed", "closed_at": None}
    merged = {
        "iid": 70, "web_url": "https://gitlab.com/grp/tool/-/merge_requests/70",
        "merged_at": _gl_iso(start + timedelta(hours=2)),
        "description": "Closes #5\n\nVISUAL_PROOF=pass",
        "references": {"full": "grp/tool!70"},
    }
    merged_yesterday = {**merged, "iid": 71, "merged_at": _gl_iso(start - timedelta(hours=2)),
                        "description": "Closes #6"}
    fake = _install(monkeypatch, {
        "merge_requests?state=merged": [merged, merged_yesterday],
        "issues?state=closed": [inside, before, never],
    })
    done = forge.search_done_on("grp", day, timezone.utc)
    assert [d["number"] for d in done] == [5]
    assert (done[0]["pr"], done[0]["visual_proof"], done[0]["not_planned"]) == (70, "pass", False)
    assert done[0]["pr_url"].endswith("/merge_requests/70")
    issue_path = next(argv[argv.index("api") + 1] for argv, _ in fake.calls if "issues" in " ".join(argv))
    assert "updated_after=" in issue_path


def test_part_of_does_not_pair_an_mr(monkeypatch):
    day = date(2026, 7, 2)
    start, _ = forge.local_day_window(day, timezone.utc)
    closed = {**_ISSUE, "iid": 5, "state": "closed", "closed_at": _gl_iso(start + timedelta(hours=3))}
    mr = {"iid": 70, "merged_at": _gl_iso(start + timedelta(hours=1)), "description": "Part of #5",
          "references": {"full": "grp/tool!70"}}
    _install(monkeypatch, {"merge_requests?state=merged": [mr], "issues?state=closed": [closed]})
    [row] = forge.search_done_on("grp", day, timezone.utc)
    assert row["pr"] is None


def test_an_issue_closed_through_the_api_lands_in_done(monkeypatch):
    # Shape and millisecond-Z stamps as gitlab.com returned them for an issue
    # filed through ``create_issue`` and then closed with ``state_event=close``.
    live = {
        "iid": 1, "state": "closed", "title": "Board live check",
        "updated_at": "2026-11-19T07:10:55.623Z", "closed_at": "2026-11-19T07:10:55.623Z",
        "web_url": "https://gitlab.com/grp/tool/-/work_items/1", "labels": [],
        "assignees": [{"id": 42}], "references": {"short": "#1", "relative": "#1", "full": "grp/tool#1"},
    }
    _install(monkeypatch, {"issues?state=closed": [live], "merge_requests?state=merged": []})
    [row] = forge.search_done_on("grp", date(2026, 11, 19), timezone.utc)
    assert (row["repo"], row["number"], row["pr"], row["not_planned"]) == ("tool", 1, None, False)
    assert row["closed_at"] == "2026-11-19T07:10:55.623Z"


def test_a_failed_mr_read_leaves_done_without_pairing(monkeypatch):
    day = date(2026, 7, 2)
    start, _ = forge.local_day_window(day, timezone.utc)
    closed = {**_ISSUE, "iid": 5, "state": "closed", "closed_at": _gl_iso(start + timedelta(hours=3))}

    def run(argv, **_k):
        if "merge_requests" in argv[argv.index("api") + 1]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([closed]), stderr="")

    monkeypatch.setattr(forge.subprocess, "run", run)
    [row] = forge.search_done_on("grp", day, timezone.utc)
    assert row["number"] == 5 and row["pr"] is None


# ---------------------------------------------------------- writes and single reads


def test_create_issue_sends_json_on_stdin_and_assigns_me(monkeypatch):
    fake = _install(monkeypatch, {
        "user": {"id": 42},
        "/issues": {"web_url": "https://gitlab.com/grp/tool/-/issues/8"},
    })
    url = forge.create_issue("grp", "tool", "Title & 'quotes'", "body")
    assert url.endswith("/issues/8")
    argv, kw = fake.calls[-1]
    assert argv[argv.index("api") + 1] == "projects/grp%2Ftool/issues"
    assert ("--method", "POST") == tuple(argv[argv.index("--method"):argv.index("--method") + 2])
    assert json.loads(kw["input"]) == {"title": "Title & 'quotes'", "description": "body",
                                       "assignee_ids": [42]}


def test_create_issue_without_a_url_is_an_error(monkeypatch):
    _install(monkeypatch, {"user": {"id": 1}, "/issues": {}})
    with pytest.raises(forge.GlabError):
        forge.create_issue("grp", "tool", "t", "b")


def test_comment_issue_posts_a_note(monkeypatch):
    fake = _install(monkeypatch, {"/notes": {"id": 1}})
    forge.comment_issue("grp", "tool", 12, "hello")
    argv, kw = fake.calls[-1]
    assert argv[argv.index("api") + 1] == "projects/grp%2Ftool/issues/12/notes"
    assert json.loads(kw["input"]) == {"body": "hello"}


def test_issue_title_and_body(monkeypatch):
    _install(monkeypatch, {"/issues/3": {"title": " T ", "description": "D"}})
    assert forge.issue_title("grp", "tool", 3) == "T"
    assert forge.issue_body("grp", "tool", 3) == "D"


# ------------------------------------------------------- project history (the History sheet)

# Trimmed from live `glab api` reads of a project's merged MR and issues; the
# stamps keep GitLab's millisecond ``Z`` form.
_LIVE_MERGED_MR = {
    "iid": 25, "state": "merged", "created_at": "2026-11-20T09:38:50.134Z",
    "merged_at": "2026-11-20T09:38:54.551Z", "closed_at": None,
    "updated_at": "2026-11-20T09:38:54.926Z", "labels": [],
}
_LIVE_CLOSED_ISSUE = {
    "iid": 1, "state": "closed", "created_at": "2026-11-19T07:10:46.491Z",
    "closed_at": "2026-11-19T07:10:55.623Z", "updated_at": "2026-11-19T07:10:55.795Z", "labels": [],
}
_SINCE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_project_history_reads_three_windows_and_returns_utc_stamps(monkeypatch):
    fake = _install(monkeypatch, {
        "merge_requests?state=merged": [_LIVE_MERGED_MR],
        "issues?created_after": [_LIVE_CLOSED_ISSUE],
        "issues?state=closed": [_LIVE_CLOSED_ISSUE],
    })
    got = forge.project_history("grp/sub/tool", _SINCE)
    assert got == {
        "merged": ["2026-11-20T09:38:54Z"], "opened": ["2026-11-19T07:10:46Z"],
        "closed": ["2026-11-19T07:10:55Z"], "truncated": [],
    }
    paths = [argv[argv.index("api") + 1] for argv, _ in fake.calls]
    assert paths == [
        "projects/grp%2Fsub%2Ftool/merge_requests?state=merged&merged_after=2026-11-01T00%3A00%3A00Z&per_page=100",
        "projects/grp%2Fsub%2Ftool/issues?created_after=2026-11-01T00%3A00%3A00Z&per_page=100",
        "projects/grp%2Fsub%2Ftool/issues?state=closed&updated_after=2026-11-01T00%3A00%3A00Z&per_page=100",
    ]


def test_project_history_keeps_a_closed_issue_only_when_it_closed_inside_the_window(monkeypatch):
    # GitLab ignores `closed_after`, so an issue closed in October but touched
    # again in November comes back from `updated_after`; its own closed_at decides.
    reopened_note = {**_LIVE_CLOSED_ISSUE, "iid": 2, "closed_at": "2026-10-20T10:00:00.000Z",
                     "updated_at": "2026-11-05T10:00:00.000Z"}
    _install(monkeypatch, {"issues?state=closed": [_LIVE_CLOSED_ISSUE, reopened_note]})
    assert forge.project_history("grp/tool", _SINCE)["closed"] == ["2026-11-19T07:10:55Z"]


def test_project_history_leaves_out_the_audit_ledger(monkeypatch):
    ledger = {**_LIVE_CLOSED_ISSUE, "iid": 9, "labels": ["audit-meta"]}
    _install(monkeypatch, {"issues?created_after": [_LIVE_CLOSED_ISSUE, ledger]})
    assert len(forge.project_history("grp/tool", _SINCE)["opened"]) == 1


def test_project_history_names_a_full_page(monkeypatch):
    _install(monkeypatch, {"merge_requests?state=merged": [_LIVE_MERGED_MR] * 100})
    got = forge.project_history("grp/tool", _SINCE)
    assert got["truncated"] == ["merged"]
    assert len(got["merged"]) == 100


def test_project_history_raises_when_glab_fails(monkeypatch):
    def boom(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="401 Unauthorized")
    monkeypatch.setattr(forge.subprocess, "run", boom)
    with pytest.raises(forge.GlabError):
        forge.project_history("grp/tool", _SINCE)
