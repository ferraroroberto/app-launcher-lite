"""Coding tab: the launch-preset dropdown and what a project launch sends.

A saved preset whose model is on the Copilot list gets a dropdown first on the
Projects launch toolbar (nothing is drawn without one). Picking a preset makes
the next launch carry its model, effort and context; Default sends none of them.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_PRESETS = [
    {"id": "p-high", "name": "MAI Flash high", "agent": "copilot", "model": "mai-code-2-flash", "effort": "high"},
    {"id": "p-long", "name": "Luna long", "agent": "copilot", "model": "gpt-6-luna", "effort": "xhigh",
     "context": "long_context"},
    # Its model is not on the list below, so the server would refuse it: not offered.
    {"id": "p-gone", "name": "Unlisted", "agent": "copilot", "model": "not-listed", "effort": "high"},
]
_ROW = {"id": "alpha", "name": "alpha", "kind": "claude-code", "project_dir": "E:/automation/alpha",
        "added_at": "", "is_favorite": False, "repo_url": None, "issues_url": None}


def _install_routes(page: Page, presets: list, launches: list) -> None:
    def _config(route):
        body = route.fetch().json()
        body["launch_presets"] = presets
        body["copilot"]["models_available"] = ["gpt-6-luna", "mai-code-2-flash"]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def _launch(route):
        launches.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "stub"}))

    page.route("**/api/config", _config)
    page.route("**/api/apps", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"scan_root": "E:/automation", "apps": [_ROW]})))
    page.route("**/api/apps/alpha/launch", _launch)


def _launch_alpha(page: Page, launches: list) -> dict:
    before = len(launches)
    page.locator('.coding-item[data-id="alpha"] .action-row-main').click()
    for _ in range(50):
        if len(launches) > before:
            return launches[-1]
        page.wait_for_timeout(100)
    raise AssertionError("the project row's launch never posted")


def test_a_picked_preset_sets_the_launch_model_effort_and_context(
    authed_page: Page, base_url: str
) -> None:
    launches: list = []
    _install_routes(authed_page, _PRESETS, launches)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    expect(authed_page.locator("#claudeList .coding-item")).to_have_count(1)

    combo = authed_page.locator("#codingPresetCombo")
    trigger = authed_page.locator("#codingPresetBtn")
    expect(trigger).to_have_text("Default")
    assert "model" not in _launch_alpha(authed_page, launches)

    trigger.click()
    options = authed_page.locator("#codingPresetMenu [role=option]")
    expect(options).to_have_text(["Default", "MAI Flash high · high", "Luna long · xhigh · long_context"])

    options.nth(1).click()
    expect(combo).to_have_attribute("data-value", "p-high")
    sent = _launch_alpha(authed_page, launches)
    assert (sent["model"], sent["effort"]) == ("copilot:mai-code-2-flash", "high")
    assert "context" not in sent

    trigger.click()
    authed_page.locator("#codingPresetMenu [role=option]").nth(2).click()
    sent = _launch_alpha(authed_page, launches)
    assert (sent["model"], sent["effort"], sent["context"]) == ("copilot:gpt-6-luna", "xhigh", "long_context")

    trigger.click()
    authed_page.locator("#codingPresetMenu [role=option]").nth(0).click()
    sent = _launch_alpha(authed_page, launches)
    assert not {"model", "effort", "context"} & set(sent)


def test_no_dropdown_is_drawn_without_an_offered_preset(authed_page: Page, base_url: str) -> None:
    _install_routes(authed_page, [_PRESETS[2]], [])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    expect(authed_page.locator("#claudeList .coding-item")).to_have_count(1)
    expect(authed_page.locator("#codingPresetCombo")).to_have_count(0)
