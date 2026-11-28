"""The fleet chief leaves the Board's lanes and every waiting count, like a job run's session.

The chief is a managed session: its conversation lives in the Board chat bar, so an idle
chief card or a "needs you" tally for it is noise. The Coding tab keeps its row. Hermetic:
the Board and the session list are route-mocked, with fictional sessions.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board._chief_support import (
    _board_payload,
    _mock_board,
    _open_board,
)

pytestmark = pytest.mark.smoke

_NOW = int(time.time())


def _session(sid: str, label: str, name: str, column: str, status: str) -> dict:
    return {"session_id": sid, "kind": "pty", "agent": "copilot", "label": label,
            "project_dir": "E:/stub/" + name, "name": name, "flags": "",
            "started_at": _NOW - 600, "alive": True, "rows": 40, "cols": 120,
            "live_title": "", "prompt_title": name + " title", "manual_title": "",
            "output_chars": 10, "board_column": column, "board_status": status}


# The chief is routed into Claude's turn by the server, but a stale session-host list can
# still hand the client one in Your turn: the filter must not rely on the lane.
_SESSIONS = [
    _session("s-chief", "chief", "chief", "your_turn", "awaiting-input"),
    _session("s-work", "", "repo-f", "claude_turn", "working"),
]


def _mock(page: Page) -> None:
    _mock_board(page, _board_payload(with_chief=True))
    page.route(re.compile(r".*/api/claude-code/sessions$"), lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"sessions": _SESSIONS})))


def _external_card(name: str, *, job_run: bool) -> dict:
    return {"session_id": None, "kind": "external", "agent": "copilot",
            "project_dir": "E:/stub/" + name, "name": name, "alive": None,
            "started_at": None, "live_title": "", "prompt_title": "", "manual_title": "",
            "project": name, "status": "awaiting-input", "age_seconds": 120,
            "job_run": job_run}


def test_chief_and_job_runs_leave_the_lanes(
    authed_page: Page, base_url: str
) -> None:
    """One page, two server answers in turn: the chief hidden, then a job run's
    session hidden beside an ordinary external one."""
    _mock(authed_page)
    _open_board(authed_page, base_url)

    expect(authed_page.locator('.board-list[data-col="claude_turn"] li.board-item')
           .filter(has_text="repo-f")).to_have_count(1)
    expect(authed_page.locator("li.board-item-chief")).to_have_count(0)
    # One worker: the header counts it, not the chief; nothing waits on the owner.
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("1 session")
    expect(authed_page.locator("#tabBoard .tab-badge")).to_have_count(0)

    # The Coding tab keeps the chief's row, and it does not claim to need you.
    authed_page.locator("#tabClaude").click()
    row = authed_page.locator('#sessionsList li[data-session-id="s-chief"]')
    expect(row).to_have_count(1)
    expect(row.locator(".session-attention")).to_have_count(0)

    # A job run's session leaves the lanes and the count like the chief does.
    payload = _board_payload(with_chief=False)
    payload["columns"]["your_turn"] = [
        _external_card("weekly-cleanup", job_run=True),
        _external_card("scratch-repo", job_run=False),
    ]
    _mock_board(authed_page, payload)
    _open_board(authed_page, base_url)
    lanes = authed_page.locator("li.board-item")
    expect(lanes.filter(has_text="scratch-repo")).to_have_count(1)
    expect(lanes.filter(has_text="weekly-cleanup")).to_have_count(0)
    # Only the ordinary external card waits on the owner; the job run is not counted.
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("1 needs you")
    expect(authed_page.locator("#tabBoard .tab-badge")).to_have_text("1")

    # Bot's turn counts the sessions at work: a finished one folds into the
    # shut Finished group under the lane, outside the count.
    payload = _board_payload(with_chief=False)
    payload["columns"]["claude_turn"] += [
        {**_external_card("done-repo", job_run=False), "status": "idle-finished",
         "finished": True, "age_seconds": 30000},
        {**_external_card("old-idle", job_run=False), "status": "idle",
         "finished": True, "age_seconds": 40000},
    ]
    _mock_board(authed_page, payload)
    _open_board(authed_page, base_url)
    lane = authed_page.locator('.board-list[data-col="claude_turn"] li.board-item')
    expect(lane).to_have_count(1)
    expect(authed_page.locator("#boardColClaude > summary .board-count")).to_have_text("1")
    group = authed_page.locator("details.board-finished")
    expect(group.locator(".board-finished-count")).to_have_text("2")
    expect(group).not_to_have_attribute("open", "")
    finished = group.locator("li.board-item")
    expect(finished).to_have_count(2)
    expect(finished.first).not_to_be_visible()
    group.locator("summary").click()
    expect(finished.filter(has_text="done-repo")).to_be_visible()
