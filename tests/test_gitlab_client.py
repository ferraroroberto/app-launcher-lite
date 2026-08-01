"""src.gitlab_client — the Board's ``glab`` fetch layer (Phase 5).

All subprocess calls are mocked (``glab`` is not assumed installed anywhere);
the canned rows encode the REAL GitLab API shapes, fetched live from
gitlab.com on 2026-08-01 — ``iid`` not ``number``, plain-string ``labels``,
``web_url`` possibly in the ``/-/work_items/N`` form, ``references.full``
carrying the (possibly subgroup-nested) project path.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from src import gitlab_client
from src.gitlab_client import GlabError


@pytest.fixture(autouse=True)
def _pristine_cache():
    gitlab_client.reset_cache()
    yield
    gitlab_client.reset_cache()


# Real shape: GET /groups/<group>/issues (gitlab.com, 2026-08-01).
_REAL_ISSUE = {
    "iid": 2323,
    "project_id": 5261717,
    "title": "Editor slash commands do not work",
    "state": "opened",
    "updated_at": "2026-07-30T17:30:59.585Z",
    "closed_at": None,
    "web_url": "https://gitlab.com/gitlab-org/gitlab-vscode-extension/-/work_items/2323",
    "labels": ["type::bug", "category:vs code"],
    "references": {
        "short": "#2323",
        "relative": "#2323",
        "full": "gitlab-org/gitlab-vscode-extension#2323",
    },
}

# Subgroup project: repo short name = LAST segment before the '#'.
_SUBGROUP_ISSUE = {
    "iid": 1079,
    "project_id": 999,
    "title": "CI health incident",
    "state": "opened",
    "updated_at": "2026-07-29T09:00:00.000Z",
    "closed_at": None,
    "web_url": "https://gitlab.com/gitlab-org/quality/analytics/ci-health-incidents/-/issues/1079",
    "labels": [],
    "references": {
        "short": "#1079",
        "relative": "#1079",
        "full": "gitlab-org/quality/analytics/ci-health-incidents#1079",
    },
}

# MR shape: issue shape plus draft/work_in_progress; references use !N.
_REAL_MR = {
    "iid": 3289,
    "project_id": 5261717,
    "title": "Draft: rework language server bootstrap",
    "state": "opened",
    "updated_at": "2026-07-30T12:00:00.000Z",
    "closed_at": None,
    "web_url": "https://gitlab.com/gitlab-org/gitlab-vscode-extension/-/merge_requests/3289",
    "labels": ["type::maintenance"],
    "draft": True,
    "work_in_progress": True,
    "references": {
        "short": "!3289",
        "relative": "!3289",
        "full": "gitlab-org/gitlab-vscode-extension!3289",
    },
}


def _fake_run(rows_by_match, calls=None):
    """subprocess.run stand-in: pick canned rows by substring of the api path."""

    def run(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        path = argv[-1]
        rows = []
        for needle, canned in rows_by_match.items():
            if needle in path:
                rows = canned
                break
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(rows), stderr=""
        )

    return run


def _gl_iso(moment: datetime) -> str:
    """GitLab's timestamp format: UTC, milliseconds, trailing Z."""
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------- normalization


def test_norm_issue_maps_real_shape(monkeypatch):
    monkeypatch.setattr(
        gitlab_client.subprocess, "run",
        _fake_run({"/issues?state=opened": [_REAL_ISSUE, _SUBGROUP_ISSUE]}),
    )
    issues = gitlab_client.search_open_issues("gitlab-org")
    assert issues[0] == {
        "kind": "issue",
        "repo": "gitlab-vscode-extension",
        "number": 2323,
        "title": "Editor slash commands do not work",
        # web_url used verbatim — including the /-/work_items/N form.
        "url": "https://gitlab.com/gitlab-org/gitlab-vscode-extension/-/work_items/2323",
        "updated_at": "2026-07-30T17:30:59.585Z",
        # Plain strings, verbatim (gh gave [{name}] dicts; glab gives strings).
        "labels": ["type::bug", "category:vs code"],
    }
    # Subgroup project: last path segment of references.full before '#'.
    assert issues[1]["repo"] == "ci-health-incidents"
    assert issues[1]["number"] == 1079


