"""Red GitLab pipelines on a project's default branch: the read and the Board cards.

``glab`` is mocked. The project and pipeline rows follow what the live probe
returned: the group's project list (``id``, ``path``, ``path_with_namespace``,
``default_branch``, ``builds_access_level``), a project with no pipeline
answering ``200 []`` (also for a ref GitLab does not know), and a pipeline row
(``id``, ``iid``, ``sha``, ``ref``, ``status``, ``created_at``, ``web_url``)
read from a public project and from a failed pipeline of the live group.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import pytest

from src import board
from src.board import ci_runs
from src.board.attention_state import AttentionState
from src.clients import gitlab as forge


def _project(pid: int, path: str, **over):
    return {
        "id": pid, "path": path, "path_with_namespace": f"grp/{path}", "default_branch": "main",
        "builds_access_level": "enabled", "empty_repo": False, **over,
    }


def _pipeline(pid: int, status: str, created: str):
    return {
        "id": pid, "iid": pid - 1000, "project_id": 7, "sha": "a" * 40, "ref": "main",
        "status": status, "source": "push", "created_at": created, "updated_at": created,
        "web_url": f"https://gitlab.example/grp/tool/-/pipelines/{pid}", "name": None,
    }


class _Glab:
    """subprocess.run stand-in: the project list, then each project's pipelines by id."""

    def __init__(self, projects, pipelines, failing=(), list_fails=False):
        self.projects, self.pipelines = projects, pipelines
        self.failing, self.list_fails = set(failing), list_fails
        self.paths: list[str] = []

    def __call__(self, argv, **_kwargs):
        path = argv[argv.index("api") + 1]
        self.paths.append(path)
        if "/projects?" in path and path.startswith("groups/"):
            if self.list_fails:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="502 Bad Gateway\n")
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(self.projects), stderr="")
        if path.startswith("groups/"):
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        pid = int(path.split("/")[1])
        if pid in self.failing:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="403 Forbidden\n")
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(self.pipelines.get(pid, [])), stderr="")


def _read(monkeypatch, projects, pipelines, **kw):
    fake = _Glab(projects, pipelines, **kw)
    monkeypatch.setattr(forge.subprocess, "run", fake)
    return fake, forge.read_pipelines("grp")["projects"]


def test_a_failed_latest_pipeline_is_red_with_the_start_of_its_streak(monkeypatch):
    rows = [
        _pipeline(30, "running", "2026-11-20T10:00:00.000Z"),
        _pipeline(29, "failed", "2026-11-20T09:00:00.000Z"),
        _pipeline(28, "canceled", "2026-11-20T08:00:00.000Z"),
        _pipeline(27, "failed", "2026-11-19T20:00:00.000Z"),
        _pipeline(26, "success", "2026-11-19T10:00:00.000Z"),
    ]
    fake, got = _read(monkeypatch, [_project(7, "tool")], {7: rows})
    red = got["grp/tool"]
    assert red["state"] == forge.CI_RED and red["pipeline_id"] == 29
    assert red["pipeline_url"].endswith("/pipelines/29") and red["branch"] == "main"
    assert red["red_since"] == "2026-11-19T20:00:00.000Z"
    assert any(p.startswith("projects/7/pipelines?ref=main&") for p in fake.paths)


def test_a_later_green_pipeline_drops_the_red(monkeypatch):
    rows = [_pipeline(31, "success", "2026-11-20T11:00:00.000Z"), _pipeline(29, "failed", "2026-11-20T09:00:00.000Z")]
    _, got = _read(monkeypatch, [_project(7, "tool")], {7: rows})
    assert got["grp/tool"]["state"] == forge.CI_GREEN
    assert ci_runs.board_cards({"projects": got}) == []


def test_no_pipeline_and_none_finished_make_no_row(monkeypatch):
    projects = [_project(7, "empty"), _project(8, "busy")]
    _, got = _read(monkeypatch, projects, {8: [_pipeline(40, "running", "2026-11-20T11:00:00.000Z")]})
    assert {k: v["state"] for k, v in got.items()} == {"grp/empty": forge.CI_NONE, "grp/busy": forge.CI_NONE}
    assert ci_runs.board_cards({"projects": got}) == []


