"""A slow or failing source never holds up the first load.

Every source the session list and the Board do not need is held unanswered
for the whole test, and the GitLab refresh fails. The page clock is paused, so
no poll can fire: whatever the page shows came from the boot reads alone. A
reload on the Board still draws its lanes (its first read used to wait for the
5 s poll), with the plan card saying it is still reading instead of blocking
them, and the Coding tab still lists its sessions while the Usage card's own
read is still held.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Error, Page, Route, expect

pytestmark = pytest.mark.smoke

# Sources neither the session list nor the Board lanes need.
_HUNG = re.compile(
    r".*/api/(claude-code/git-status|ports/probe|apps/running|system/load|system/health"
    r"|team-os/skills|copilot-usage|webauthn/status|status|terminal-themes|board/chief-plan"
    r"|board/chief-journal)(\?.*)?$"
)
_FAILING = re.compile(r".*/api/board/github/refresh$")


def test_a_hung_or_failing_source_never_holds_up_the_list_or_the_board(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    held: list[Route] = []
    page.route(_HUNG, lambda route: held.append(route))
    page.route(_FAILING, lambda route: route.fulfill(status=500, json={"detail": "glab down"}))
    page.clock.install()
    page.add_init_script("try { localStorage.setItem('app-launcher.tab', 'board'); } catch (e) {}")
    try:
        page.goto(f"{base_url}/", wait_until="commit")
        page.clock.pause_at(page.evaluate("Date.now()"))

        expect(page.locator("#paneBoard")).to_be_visible()
        expect(page.locator("#boardChiefPlan .board-plan-note")).to_have_text("Reading the plan…")
        expect(page.locator("#boardFetchState")).to_be_hidden()
        assert held, "no source was held: the test proved nothing"

        page.locator("#tabClaude").click()
        page.wait_for_function(
            """() => {
              const list = document.getElementById('sessionsList');
              const empty = document.getElementById('sessionsEmpty');
              return !!list && (list.children.length > 0 || (empty && !empty.hidden));
            }"""
        )
        # The Usage card's read is held too: the card keeps its placeholder.
        expect(page.locator("#codingUsage")).to_have_attribute("data-pending", "true")
    finally:
        for route in held:
            try:
                route.abort()
            except Error:
                pass
