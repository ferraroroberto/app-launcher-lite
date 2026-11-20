"""Settings > Speech to text, walked inside the Settings sheets test: the sheet
saves each field, the row value follows the switch, and the connection test
shows each of its three states."""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect

from tests.e2e.fixtures.pages import loopback
from tests.e2e.fixtures.sessions import auth_headers
from tests.e2e.fixtures.ui import open_settings, open_settings_sheet


def set_speech(base_url: str, auth_token: str, **patch: object) -> None:
    """Write speech settings straight to the disposable webapp's config."""
    loopback("POST", base_url + "/api/config", headers=auth_headers(auth_token),
             json=patch).raise_for_status()


def _stub_probe(state: str):
    return lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"state": state, "detail": "stubbed"}))


def walk_speech_sheet(page: Page, base_url: str, auth_token: str) -> None:
    reset = dict(speech_enabled=False, speech_base_url="", speech_model="", speech_api_key="")
    set_speech(base_url, auth_token, **reset)
    try:
        page.goto(base_url, wait_until="domcontentloaded")
        open_settings(page)
        row = page.locator('#paneSettings [data-settings-value="speech"]')
        expect(row).to_have_text("Off")
        open_settings_sheet(page, "speechSheet")

        page.locator("#speechBaseUrl").fill("http://127.0.0.1:9/v1")
        page.locator("#speechBaseUrl").blur()
        page.locator("#speechApiKey").fill("fake-key-for-test")
        page.locator("#speechApiKey").blur()
        expect(page.locator("#speechApiKeyState")).to_have_text("A key is set.")
        expect(page.locator("#speechApiKey")).to_have_value("")
        page.locator("#speechEnabledToggle").click()
        expect(page.locator("#speechEnabledToggle")).to_have_attribute("aria-checked", "true")

        # Port 9 has no listener: the real server answers "refused".
        page.locator("#speechTest").click()
        expect(page.locator("#speechTestResult")).to_contain_text("Refused")

        for state in ("reachable", "unknown"):
            page.route("**/api/speech/test", _stub_probe(state))
            page.locator("#speechTest").click()
            expect(page.locator("#speechTestResult")).to_have_text(f"{state.capitalize()}: stubbed")
            page.unroute("**/api/speech/test")

        page.locator("#speechApiKeyClear").click()
        expect(page.locator("#speechApiKeyState")).to_have_text("No key set.")
        page.locator("#speechSheet .detail-actions [data-sheet-close]").click()
        expect(row).to_have_text("On")
    finally:
        # The disposable webapp is shared by the rest of the run.
        set_speech(base_url, auth_token, **reset)