def test_norm_mr_keeps_pr_kind_and_maps_draft(monkeypatch):
    monkeypatch.setattr(
        gitlab_client.subprocess, "run",
        _fake_run({"merge_requests": [_REAL_MR]}),
    )
    prs = gitlab_client.search_open_prs("gitlab-org")
    assert prs == [{
        "kind": "pr",             # internal kind name — board vocabulary
        "repo": "gitlab-vscode-extension",   # from "...extension!3289"
        "number": 3289,
        "title": "Draft: rework language server bootstrap",
        "url": "https://gitlab.com/gitlab-org/gitlab-vscode-extension/-/merge_requests/3289",
        "updated_at": "2026-07-30T12:00:00.000Z",
        "is_draft": True,
        "labels": ["type::maintenance"],
    }]


def test_audit_meta_label_filtered_from_issues_and_mrs(monkeypatch):
    ledger_issue = {**_REAL_ISSUE, "iid": 37, "labels": ["audit-meta"]}
    ledger_mr = {**_REAL_MR, "iid": 38, "labels": ["Audit-Meta"]}
    monkeypatch.setattr(
        gitlab_client.subprocess, "run",
        _fake_run({
            "/issues?state=opened": [_REAL_ISSUE, ledger_issue],
            "merge_requests": [_REAL_MR, ledger_mr],
        }),
    )
    assert [i["number"] for i in gitlab_client.search_open_issues("g")] == [2323]
    assert [p["number"] for p in gitlab_client.search_open_prs("g")] == [3289]


# ------------------------------------------------------- query construction


def test_group_is_url_encoded_as_one_path_segment(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        gitlab_client.subprocess, "run", _fake_run({}, calls)
    )
    gitlab_client.search_open_issues("grp/sub")
    (argv, _kwargs) = calls[0]
    assert argv[0] == "glab" and argv[1] == "api"
    assert argv[2].startswith("groups/grp%2Fsub/issues?")
    assert "state=opened" in argv[2]
    assert "order_by=updated_at" in argv[2]
    assert "sort=desc" in argv[2]
    assert "per_page=100" in argv[2]


def test_host_rides_gitlab_host_env(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        gitlab_client.subprocess, "run", _fake_run({}, calls)
    )
    gitlab_client.search_open_issues("grp", host="gitlab.example.com")
    gitlab_client.search_open_issues("grp")
    env_with_host = calls[0][1].get("env")
    assert env_with_host is not None
    assert env_with_host["GITLAB_HOST"] == "gitlab.example.com"
    # No host → glab's own default context: env untouched (None).
    assert calls[1][1].get("env") is None


# --------------------------------------------------------------- done today


def test_done_today_filters_on_closed_at_since_local_midnight(monkeypatch):
    """Group issues have no closed_after param — the query is
    state=closed&updated_after=<midnight>, so a row *updated* today but
    *closed* yesterday still comes back and must be dropped client-side,
    as must a closed_at of null."""
    midnight = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    closed_today = {
        **_REAL_ISSUE, "iid": 9, "state": "closed",
        "closed_at": _gl_iso(midnight + timedelta(hours=1)),
        "updated_at": _gl_iso(midnight + timedelta(hours=1)),
    }
    closed_yesterday_updated_today = {
        **_REAL_ISSUE, "iid": 10, "state": "closed",
        "closed_at": _gl_iso(midnight - timedelta(hours=3)),
        "updated_at": _gl_iso(midnight + timedelta(hours=2)),
    }
    closed_at_null = {
        **_REAL_ISSUE, "iid": 11, "state": "closed",
        "closed_at": None,
        "updated_at": _gl_iso(midnight + timedelta(hours=3)),
    }
    audit_meta_closed = {
        **_REAL_ISSUE, "iid": 12, "state": "closed",
        "labels": ["audit-meta"],
        "closed_at": _gl_iso(midnight + timedelta(hours=1)),
        "updated_at": _gl_iso(midnight + timedelta(hours=1)),
    }
    later_today = {
        **_REAL_ISSUE, "iid": 13, "state": "closed",
        "closed_at": _gl_iso(midnight + timedelta(hours=5)),
        "updated_at": _gl_iso(midnight + timedelta(hours=5)),
    }
    calls: list = []
    monkeypatch.setattr(
        gitlab_client.subprocess, "run",
        _fake_run({"state=closed": [
            closed_today, closed_yesterday_updated_today,
            closed_at_null, audit_meta_closed, later_today,
        ]}, calls),
    )
    done = gitlab_client.search_done_today("grp")
    # Newest first by updated_at; done cards are stripped to state+no labels.
    assert [(d["number"], d["state"], d["labels"]) for d in done] == [
        (13, "closed", []), (9, "closed", []),
    ]
    # The query itself carries updated_after=<url-encoded local midnight>.
    path = calls[0][0][2]
    assert "state=closed" in path
    assert "updated_after=" in path
    assert "%3A" in path  # ISO offset got URL-encoded into the one segment


