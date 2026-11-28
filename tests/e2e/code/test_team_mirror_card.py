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


def test_rows_say_each_state_and_publish_all_counts_only_safe_apps(authed_page: Page, base_url: str) -> None:
    _install(authed_page)
    _open_card(authed_page, base_url)
    expect(_row_loc(authed_page, "alpha")).to_contain_text("Not linked")
    expect(_row_loc(authed_page, "beta")).to_contain_text("3 commits behind")
    expect(_row_loc(authed_page, "beta")).to_contain_text("team changed")
    expect(_row_loc(authed_page, "gamma")).to_contain_text("Up to date")
    expect(_row_loc(authed_page, "delta")).to_contain_text("blocked")
    # beta needs a confirmation and delta is blocked, so nothing is safe to publish in bulk.
    expect(authed_page.locator("#teamMirrorPublishAll")).to_be_disabled()
    expect(authed_page.locator("#teamMirrorFooter")).to_contain_text("refreshes every 30 min")


def test_publish_needs_the_second_confirmation_and_stops_at_an_open_pr(authed_page: Page, base_url: str) -> None:
    sent = _install(authed_page)
    _open_card(authed_page, base_url)
    _row_loc(authed_page, "beta").locator(".action-row-main").click()

    sheet = authed_page.locator("#teamMirrorPreviewSheet")
    expect(sheet).to_be_visible()
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


def test_a_blocked_preview_names_file_and_line_and_cannot_publish(authed_page: Page, base_url: str) -> None:
    _install(authed_page)
    _open_card(authed_page, base_url)
    _row_loc(authed_page, "delta").locator(".action-row-main").click()
    sheet = authed_page.locator("#teamMirrorPreviewSheet")
    expect(sheet.locator("#teamMirrorScan")).to_contain_text("docs/guide.md:3 · term-app")
    expect(sheet.locator("#teamMirrorPublish")).to_be_disabled()


def test_linking_tests_access_and_saves_the_fields(authed_page: Page, base_url: str) -> None:
    sent = _install(authed_page)
    _open_card(authed_page, base_url)
    _row_loc(authed_page, "alpha").locator(".action-row-main").click()

    sheet = authed_page.locator("#teamMirrorLinkSheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator("#teamMirrorAuthor")).to_contain_text("Work Robot")
    sheet.locator("#teamMirrorUrlInput").fill("https://github.com/team-org/alpha.git")
    sheet.locator("#teamMirrorLeaveOutInput").fill("private\nlocal.cfg\n")
    sheet.locator("#teamMirrorTestAccess").click()
    expect(sheet.locator("#teamMirrorAccessResult")).to_contain_text("no repository at that URL")

    sheet.locator('[data-track="team-mirror-link.save"]').click()
    expect(sheet).to_be_hidden()
    assert sent["link"] == [("PUT", {"team_url": "https://github.com/team-org/alpha.git", "branch": "main",
                                     "leave_out": ["private", "local.cfg"]})]
