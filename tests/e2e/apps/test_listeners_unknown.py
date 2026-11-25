"""An unknown port probe never empties the Other ports list.

``GET /api/ports/probe`` answers from a background sampler. While it has no
trustworthy snapshot (a scan over its budget, an unreadable socket table) it
returns ``listeners: null`` with a reason. The page keeps the last good list on
screen with the stale line, never an empty list that reads as "nothing is
listening". Runs on the wide layout, where the list sits inline in the Apps tab.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_ROW = {
    "port": 8000, "pid": 100, "name": "python.exe", "exe": "python.exe",
    "cmdline": "python -m src.hub", "app": "Local LLM Hub",
    "parent_port": None, "service": "", "url": None,
}


def test_an_unknown_probe_keeps_the_last_list_and_marks_it_stale(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    if browser_name != "chromium":
        pytest.skip("the wide layout needs a fine pointer: desktop Chromium only")
    answers = [{"listeners": [_ROW], "reason": None, "detail": "", "age_seconds": 1.0}]

    def answer(route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answers[0]))

    authed_page.set_viewport_size({"width": 1280, "height": 900})
    authed_page.route("**/api/ports/probe", answer)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabApps").click()
    expect(authed_page.locator("#listenersList .listener-row")).to_have_count(1)

    answers[0] = {"listeners": None, "reason": "overrun", "detail": "the scan has run 12.0s", "age_seconds": 20.0}
    authed_page.locator("#tabApps").click()
    expect(authed_page.locator("#listenersState")).to_have_attribute("data-state", "stale")
    expect(authed_page.locator("#listenersList .listener-row")).to_have_count(1)