# ------------------------------------------------------------------ errors


def test_missing_binary_raises_glab_error(monkeypatch):
    def _boom(argv, **kwargs):
        raise FileNotFoundError("glab not on PATH")

    monkeypatch.setattr(gitlab_client.subprocess, "run", _boom)
    with pytest.raises(GlabError, match="glab"):
        gitlab_client.search_open_issues("grp")


def test_timeout_raises_glab_error(monkeypatch):
    def _slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 20.0)

    monkeypatch.setattr(gitlab_client.subprocess, "run", _slow)
    with pytest.raises(GlabError):
        gitlab_client.search_open_issues("grp")


def test_nonzero_exit_raises_glab_error_with_first_stderr_line(monkeypatch):
    def _fail(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="401 Unauthorized\nrun glab auth login"
        )

    monkeypatch.setattr(gitlab_client.subprocess, "run", _fail)
    with pytest.raises(GlabError, match="401 Unauthorized"):
        gitlab_client.search_open_issues("grp")


def test_unparseable_json_raises_glab_error(monkeypatch):
    def _garbage(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="{not json", stderr="")

    monkeypatch.setattr(gitlab_client.subprocess, "run", _garbage)
    with pytest.raises(GlabError, match="unparseable"):
        gitlab_client.search_open_issues("grp")


# ------------------------------------------------------------------- cache


def test_refresh_and_snapshot(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        gitlab_client.subprocess, "run",
        _fake_run({"/issues?state=opened": [_REAL_ISSUE]}, calls),
    )
    snap = gitlab_client.refresh("gitlab-org")
    assert snap["error"] is None
    assert snap["fetched_at"]
    assert [i["number"] for i in snap["issues"]] == [2323]
    assert snap["done"] == []
    # refresh fetches exactly what the Board consumes: issues + done today.
    paths = [argv[2] for (argv, _kw) in calls]
    assert len(paths) == 2
    assert not any("merge_requests" in p for p in paths)
    # snapshot() is the memory read the poll uses — no new subprocess calls.
    calls_before = len(calls)
    assert gitlab_client.snapshot()["issues"] == snap["issues"]
    assert len(calls) == calls_before


def test_refresh_failure_keeps_old_data(monkeypatch):
    monkeypatch.setattr(
        gitlab_client.subprocess, "run",
        _fake_run({"/issues?state=opened": [_REAL_ISSUE]}),
    )
    gitlab_client.refresh("gitlab-org")

    def _boom(argv, **kwargs):
        raise FileNotFoundError("glab not on PATH")

    monkeypatch.setattr(gitlab_client.subprocess, "run", _boom)
    snap = gitlab_client.refresh("gitlab-org")
    assert "glab" in (snap["error"] or "")
    assert [i["number"] for i in snap["issues"]] == [2323]  # data survives


def test_refresh_with_empty_group_skips_subprocess_and_hints(monkeypatch):
    def _boom(argv, **kwargs):
        raise AssertionError("no subprocess may run with an empty group")

    monkeypatch.setattr(gitlab_client.subprocess, "run", _boom)
    snap = gitlab_client.refresh("")
    assert snap["error"] == "set gitlab_group in Settings"
    assert snap["fetched_at"] is None
    assert snap["issues"] == []


def test_reset_cache_restores_pristine_state(monkeypatch):
    monkeypatch.setattr(
        gitlab_client.subprocess, "run",
        _fake_run({"/issues?state=opened": [_REAL_ISSUE]}),
    )
    gitlab_client.refresh("gitlab-org")
    assert gitlab_client.snapshot()["issues"]
    gitlab_client.reset_cache()
    assert gitlab_client.snapshot() == {
        "fetched_at": None, "issues": [], "prs": [], "done": [], "error": None,
    }
