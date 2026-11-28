"""The Team mirrors card on the Code tab and its Link and Publish sheets.

/api/team-mirror and its POSTs are mocked before goto() (#510), so the rows,
the preview and the publish answer are fixed; the server side has its own
tests against real throwaway repos. Every name here is invented.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.smoke

CHECKED = "2026-01-01T10:00:00Z"
LINK = {"team_url": "https://github.com/team-org/beta.git", "branch": "main", "leave_out": ["private"]}


def _row(project, state, link=True, **status):
    return {
        "project": project,
        "link": dict(LINK) if link else None,
        "host": "github" if link else None,
        "status": {"project": project, "state": state, "checked_at": CHECKED, **status},
    }


LIST = {
    "identity": {"name": "Work Robot", "email": "robot@team-host.test"},
    "refresh_minutes": 30,
    "projects": [
        _row("alpha", "not_linked", link=False),
        _row("beta", "behind", behind=3, team_changes=1, published_before=True,
             scan={"state": "clear", "findings": [], "unknown": {}, "scanned": 12}),
        _row("gamma", "up_to_date", behind=0),
        _row("delta", "blocked", behind=None, team_changes=0,
             scan={"state": "blocked", "findings": [{"path": "docs/guide.md", "line": 3, "rule": "term-app"}],
                   "unknown": {}, "scanned": 4}),
    ],
}

PREVIEW_BETA = {
    "project": "beta", "host": "github", "identity": LIST["identity"], "up_to_date": False, "behind": 3,
    "changes": [{"status": "A", "path": "app/util.py"}, {"status": "M", "path": "README.md"},
                {"status": "D", "path": "old.txt"}],
    "scan": {"state": "clear", "findings": [], "unknown": {}, "scanned": 12},
    "team_changes": [{"sha": "abc1234", "subject": "docs: team note"}],
    "needs_confirmation": True, "pr_title": "Mirror update", "pr_body": "- feat: add util",
    "pr_body_source": "subjects", "can_publish": True,
}

PREVIEW_DELTA = {
    **PREVIEW_BETA, "project": "delta", "team_changes": [], "needs_confirmation": False, "can_publish": False,
    "scan": LIST["projects"][3]["status"]["scan"],
}


def _json(route: Route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def _install(page: Page) -> dict:
    sent: dict = {"publish": [], "link": []}
    page.route(re.compile(r".*/api/team-mirror(\?.*)?$"), lambda r: _json(r, LIST))
    page.route("**/api/team-mirror/beta/preview", lambda r: _json(r, PREVIEW_BETA))
    page.route("**/api/team-mirror/delta/preview", lambda r: _json(r, PREVIEW_DELTA))
    page.route("**/api/team-mirror/test-access",
               lambda r: _json(r, {"ok": False, "error": "no repository at that URL, or it is private to this account"}))

    def publish(route: Route):
        sent["publish"].append(json.loads(route.request.post_data or "{}"))
        _json(route, {"result": "pr_opened", "url": "https://github.com/team-org/beta/pull/7",
                      "note": "the host did not allow merging from here", "branch": "mirror/x"})

    def link(route: Route):
        sent["link"].append((route.request.method, json.loads(route.request.post_data or "{}")))
        _json(route, {"ok": True})

    page.route("**/api/team-mirror/beta/publish", publish)
    page.route("**/api/team-mirror/links/alpha", link)
    return sent


def _open_card(page: Page, base_url: str):
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#teamMirrorCard").evaluate("el => { el.open = true; }")
    rows = page.locator("#teamMirrorList .team-mirror-item")
    expect(rows).to_have_count(4, timeout=5_000)
    return rows


def _row_loc(page: Page, project: str):
    return page.locator(f'#teamMirrorList .team-mirror-item[data-id="{project}"]')


def test_the_card_lists_states_previews_publishes_and_links(authed_page: Page, base_url: str) -> None:
    """One boot for the card and both sheets: the e2e suite's node budget is fixed."""
    page = authed_page
    sent = _install(page)
    _open_card(page, base_url)

    # The rows: each state in words; beta needs a confirmation and delta is
    # blocked, so nothing is safe to publish in bulk.
    expect(_row_loc(page, "alpha")).to_contain_text("Not linked")
    expect(_row_loc(page, "beta")).to_contain_text("3 commits behind")
    expect(_row_loc(page, "beta")).to_contain_text("team changed")
    expect(_row_loc(page, "gamma")).to_contain_text("Up to date")
    expect(_row_loc(page, "delta")).to_contain_text("blocked")
    expect(page.locator("#teamMirrorPublishAll")).to_be_disabled()
    expect(page.locator("#teamMirrorFooter")).to_contain_text("refreshes every 30 min")

    # A blocked preview names file, line and rule and cannot publish.
    sheet = page.locator("#teamMirrorPreviewSheet")
    _row_loc(page, "delta").locator(".action-row-main").click()
    expect(sheet.locator("#teamMirrorScan")).to_contain_text("docs/guide.md:3 · term-app")
    expect(sheet.locator("#teamMirrorPublish")).to_be_disabled()
    sheet.locator("#teamMirrorPreviewClose").click()
    expect(sheet).to_be_hidden()

    # A team-changed app needs the replace switch; the merge switch turns the
    # button into Open PR only, and a refused merge ends at the PR's link.
    _row_loc(page, "beta").locator(".action-row-main").click()
    expect(sheet.locator("#teamMirrorFiles li")).to_have_count(3)
    expect(sheet.locator("#teamMirrorTeamList")).to_contain_text("docs: team note")
    publish = sheet.locator("#teamMirrorPublish")
    expect(publish).to_have_text("Publish and merge")
    expect(publish).to_be_disabled()
    sheet.locator('[data-track="team-mirror-preview.confirm-team"]').click()
    expect(publish).to_be_enabled()
    sheet.locator('[data-track="team-mirror-preview.merge"]').click()
    expect(publish).to_have_text("Open PR only")
    sheet.locator("#teamMirrorPrBody").fill("Edited text")
    publish.click()
    expect(sheet.locator("#teamMirrorResult")).to_contain_text("PR opened, not merged")
    expect(sheet.locator("#teamMirrorResult a.reference-pill")).to_have_attribute(
        "href", "https://github.com/team-org/beta/pull/7")
    assert sent["publish"] == [{"mode": "pr_only", "confirm_team_changes": True, "pr_body": "Edited text"}]
    sheet.locator("#teamMirrorPreviewClose").click()

    # Linking: Test access shows the server's sentence; Save sends the fields.
    link = page.locator("#teamMirrorLinkSheet")
    _row_loc(page, "alpha").locator(".action-row-main").click()
    expect(link).to_be_visible()
    expect(link.locator("#teamMirrorAuthor")).to_contain_text("Work Robot")
    link.locator("#teamMirrorUrlInput").fill("https://github.com/team-org/alpha.git")
    link.locator("#teamMirrorLeaveOutInput").fill("private\nlocal.cfg\n")
    link.locator("#teamMirrorTestAccess").click()
    expect(link.locator("#teamMirrorAccessResult")).to_contain_text("no repository at that URL")
    link.locator('[data-track="team-mirror-link.save"]').click()
    expect(link).to_be_hidden()
    assert sent["link"] == [("PUT", {"team_url": "https://github.com/team-org/alpha.git", "branch": "main",
                                     "leave_out": ["private", "local.cfg"]})]