def test_an_unreadable_project_is_unknown_never_green(monkeypatch):
    projects = [_project(7, "tool"), _project(8, "other")]
    _, got = _read(monkeypatch, projects, {8: []}, failing=[7])
    assert got["grp/tool"]["state"] == forge.CI_UNKNOWN and "403 Forbidden" in got["grp/tool"]["reason"]
    assert got["grp/other"]["state"] == forge.CI_NONE
    [card] = ci_runs.board_cards({"projects": got})
    assert (card["kind"], card["repo"], card["state"]) == ("ci", "tool", AttentionState.UNKNOWN)
    assert "403 Forbidden" in card["error"] and card["pipeline_url"] is None


def test_an_unnamed_default_branch_is_unknown_and_reads_no_pipelines(monkeypatch):
    fake, got = _read(monkeypatch, [_project(7, "tool", default_branch=None)], {})
    assert got["grp/tool"]["state"] == forge.CI_UNKNOWN
    assert got["grp/tool"]["reason"] == "default branch not named"
    assert not [p for p in fake.paths if "/pipelines" in p]


def test_a_project_with_ci_off_is_not_read(monkeypatch):
    fake, got = _read(monkeypatch, [_project(7, "tool", builds_access_level="disabled")], {})
    assert got == {} and not [p for p in fake.paths if "/pipelines" in p]


def test_a_reply_that_is_not_a_list_is_unknown(monkeypatch):
    monkeypatch.setattr(forge, "_api_json", lambda *_a, **_k: {"message": "odd"})
    assert forge._project_ci(_project(7, "tool"), "")["state"] == forge.CI_UNKNOWN


def test_a_red_card_carries_its_age_and_link():
    ci = {"projects": {"grp/tool": {
        "repo": "tool", "state": forge.CI_RED, "branch": "main", "pipeline_id": 29,
        "pipeline_url": "https://gitlab.example/p/29", "red_since": "2026-11-20T09:00:00.000Z",
    }}}
    [card] = ci_runs.board_cards(ci, now=datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc))
    assert card["state"] == AttentionState.FAILED and card["age_seconds"] == 7200
    assert card["pipeline_url"] == "https://gitlab.example/p/29" and card["error"] is None


def test_refresh_reads_pipelines_and_the_board_routes_them_to_other(monkeypatch):
    rows = [_pipeline(29, "failed", "2026-11-20T09:00:00.000Z")]
    fake = _Glab([_project(7, "tool")], {7: rows})
    monkeypatch.setattr(forge.subprocess, "run", fake)
    forge.reset_cache()
    snap = forge.refresh("grp")
    assert snap["error"] is None and snap["ci"]["fetched_at"] == snap["fetched_at"]
    columns = board.build_board([], snap, [])
    assert [c["kind"] for c in columns["other"]] == ["ci"]
    assert columns["other"][0]["state"] == AttentionState.FAILED


def test_no_pipeline_call_runs_off_the_refresh(monkeypatch):
    forge.reset_cache()

    def boom(*_a, **_k):
        raise AssertionError("glab must not run on a poll")

    monkeypatch.setattr(forge.subprocess, "run", boom)
    assert board.build_board([], forge.snapshot(), [])["other"] == []


def test_a_failed_project_list_turns_the_known_projects_unknown(monkeypatch):
    rows = [_pipeline(29, "failed", "2026-11-20T09:00:00.000Z")]
    monkeypatch.setattr(forge.subprocess, "run", _Glab([_project(7, "tool")], {7: rows}))
    forge.reset_cache()
    forge.refresh("grp")
    monkeypatch.setattr(forge.subprocess, "run", _Glab([], {}, list_fails=True))
    snap = forge.refresh("grp")
    assert snap["error"] is None
    assert "502 Bad Gateway" in snap["ci"]["error"]
    [card] = board.build_board([], snap, [])["other"]
    assert card["state"] == AttentionState.UNKNOWN and card["repo"] == "tool"


def test_a_first_failed_project_list_still_says_unknown(monkeypatch):
    monkeypatch.setattr(forge.subprocess, "run", _Glab([], {}, list_fails=True))
    forge.reset_cache()
    snap = forge.refresh("grp")
    [card] = board.build_board([], snap, [])["other"]
    assert card["state"] == AttentionState.UNKNOWN and "502 Bad Gateway" in card["error"]


@pytest.fixture(autouse=True)
def _clean_cache():
    forge.reset_cache()
    yield
    forge.reset_cache()
