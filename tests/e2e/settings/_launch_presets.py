"""Settings › Coding agent › Launch presets, walked inside the Coding agent sheet test.

The editor lists the saved presets and one form adds or edits one. The model
list is the install's Copilot list, the effort list follows the chosen model
(``mai-code-2-flash`` offers low, medium and high), and a saved preset reaches
``GET /api/config`` with its model, effort and context. A server refusal shows
under the field it names. It lives beside ``test_settings_sheets.py`` as a
helper, not a test of its own, because it shares that test's setup (the e2e
node budget in ``.fleet.toml``).
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e.fixtures.pages import loopback
from tests.e2e.fixtures.sessions import auth_headers
from tests.e2e.fixtures.ui import close_settings_sheets, open_agent_sheet, open_row_menu

_MODEL = "mai-code-2-flash"


def _get(base_url: str, auth_token: str) -> dict:
    resp = loopback("GET", base_url + "/api/config", headers=auth_headers(auth_token))
    resp.raise_for_status()
    return resp.json()


def _presets(base_url: str, auth_token: str) -> list:
    return _get(base_url, auth_token)["launch_presets"]


def _set_models(base_url: str, auth_token: str, models: list) -> None:
    resp = loopback("POST", base_url + "/api/config", headers=auth_headers(auth_token),
                    json={"copilot_models": models})
    resp.raise_for_status()


def walk_launch_presets(page: Page, base_url: str, auth_token: str) -> None:
    """Add, edit, refuse and remove a preset in the Coding agent sheet.

    Reloads the page, so the sheet starts closed. The probed model is put on
    the Copilot list for the walk and the list is put back after: the shared
    config restore covers top-level keys only, and the list sits under
    ``copilot``.
    """
    before = _get(base_url, auth_token)["copilot"]["models_available"]
    if _MODEL not in before:
        _set_models(base_url, auth_token, [*before, _MODEL])
    try:
        page.goto(base_url, wait_until="domcontentloaded")
        _walk(page, base_url, auth_token)
        close_settings_sheets(page)
    finally:
        if _MODEL not in before:
            _set_models(base_url, auth_token, before)


def _walk(page: Page, base_url: str, auth_token: str) -> None:
    open_agent_sheet(page)
    rows = page.locator("#launchPresetList .launch-preset-row")
    expect(rows).to_have_count(0)

    # Only the efforts the model offers can be picked.
    page.locator("#launchPresetModel").select_option(_MODEL)
    expect(page.locator("#launchPresetEffort > option")).to_have_text(["Low", "Medium", "High", "Xhigh", "Max"])
    page.locator("#launchPresetName").fill("Flash high")
    page.locator("#launchPresetEffort").select_option("high")
    page.locator("#launchPresetContext").select_option("long_context")
    page.locator("#launchPresetAdd").click()

    expect(rows).to_have_count(1)
    expect(rows.first.locator(".launch-preset-meta")).to_have_text(f"{_MODEL} · High · Long context")
    saved = _presets(base_url, auth_token)
    assert [(p["name"], p["agent"], p["model"], p["effort"], p.get("context")) for p in saved] == [
        ("Flash high", "copilot", _MODEL, "high", "long_context")
    ]

    # Edit loads the row into the form; saving keeps the id and changes the fields.
    rows.first.locator(".action-row-kebab").click()
    open_row_menu(page).get_by_role("menuitem", name="Edit Flash high").click()
    expect(page.locator("#launchPresetAdd")).to_have_text("Save preset")
    expect(page.locator("#launchPresetName")).to_have_value("Flash high")
    page.locator("#launchPresetEffort").select_option("low")
    page.locator("#launchPresetContext").select_option("")
    page.locator("#launchPresetAdd").click()
    expect(rows.first.locator(".launch-preset-meta")).to_have_text(f"{_MODEL} · Low")
    edited = _presets(base_url, auth_token)
    assert edited[0]["id"] == saved[0]["id"] and edited[0]["effort"] == "low"
    assert "context" not in edited[0]
    expect(page.locator("#launchPresetAdd")).to_have_text("Add preset")

    # A refusal shows beside the field it is about: a name already used.
    page.locator("#launchPresetName").fill("flash HIGH")
    page.locator("#launchPresetAdd").click()
    expect(page.locator("#launchPresetNameError")).to_have_text("flash HIGH: that name is already used")
    expect(page.locator("#launchPresetName")).to_have_attribute("aria-invalid", "true")
    expect(rows).to_have_count(1)

    rows.first.locator(".action-row-kebab").click()
    open_row_menu(page).get_by_role("menuitem", name="Remove Flash high").click()
    expect(rows).to_have_count(0)
    assert _presets(base_url, auth_token) == []
