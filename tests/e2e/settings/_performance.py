"""Settings > Performance, walked inside the Settings sheets test: the presets,
a refused value with its reason, an edited limit reading Custom, and the
health line."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e.fixtures.pages import loopback
from tests.e2e.fixtures.sessions import auth_headers
from tests.e2e.fixtures.ui import open_settings, open_settings_sheet
from tests.e2e.fixtures.waits import stable_eval


def _config_values(base_url: str, auth_token: str) -> dict:
    resp = loopback("GET", base_url + "/api/config", headers=auth_headers(auth_token))
    resp.raise_for_status()
    return resp.json()["performance"]["values"]


def _field(page: Page, key: str):
    return page.locator(f'#performanceFields [data-perf-key="{key}"]')


def _preset(page: Page, name: str):
    return page.locator(f'#performancePreset [data-preset="{name}"]')


def walk_performance_sheet(page: Page, base_url: str, auth_token: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings(page)
    row = page.locator('#paneSettings [data-settings-value="performance"]')
    expect(row).to_have_text("Standard")
    open_settings_sheet(page, "performanceSheet")
    expect(_preset(page, "standard")).to_have_attribute("aria-pressed", "true")
    expect(_preset(page, "custom")).to_be_disabled()
    # The health line never stays blank: a figure it cannot read says unknown.
    expect(page.locator("#performanceHealth")).to_contain_text("Now: ")
    # The help line sits clear of the selector, not flush against it.
    gap = stable_eval(page.locator("#performancePresetHelp"), """el => {
      const sel = document.getElementById('performancePreset').getBoundingClientRect();
      return el.getBoundingClientRect().top - sel.bottom;
    }""")
    assert gap >= 8, f"preset help sits {gap}px under the selector"

    # A refused value stays in the field with the reason, and nothing is saved.
    _field(page, "poll_board_s").fill("1")
    _field(page, "poll_board_s").blur()
    expect(page.locator("#performanceFields .field-error:visible")).to_contain_text("between 2 and 120")
    assert _config_values(base_url, auth_token)["poll_board_s"] == 5

    # One edited limit leaves both presets: Custom lights up.
    _field(page, "poll_board_s").fill("9")
    _field(page, "poll_board_s").blur()
    expect(_preset(page, "custom")).to_have_attribute("aria-pressed", "true")
    assert _config_values(base_url, auth_token)["poll_board_s"] == 9

    # Light machine writes every limit in one go and fills the fields.
    _preset(page, "light").click()
    expect(_preset(page, "light")).to_have_attribute("aria-pressed", "true")
    expect(_field(page, "poll_board_s")).to_have_value("15")
    expect(_field(page, "pool_git_status")).to_have_value("2")
    assert _config_values(base_url, auth_token)["pool_hot_poll"] == 4

    # The hidden-tab pause is a switch like the others, and flipping it makes the set Custom.
    switch = _field(page, "poll_pause_hidden")
    expect(switch).to_have_attribute("aria-checked", "true")
    switch.click()
    expect(switch).to_have_attribute("aria-checked", "false")
    expect(_preset(page, "custom")).to_have_attribute("aria-pressed", "true")

    _preset(page, "standard").click()
    expect(_preset(page, "standard")).to_have_attribute("aria-pressed", "true")
    expect(switch).to_have_attribute("aria-checked", "true")
    page.locator("#performanceSheet .detail-actions [data-sheet-close]").click()
    expect(row).to_have_text("Standard")
