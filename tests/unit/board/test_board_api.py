"""Board tab — column routing and the GET /api/board + glab refresh API shape."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import board, gitlab_client
from tests.unit.board._board_helpers import _iso, _live

# ---------------------------------------------- build_board routing (#608)


def test_build_board_idle_finished_routes_to_bot_turn():
    """idle-finished isn't an alert — it belongs in Bot's turn alongside
    working/idle/unknown, not Your turn."""
    cards = [{"session_id": "s1", "status": "idle-finished", "label": ""}]
    columns = board.build_board(cards, {})
    assert columns["your_turn"] == []
    assert [c["session_id"] for c in columns["bot_turn"]] == ["s1"]


def test_build_board_needs_you_family_routes_to_your_turn():
    """stalled / awaiting-decision / awaiting-input all still mean "a human
    needs to look at this" and route to Your turn."""
    cards = [
        {"session_id": "s-stalled", "status": "stalled", "label": ""},
        {"session_id": "s-decision", "status": "awaiting-decision", "label": ""},
        {"session_id": "s-input", "status": "awaiting-input", "label": ""},
    ]
    columns = board.build_board(cards, {})
    assert {c["session_id"] for c in columns["your_turn"]} == {
        "s-stalled", "s-decision", "s-input",
    }
    assert columns["bot_turn"] == []


def test_build_board_has_exactly_four_columns():
    """Phase 5: the "Other" column (open MRs + job cards) is gone — the
    Board is Backlog / Bot's turn / Your turn / Done, nothing else."""
    columns = board.build_board([], {"issues": [], "done": []})
    assert set(columns) == {"backlog", "bot_turn", "your_turn", "done"}


# ---------------------------------------------------------------- API shape


def _gl_issue(repo: str, iid: int, title: str, *, closed: bool = False,
              closed_at: str | None = None, labels: list | None = None,
              updated_at: str = "2026-07-01T10:00:00.000Z") -> dict:
    """A GitLab group-issues row, in the real API shape (see
    tests/test_gitlab_client.py for the client's own fixture coverage)."""
    return {
        "iid": iid, "project_id": 1, "title": title,
        "state": "closed" if closed else "opened",
        "updated_at": updated_at, "closed_at": closed_at,
        "web_url": f"https://gitlab.com/testgroup/{repo}/-/work_items/{iid}",
        "labels": labels or [],
        "references": {
            "short": f"#{iid}", "relative": f"#{iid}",
            "full": f"testgroup/{repo}#{iid}",
        },
    }


class _FakeGlab:
    """subprocess.run stand-in keyed on the glab api path."""

    def __init__(self, closed_rows: list | None = None):
        self.calls = []
        self.closed_rows = closed_rows or []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        path = argv[-1]
        rows: list = []
        if "merge_requests" in path:
            rows = []
        elif "state=closed" in path:
            rows = self.closed_rows
        elif "/issues?state=opened" in path:
            rows = [_gl_issue(
                "app-launcher", 164, "Board tab", labels=["enhancement"],
            )]
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(rows), stderr=""
        )


def test_api_board_shape_with_everything_absent(webapp_client):
    client, _app, _overrides = webapp_client
    body = client.get("/api/board").json()
    assert set(body["columns"]) == {"backlog", "bot_turn", "your_turn", "done"}
    assert body["gitlab"] == {"fetched_at": None, "error": None}
    assert body["sessions_state"]["available"] is False
    assert body["active_issues"]["available"] is False
    assert body["columns"]["backlog"] == []
    assert body["generated_at"]


def test_api_board_marks_active_backlog_issue(
    webapp_client, monkeypatch
):
    client, app, _overrides = webapp_client
    fake = _FakeGlab()
    monkeypatch.setattr(gitlab_client.subprocess, "run", fake)
    gitlab_client.refresh("testgroup")

    active_file = Path(app.state.webapp_config.sessions_state_file).with_name(
        "active-issues.json"
    )
    active_file.write_text(json.dumps({
        "app-launcher#164": {
            "repo": "app-launcher", "number": 164,
            "branch": "feat/164-board", "started_at": _iso(datetime.now(timezone.utc)),
        },
    }), encoding="utf-8")

    body = client.get("/api/board").json()
    assert body["active_issues"]["available"] is True
    assert body["active_issues"]["count"] == 1
    assert body["columns"]["backlog"][0]["in_progress"] is True


def test_api_board_merges_live_sessions_and_state(webapp_client):
    client, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [
        _live("live-1", "E:/automation/photo-ocr", 20),
    ]
    state_file = Path(app.state.webapp_config.sessions_state_file)
    state_file.write_text(json.dumps({
        "t-uuid": {"project": "photo-ocr", "status": "needs-you",
                   "cwd": "E:/automation/photo-ocr",
                   "updated_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=3))},
    }), encoding="utf-8")

    body = client.get("/api/board").json()
    assert body["sessions_state"]["available"] is True
    your_turn = body["columns"]["your_turn"]
    assert [c["session_id"] for c in your_turn] == ["live-1"]
    # #608: no transcript to check, so the needs-you split lands on the
    # safe generic value — still routed to Your turn either way.
    assert your_turn[0]["status"] == "awaiting-input"
    assert body["columns"]["bot_turn"] == []


def test_api_board_survives_session_host_down(webapp_client):
    client, _app, overrides = webapp_client
    from src.session_client import SessionHostError
    overrides["session"].list_sessions.side_effect = SessionHostError("down")
    body = client.get("/api/board").json()
    assert body["columns"]["bot_turn"] == []


def test_api_refresh_endpoint_fills_cache(webapp_client, monkeypatch):
    client, _app, _overrides = webapp_client
    fake = _FakeGlab()
    monkeypatch.setattr(gitlab_client.subprocess, "run", fake)

    gitlab = client.post("/api/board/gitlab/refresh").json()
    assert gitlab["error"] is None
    assert gitlab["fetched_at"]
    # The group from config reaches the glab api path.
    assert any("testgroup" in " ".join(argv) for argv in fake.calls)
    # The 4-column Board consumes issues + done only — no MR fetch.
    assert not any("merge_requests" in " ".join(argv) for argv in fake.calls)

    body = client.get("/api/board").json()
    assert [c["number"] for c in body["columns"]["backlog"]] == [164]
    assert body["columns"]["your_turn"] == []
    assert body["columns"]["done"] == []


def test_api_refresh_endpoint_hints_when_group_unset(webapp_client, monkeypatch):
    """Empty gitlab_group: no subprocess at all, and the snapshot's error
    tells the UI to point at Settings instead of crashing the panel."""
    client, app, _overrides = webapp_client
    app.state.webapp_config.gitlab_group = ""

    def _boom(argv, **kwargs):
        raise AssertionError("glab must not run with no group configured")

    monkeypatch.setattr(gitlab_client.subprocess, "run", _boom)
    gitlab = client.post("/api/board/gitlab/refresh").json()
    assert "gitlab_group" in (gitlab["error"] or "")
    assert gitlab["fetched_at"] is None
