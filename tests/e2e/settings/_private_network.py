"""Settings › Terminal's two private-network switches, walked inside the Settings
sheets test: each asks before it widens access, a dismissal saves nothing, and
the screen switch stands down while the first is off.

The POSTs are answered without reaching disk and ``GET /api/config`` is
overlaid, so the box's own settings never decide the walk.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, expect

from tests.e2e.fixtures.routes import fulfill_edited_json
from tests.e2e.fixtures.ui import close_settings_sheets, open_settings_sheet
from tests.e2e.fixtures.waits import wait_until

_CONFIG = re.compile(r".*/api/config$")


def _mock_config(page: Page) -> dict:
    state = {"terminal": False, "screen": False, "posts": []}

    def _route(route):
        if route.request.method == "POST":
            body = route.request.post_data_json or {}
            state["posts"].append(body)
            state["terminal"] = body.get("terminal_allow_private_lan", state["terminal"])
            state["screen"] = body.get("screen_allow_private_lan", state["screen"])
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True}))
            return

        def _overlay(body: dict) -> None:
            body["terminal_allow_private_lan"] = state["terminal"]
            body["screen_allow_private_lan"] = state["screen"]

        fulfill_edited_json(route, _overlay)

    page.route(_CONFIG, _route)
    return state


def walk_private_network_switches(page: Page) -> None:
    state = _mock_config(page)
    try:
        open_settings_sheet(page, "terminalSheet")
        main = page.locator("#privateLanToggle")
        screen = page.locator("#screenPrivateLanToggle")
        confirm = page.locator("#confirmDialog")
        expect(main).to_have_attribute("aria-checked", "false")
        expect(screen).to_be_disabled()

        # A dismissal saves nothing.
        main.click()
        expect(confirm).to_be_visible()
        expect(confirm.locator("#confirmDialogMessage")).to_contain_text(
            "anything else on that network")
        confirm.locator("#confirmDialogClose").click()
        expect(confirm).to_be_hidden()
        expect(main).to_have_attribute("aria-checked", "false")
        assert state["posts"] == []

        # The action saves, and the screen switch wakes up.
        main.click()
        confirm.locator("#confirmDialogOk").click()
        wait_until(page, lambda: {"terminal_allow_private_lan": True} in state["posts"],
                   "the private-network switch's POST")
        expect(main).to_have_attribute("aria-checked", "true")
        expect(screen).to_be_enabled()

        # The screen switch has its own warning, naming the whole desktop.
        screen.click()
        expect(confirm.locator("#confirmDialogMessage")).to_contain_text("whole PC")
        confirm.locator("#confirmDialogOk").click()
        wait_until(page, lambda: {"screen_allow_private_lan": True} in state["posts"],
                   "the screen switch's POST")
        expect(screen).to_have_attribute("aria-checked", "true")

        # Off needs no warning.
        main.click()
        wait_until(page, lambda: {"terminal_allow_private_lan": False} in state["posts"],
                   "the switch's off POST")
        expect(confirm).to_be_hidden()
        close_settings_sheets(page)
    finally:
        page.unroute(_CONFIG)
