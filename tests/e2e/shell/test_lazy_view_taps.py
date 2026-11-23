"""The Settings pane and the Team OS tab load their code on first open.

Neither draws the Code tab or the Board, so their modules are imported when
the pane is first shown (or once the phone goes idle), not with the page. A
first visit must still work as if the code were already there: a tap on a
Settings row that comes before the pane's code is held and replayed once the
code lands, never dropped, and a first Team OS visit reads its skills once the
tab's code has loaded. Each test holds the module's response to open that
window on purpose, so the tap always lands before the code.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.fixtures.ui import open_settings
from tests.e2e.fixtures.waits import wait_until

pytestmark = pytest.mark.smoke

_SKILLS = {"available": True, "team_os_dir": "C:/stub/team", "skills": [
    {"id": "standup", "name": "standup", "command": "standup",
     "description": "Draft the daily standup.", "skill_md": "x"},
]}


def _hold(page: Page, module: str) -> list:
    """Hold ``module``'s response until the test continues it."""
    held: list = []
    page.route(re.compile(r".*/static/" + re.escape(module) + r"(\?.*)?$"),
               lambda route: held.append(route))
    return held


def _release(held: list) -> None:
    route: Route
    for route in held:
        route.continue_()


def test_a_settings_row_tapped_before_its_code_lands_opens_its_sheet(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    held = _hold(page, "settings/settings-sheets.js")
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    open_settings(page)
    wait_until(page, lambda: bool(held), "the Settings code request")
    page.locator('#paneSettings [data-sheet="copilotSheet"]').click()
    # The code is still held, so nothing can have opened the sheet yet.
    expect(page.locator("#copilotSheet")).to_be_hidden()
    _release(held)
    expect(page.locator("#copilotSheet")).to_be_visible()


def test_a_first_team_os_visit_reads_its_skills_once_its_code_lands(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    page.route(re.compile(r".*/api/team-os/skills$"), lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(_SKILLS)))
    held = _hold(page, "team-os.js")
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabTeamOS").click()
    expect(page.locator("#paneTeamOS")).to_be_visible()
    expect(page.locator("#teamOsList li.teamos-item")).to_have_count(0)
    _release(held)
    expect(page.locator("#teamOsList li.teamos-item")).to_have_count(1)
