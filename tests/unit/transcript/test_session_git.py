"""A session's Changed files also lists what a shell command wrote, from
``git status``, and attributes nothing that was dirty before the session began.

Every case runs against a real throwaway ``git`` repository, like
``test_git_changes.py``: the module's job is reading what git prints. Every
path and line of content is synthetic.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.session_view.changes import changed_files
from src.session_view.session_git import (
    git_changed_path,
    git_diff_of,
    merge_git_files,
    session_started,
)
from src.subprocess_flags import NO_WINDOW

HOUR = 3600


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
        creationflags=NO_WINDOW, timeout=60,
    ).stdout


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _cop(kind: str, data: Dict[str, Any], ts: float) -> Dict[str, Any]:
    return {"type": kind, "data": data, "id": "e", "parentId": None, "timestamp": _iso(ts)}


def _write_events(path: Path, lines: List[Dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _age(path: Path, seconds_ago: float) -> None:
    then = time.time() - seconds_ago
    os.utime(path, (then, then))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
    _git(r, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (r / "edited.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (r / "earlier.txt").write_text("one\n", encoding="utf-8")
    (r / "doomed.txt").write_text("bye\n", encoding="utf-8")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    # The state a session starts in: one file already dirty before it began.
    (r / "earlier.txt").write_text("one\ntwo\n", encoding="utf-8")
    _age(r / "earlier.txt", 3 * HOUR)
    return r


@pytest.fixture
def transcript(tmp_path: Path, repo: Path) -> Path:
    """A Copilot session that began an hour ago, edited ``edited.txt`` with its
    edit tool and ran a shell command that wrote ``from-shell.txt``."""
    start = time.time() - HOUR
    (repo / "edited.txt").write_text("alpha\ngamma\n", encoding="utf-8")
    (repo / "from-shell.txt").write_text("made\nby\na shell\n", encoding="utf-8")
    return _write_events(tmp_path / "events.jsonl", [
        _cop("session.start", {}, start),
        _cop("tool.execution_start", {"toolCallId": "t1", "toolName": "edit", "arguments": {
            "path": str(repo / "edited.txt"), "old_str": "beta", "new_str": "gamma"}}, start + 5),
        _cop("tool.execution_complete", {"toolCallId": "t1", "success": True, "result": {"content": "ok"}}, start + 6),
        _cop("tool.execution_start", {"toolCallId": "t2", "toolName": "powershell", "arguments": {
            "command": "Set-Content from-shell.txt ..."}}, start + 10),
        _cop("tool.execution_complete", {"toolCallId": "t2", "success": True, "result": {"content": "ok"}}, start + 11),
    ])


def _merged(transcript: Path, repo: Path) -> Dict[str, Any]:
    return merge_git_files(changed_files(transcript, str(repo), "copilot"), str(repo), transcript)


class TestMergedList:
    def test_a_file_only_a_shell_wrote_is_listed_from_git(self, repo, transcript):
        found = _merged(transcript, repo)
        rows = {r["path"]: r for r in found["files"]}
        assert rows["from-shell.txt"]["source"] == "git"
        assert rows["from-shell.txt"]["status"] == "U"
        assert rows["from-shell.txt"]["additions"] == 3
        assert found["git"] == {"state": "ok", "added": 1}

    def test_a_file_the_edit_tool_recorded_stays_one_transcript_row(self, repo, transcript):
        rows = [r for r in _merged(transcript, repo)["files"] if r["path"] == "edited.txt"]
        assert len(rows) == 1 and rows[0]["source"] == "transcript" and rows[0]["steps"] == 1

    def test_a_file_dirty_before_the_session_is_not_attributed_to_it(self, repo, transcript):
        assert "earlier.txt" not in [r["path"] for r in _merged(transcript, repo)["files"]]

    def test_a_file_the_shell_deleted_is_left_out_not_guessed(self, repo, transcript):
        (repo / "doomed.txt").unlink()
        assert "doomed.txt" not in [r["path"] for r in _merged(transcript, repo)["files"]]

    def test_totals_cover_both_sources(self, repo, transcript):
        found = _merged(transcript, repo)
        assert found["counts"]["additions"] == sum(r["additions"] for r in found["files"])
        assert found["counts"]["additions"] >= 3 + 1

    def test_a_folder_that_is_not_a_repo_keeps_the_transcript_list_and_says_so(self, tmp_path, transcript):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "x.txt").write_text("x\n", encoding="utf-8")
        found = merge_git_files(changed_files(transcript, str(plain), "copilot"), str(plain), transcript)
        assert found["git"] == {"state": "not_a_repo", "added": 0}
        assert all(r["source"] == "transcript" for r in found["files"])

    def test_a_transcript_with_no_start_time_offers_nothing_and_says_so(self, tmp_path, repo):
        bare = _write_events(tmp_path / "bare.jsonl", [{"type": "noise"}])
        found = merge_git_files(changed_files(bare, str(repo), "copilot"), str(repo), bare)
        assert found["git"] == {"state": "baseline_unknown", "added": 0}
        assert found["files"] == []

    def test_a_missing_project_is_not_a_repo(self, transcript):
        found = merge_git_files(changed_files(transcript, None, "copilot"), None, transcript)
        assert found["git"]["state"] == "not_a_repo"


class TestSessionStart:
    def test_reads_iso_and_epoch_stamps(self, tmp_path):
        iso = _write_events(tmp_path / "a.jsonl", [{"x": 1}, {"timestamp": "2026-09-30T10:00:01Z"}])
        assert session_started(iso) == datetime(2026, 9, 30, 10, 0, 1, tzinfo=timezone.utc).timestamp()
        secs = _write_events(tmp_path / "b.jsonl", [{"timestamp": 1_789_670_620}])
        assert session_started(secs) == 1_789_670_620
        millis = _write_events(tmp_path / "c.jsonl", [{"timestamp": 1_789_670_620_000}])
        assert session_started(millis) == 1_789_670_620

    def test_none_for_no_stamp_or_no_file(self, tmp_path):
        assert session_started(_write_events(tmp_path / "d.jsonl", [{"x": 1}])) is None
        assert session_started(tmp_path / "missing.jsonl") is None


class TestGitFallbacks:
    def test_the_jail_answers_only_files_in_the_sessions_git_list(self, repo, transcript):
        shell = str(repo / "from-shell.txt")
        assert git_changed_path(str(repo), transcript, shell) == str((repo / "from-shell.txt").resolve())
        assert git_changed_path(str(repo), transcript, str(repo / "earlier.txt")) is None
        assert git_changed_path(str(repo), transcript, str(repo / "keep-out" / ".." / "x")) is None
        assert git_changed_path(str(repo), transcript, "C:/Windows/win.ini") is None

    def test_the_diff_is_gits_for_that_file(self, repo, transcript):
        got = git_diff_of(str(repo), transcript, str(repo / "from-shell.txt"))
        assert got is not None and "+made" in got["diff"] and got["binary"] is False
        assert git_diff_of(str(repo), transcript, str(repo / "earlier.txt")) is None


class TestRoutes:
    def _serve(self, overrides, monkeypatch, path: Path, project: Path) -> None:
        overrides["session"].list_sessions.return_value = [{
            "session_id": "s1", "kind": "pty", "agent": "copilot", "alive": True,
            "project_dir": str(project), "started_at": 1_789_000_000,
        }]
        from app.webapp.routers import session_transcript as router_mod
        monkeypatch.setattr(router_mod, "find_copilot_transcript", lambda session: path)

    def test_list_diff_and_content_of_a_shell_written_file(
        self, webapp_client, _bypass_gate, monkeypatch, repo, transcript,
    ):
        client, _, overrides = webapp_client
        self._serve(overrides, monkeypatch, transcript, repo)
        base = "/api/claude-code/sessions/s1/changed-files"
        body = client.get(base).json()
        assert body["available"] is True, body
        assert body["git"]["state"] == "ok"
        row = next(r for r in body["files"] if r["path"] == "from-shell.txt")
        assert row["source"] == "git"
        diff = client.get(base + "/diff", params={"path": row["key"]}).json()
        assert diff["available"] is True and "+made" in diff["git_diff"] and diff["steps"] == []
        content = client.get(base + "/content", params={"path": row["key"]}).json()
        assert content["available"] is True and content["text"].startswith("made")

    def test_a_file_dirty_before_the_session_stays_file_not_found(
        self, webapp_client, _bypass_gate, monkeypatch, repo, transcript,
    ):
        client, _, overrides = webapp_client
        self._serve(overrides, monkeypatch, transcript, repo)
        base = "/api/claude-code/sessions/s1/changed-files"
        for leaf in ("diff", "content"):
            body = client.get(f"{base}/{leaf}", params={"path": str(repo / "earlier.txt")}).json()
            assert body["available"] is False and body["reason"] == "file_not_found"

    def test_an_older_window_never_reads_git(
        self, webapp_client, _bypass_gate, monkeypatch, repo, transcript,
    ):
        client, _, overrides = webapp_client
        self._serve(overrides, monkeypatch, transcript, repo)
        body = client.get("/api/claude-code/sessions/s1/changed-files",
                          params={"before": transcript.stat().st_size}).json()
        assert "git" not in body
