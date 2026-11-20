"""Board: row verbs that settle an alert or a prompt without leaving the board.

Dismiss a failed job alert once it has been dealt with, and trust the folder a
Copilot session is asking about (the second half of the one test below).

A failed or unconfirmed run's card carries a dismiss verb; tapping it POSTs
the run's dismissal and drops the card, the lane count and the header's
"failing" count. A stuck or unreadable job card describes a live problem and
has no such verb. The lane also holds the GitLab pipeline rows: a red default-branch
pipeline is a `CI failed` row that opens the pipeline and counts as failing, an
unread one is an inert `CI unknown` line, and neither has a dismiss verb. Hermetic: the board and the dismiss route are mocked, and
the server-side half is covered by tests/unit/jobs/test_jobs_dismissed.py.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board._support import (
    _board_payload,
    _mock_board,
    _open_board,
    _route_board_from,
)
from tests.e2e.fixtures.waits import flush_requests, wait_until

pytestmark = pytest.mark.smoke


def _job(job_id: str, state: str, run_id: str | None) -> dict:
    return {
        "kind": "job", "job_id": job_id, "job_name": job_id.replace("-", " "),
        "state": state, "run_id": run_id,
        "finished_at": "2026-07-02T09:02:00" if state != "stuck" else None,
        "age_seconds": 600,
    }


def test_dismissing_a_failed_job_card_drops_it_and_the_failing_count(
    authed_page: Page, base_url: str
) -> None:
    payload = _board_payload()
    payload["columns"]["other"] = [
        _job("weekly-photos", "failed", "20260702T090200"),
        _job("digest", "unconfirmed", "20260702T091000"),
        _job("wedged", "stuck", "20260702T080000"),
        {"kind": "job", "job_id": "locked", "job_name": "locked",
         "state": "unreadable", "run_id": None, "finished_at": None,
         "age_seconds": None, "error": "[WinError 5] Access is denied"},
        {"kind": "ci", "repo": "tool", "state": "failed", "branch": "main",
         "pipeline_id": 29, "pipeline_url": "https://gitlab.example/grp/tool/-/pipelines/29",
         "age_seconds": 7200, "error": None},
        {"kind": "ci", "repo": "notes", "state": "unknown", "branch": None,
         "pipeline_id": None, "pipeline_url": None, "age_seconds": None,
         "error": "glab api exited 1: 403 Forbidden"},
    ]
    payload["columns"]["your_turn"][0].update(
        {"session_id": "s-trust", "status": "awaiting-decision", "folder_trust": "photo-ocr"}
    )
    _mock_board(authed_page)
    current = {"body": payload}
    _route_board_from(authed_page, current)

    dismissals: list[str] = []

    def _dismiss(route) -> None:
        # What the server does: the dismissed run raises no card any more.
        sent = _json.loads(route.request.post_data or "{}")
        run_id = sent.get("run_id") or route.request.url.split("/runs/")[1].split("/")[0]
        dismissals.append(f"{route.request.method} {route.request.url.split('/api/')[1]} {sent}")
        other = current["body"]["columns"]["other"]
        current["body"]["columns"]["other"] = [c for c in other if c.get("run_id") != run_id]
        route.fulfill(
            status=200, content_type="application/json", body=_json.dumps({"dismissed": True}),
        )

    authed_page.route(re.compile(r".*/api/jobs/[^/]+/runs/[^/]+/dismiss$"), _dismiss)

    trusts: list[str] = []

    def _trust(route) -> None:
        trusts.append(f"{route.request.method} {route.request.url.split('/api/')[1]}")
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "folder": "photo-ocr", "trusted": True}),
        )

    authed_page.route(re.compile(r".*/api/claude-code/sessions/s-trust/folder-trust$"), _trust)
    _open_board(authed_page, base_url)

    lane = authed_page.locator('.board-list[data-col="other"]')
    failed = lane.locator("li.board-item-job.is-failed")
    unconfirmed = lane.locator("li.board-item.is-unconfirmed")
    expect(failed).to_have_count(1)
    expect(failed.locator(".board-job-dismiss")).to_have_count(1)
    expect(unconfirmed.locator(".board-job-dismiss")).to_have_count(1)
    # Stuck and unreadable describe a live problem: no verb.
    expect(lane.locator("li.board-item.is-stuck .board-job-dismiss")).to_have_count(0)
    expect(lane.locator("li.board-item.is-unreadable .board-job-dismiss")).to_have_count(0)
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("6")
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("3 failing")
    # A red pipeline names its project and branch and opens the pipeline; an
    # unread one is an inert line. Neither has a dismiss verb.
    red = lane.locator("li.board-item-ci.is-failed")
    unread = lane.locator("li.board-item-ci.is-unknown")
    expect(red.locator(".board-ci-chip")).to_have_text("CI failed")
    expect(red).to_contain_text("tool")
    expect(red).to_contain_text("main")
    expect(red.get_by_role("button", name="Open the failed pipeline of tool on GitLab")).to_have_count(1)
    expect(unread.locator(".board-ci-chip")).to_have_text("CI unknown")
    expect(unread).to_contain_text("pipelines not read")
    expect(unread.locator("button")).to_have_count(0)
    expect(lane.locator("li.board-item-ci .action-row-verb")).to_have_count(0)

    failed.locator(".board-job-dismiss").click()

    wait_until(authed_page, lambda: len(dismissals) == 1, "the dismiss POST")
    flush_requests(authed_page)
    assert dismissals == ["POST jobs/weekly-photos/runs/20260702T090200/dismiss {}"]
    expect(failed).to_have_count(0)
    expect(unconfirmed).to_have_count(1)
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("5")
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("2 failing")

    # The same mocked board, one row verb over: a session sitting on Copilot's
    # folder-trust prompt names that state and carries Trust this folder, which
    # POSTs its route; no other row carries the chip or the verb.
    row = authed_page.locator('.board-list[data-col="your_turn"] li.board-item-session')
    expect(row.locator(".board-folder-trust-chip")).to_have_text("needs folder trust")
    expect(authed_page.locator(".board-folder-trust")).to_have_count(1)
    row.locator(".board-folder-trust").click()
    wait_until(authed_page, lambda: len(trusts) == 1, "the folder-trust POST")
    flush_requests(authed_page)
    assert trusts == ["POST claude-code/sessions/s-trust/folder-trust"]
    expect(authed_page.locator(".toast")).to_contain_text("Folder trusted")
