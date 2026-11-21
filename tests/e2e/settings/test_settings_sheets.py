"""Settings as inset groups (#1435, step 3 of #1432).

Settings is groups of rows (Appearance, Agents, Sessions, Board,
Connections, This PC). Each row shows its current value and a chevron and
opens a sheet on the vendored modal shell: a header back + ✕ and one
full-width primary, Done. Contract:

  * the groups and rows render, and the values match the saved config
    (Coding agent, Chief and GitLab at least);
  * every sheet opens and closes (back, ✕, Done, Escape);
  * the Coding agent sheet offers Default plus the Copilot models, saves a
    pick and shows the server's flags, and its Launch presets add, edit,
    refuse and remove a preset (``_launch_presets.py``);
  * the Terminal sheet's two private-network switches warn before they turn
    on and the screen one follows the first (``_private_network.py``);
  * every ``openSettingsAt`` caller lands inside the sheet that holds its
    field (the Code and Team OS empty states, the Code tab's chief row);
  * no Save button is left: fields save as they change.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._surface import surface
from tests.e2e.fixtures.pages import loopback
from tests.e2e.fixtures.sessions import auth_headers
from tests.e2e.fixtures.ui import (
    close_settings_sheets,
    open_agent_sheet,
    open_row_menu,
    open_settings,
    open_settings_sheet,
)
from tests.e2e.settings._launch_presets import walk_launch_presets
from tests.e2e.settings._private_network import walk_private_network_switches
from tests.e2e.settings._speech import walk_speech_sheet

pytestmark = pytest.mark.smoke

_GROUPS = ["Appearance", "Agents", "Sessions", "Board", "Connections", "This PC"]
_SHEETS = [
    "copilotSheet", "chiefSheet", "gitlabSheet", "speechSheet", "tokensSheet", "foldersSheet",
    "passkeysSheet", "terminalSheet",
]
# The design inventory's name for each sheet (surface() wants lowercase).
_SURFACE_NAMES = {
    "copilotSheet": "copilot", "chiefSheet": "chief", "gitlabSheet": "gitlab",
    "speechSheet": "speech", "tokensSheet": "tokens", "foldersSheet": "folders",
    "passkeysSheet": "passkeys", "terminalSheet": "terminal",
}


def _config(base_url: str, auth_token: str) -> dict:
    resp = loopback("GET", base_url + "/api/config", headers=auth_headers(auth_token))
    resp.raise_for_status()
    return resp.json()


def _value(page: Page, key: str):
    return page.locator(f'#paneSettings [data-settings-value="{key}"]')


def _assert_groups_render_with_values_matching_the_saved_config(authed_page: Page, cfg: dict) -> None:
    expect(authed_page.locator("#paneSettings .settings-overline:visible")).to_have_text(_GROUPS)
    expect(authed_page.locator("#paneSettings button.settings-row[data-sheet]")).to_have_count(len(_SHEETS))
    # Every row is at least the one-line row height and its value sits in
    # the row (a chevron row), never wrapped below it.
    for sheet_id in _SHEETS:
        row = authed_page.locator(f'#paneSettings [data-sheet="{sheet_id}"]')
        expect(row.locator(".settings-row-chevron")).to_have_count(1)
        box = row.bounding_box()
        assert box and box["height"] >= 52 - 0.5, (sheet_id, box)

    cap = cfg["chief_worker_cap"]
    expect(_value(authed_page, "chief")).to_have_text(
        f"{cfg['chief_model'] or 'Default'} · {cap} {'worker' if cap == 1 else 'workers'}"
    )
    # The Coding agent row leads with the Copilot model (pinned by
    # fixtures/launch_target.py), "" reading as Default.
    expect(_value(authed_page, "copilot")).to_contain_text(cfg["copilot"]["model"] or "Default")
    expect(_value(authed_page, "gitlab")).to_have_text(
        " · ".join(p for p in (cfg["gitlab_group"], cfg["gitlab_host"]) if p) or "Not set"
    )
    expect(_value(authed_page, "terminal")).to_have_text(
        f"{cfg['terminal_history_lines']} lines · {cfg['large_upload_max_mb']} MB"
    )

    # Start at log on is the vendored switch, inline on its row.
    toggle = authed_page.locator("#paneSettings .settings-row--inline #bootAutostartToggle")
    expect(toggle).to_have_attribute("role", "switch")
    expect(toggle).to_have_class("toggle" if toggle.get_attribute("aria-checked") != "true" else "toggle on")


@pytest.mark.iphone
def test_every_sheet_opens_and_closes(authed_page: Page, base_url: str, auth_token: str) -> None:
    """The groups and rows render with the saved config's values, then every
    sheet opens and closes on the same page."""
    cfg = _config(base_url, auth_token)
    # The Voice group shows only in a Remote Desktop session; pin this PC's
    # session so the group list does not depend on how the box is reached.
    authed_page.route(
        "**/api/desktop-session",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"state": "local", "this_pc": true}'
        ),
    )
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings(authed_page)
    _assert_groups_render_with_values_matching_the_saved_config(authed_page, cfg)
    surface(authed_page, "settings.pane")

    for sheet_id in _SHEETS:
        sheet = authed_page.locator(f"#{sheet_id}")
        # Modal contract (#545): a header ✕ and one full-width primary.
        expect(sheet.locator(".detail-actions button")).to_have_count(1)
        expect(sheet.locator(".detail-actions .button-primary.detail-save-btn")).to_have_text("Done")
        open_settings_sheet(authed_page, sheet_id)
        surface(authed_page, f"settings.{_SURFACE_NAMES[sheet_id]}")
        sheet.locator("[data-sheet-back]").click()
        expect(sheet).to_be_hidden()
        open_settings_sheet(authed_page, sheet_id)
        sheet.locator(".detail-header .detail-close").click()
        expect(sheet).to_be_hidden()
        expect(authed_page.locator("#paneSettings")).to_be_visible()

    # Escape steps back out of a sheet opened from the pane.
    open_agent_sheet(authed_page)
    authed_page.keyboard.press("Escape")
    expect(authed_page.locator("dialog.settings-sheet[open]")).to_have_count(0)
    expect(authed_page.locator("#paneSettings")).to_be_visible()

    walk_speech_sheet(authed_page, base_url, auth_token)


def test_copilot_sheet_saves_a_model_and_shows_the_flags(
    authed_page: Page, base_url: str, auth_token: str
) -> None:
    """Settings › Coding agent: Default plus the pinned Copilot models, a pick
    POSTs ``copilot_model`` and the flags line follows the server's answer;
    Effort stands down on Default, where the server sends no ``--effort``.
    The GitLab sheet's two fields save as they change. The autouse fixture
    puts the config back."""
    page = authed_page
    cfg = _config(base_url, auth_token)
    models = cfg["copilot"]["models_available"]
    page.goto(base_url, wait_until="domcontentloaded")
    open_agent_sheet(page)
    options = page.locator("select#copilotModel > option")
    expect(options).to_have_text(["Default", *models])
    pick = models[-1]
    page.locator("#copilotModel").select_option(pick)
    expect(page.locator("#copilotFlagsPreview")).to_contain_text(pick)
    assert _config(base_url, auth_token)["copilot"]["model"] == pick
    expect(page.locator("#copilotEffort")).to_be_enabled()
    page.locator("#copilotModel").select_option("")
    expect(page.locator("#copilotEffort")).to_be_disabled()
    expect(page.locator("#copilotFlagsPreview")).not_to_contain_text("--model")
    assert _config(base_url, auth_token)["copilot"]["model"] == ""
    close_settings_sheets(page)

    walk_launch_presets(page, base_url, auth_token)

    open_settings_sheet(page, "gitlabSheet")
    group = page.locator("#gitlabGroup")
    group.fill("e2e-group/sub")
    group.dispatch_event("change")
    expect(page.locator("#toast")).to_contain_text("GitLab group saved.")
    assert _config(base_url, auth_token)["gitlab_group"] == "e2e-group/sub"
    close_settings_sheets(page)
    expect(_value(page, "gitlab")).to_contain_text("e2e-group/sub")

    walk_private_network_switches(page)


_OPEN_SETTINGS_AT_CALLERS = [
    # The Code tab's chief row kebab item (#1434; the Board's gear went
    # with its chief card in #1436). The menu is in the document only
    # while open (#1565), so the kebab comes first. It leads, while the
    # Code tab still shows.
    (("#sessionsList li.session-item-chief .session-kebab",
      ".session-menu [data-item=chief-settings-btn]"), "chiefSheet", None),
    (("#claudeEmptyAction",), "foldersSheet", "projectsDir"),
    (("#teamOsEmptyAction",), "foldersSheet", "teamOsDir"),
]


def test_no_save_button_is_left_and_open_settings_at_callers_land_in_their_sheet(
    authed_page: Page, base_url: str
) -> None:
    """Every Settings field saves as it changes (#1435), so no Save button is
    left. Then each ``openSettingsAt`` caller is clicked through its own
    listener (a dispatched click: the empty states only show when their list
    is empty), closing the sheet it opened before the next."""
    authed_page.goto(base_url, wait_until="domcontentloaded")
    scope = authed_page.locator("#paneSettings button, dialog.settings-sheet button")
    texts = [t.strip() for t in scope.all_text_contents()]
    assert not [t for t in texts if t == "Save"], texts
    expect(authed_page.locator("#saveSettings, #saveChiefSettings, #chiefSettingsDialog, #codingOptions")).to_have_count(0)

    for trigger, sheet_id, field_id in _OPEN_SETTINGS_AT_CALLERS:
        for selector in trigger:
            expect(authed_page.locator(selector)).to_be_attached()
            authed_page.locator(selector).dispatch_event("click")
        expect(authed_page.locator("#paneSettings")).to_be_visible()
        expect(authed_page.locator(f"#{sheet_id}")).to_be_visible()
        expect(authed_page.locator("dialog.settings-sheet[open]")).to_have_count(1)
        if field_id:
            expect(authed_page.locator(f"#{field_id}")).to_be_focused()
        close_settings_sheets(authed_page)


def _route_passkeys(page: Page) -> dict:
    """Answer the passkey routes from memory; returns what each one saw."""
    status = {
        "configured": True, "enrollment_open": True, "enrollment_seconds_left": 60,
        "devices": [{"id": "dev-1", "label": "Test phone", "added_at": "2026-01-01", "last_used": None}],
    }
    seen: dict = {"deletes": [], "begins": [], "native": []}
    page.route("**/api/webauthn/status", lambda route: route.fulfill(json=status))
    page.route(
        "**/api/webauthn/devices/*",
        lambda route: (seen["deletes"].append(route.request.method), route.fulfill(json={"ok": True})),
    )
    page.route(
        "**/api/webauthn/enroll/begin",
        lambda route: (seen["begins"].append(route.request.post_data_json),
                       route.fulfill(status=400, json={"detail": "stop here"})),
    )
    page.on("dialog", lambda d: (seen["native"].append(d.message), d.dismiss()))
    return seen


def _assert_passkeys_list_remove_and_enrol(page: Page, seen: dict) -> None:
    """Passkeys moved out of General into their own sheet (#1435): the row
    counts the devices, and remove and enrol still reach their endpoints."""
    expect(_value(page, "passkeys")).to_have_text("1 device")

    open_settings_sheet(page, "passkeysSheet")
    expect(page.locator("#webauthnDevices li")).to_have_count(1)
    page.locator("#webauthnDevices li .icon-button").click()
    # Remove and the device name ask in the themed dialog (#1501), not the
    # browser's confirm() / prompt().
    expect(page.locator("#confirmDialogTitle")).to_have_text('Remove passkey "Test phone"?')
    page.locator("#confirmDialogOk").click()
    expect(page.locator("#toast")).to_contain_text("Removed Test phone")
    assert seen["deletes"] == ["DELETE"]

    page.locator("#enrollDeviceBtn").click()
    expect(page.locator("#confirmDialogTitle")).to_have_text("Name this device")
    field = page.locator("#confirmDialogInput")
    expect(field).to_be_visible()
    expect(field).to_have_value("iPhone")
    field.fill("Second phone")
    page.locator("#confirmDialogOk").click()
    expect(page.locator("#toast")).to_contain_text("Enrollment failed")
    assert seen["begins"] == [{"label": "Second phone"}]
    assert seen["native"] == [], f"a native dialog was raised: {seen['native']}"
    close_settings_sheets(page)


@pytest.mark.iphone
def test_passkeys_sheet(authed_page: Page, base_url: str) -> None:
    """Passkeys remove and enrol through their own sheet."""
    page = authed_page
    passkeys = _route_passkeys(page)
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings(page)
    _assert_passkeys_list_remove_and_enrol(page, passkeys)


@pytest.mark.iphone
def test_chat_file_folders_add_remove_and_refuse(
    authed_page: Page, base_url: str, auth_token: str, tmp_path
) -> None:
    """Chat file folders (#1748): the Folders sheet lists the folders an agent
    may send Chat files from, adds one (live on the next send, no restart),
    removes it, says why a bad entry is refused and keeps the last row."""
    page = authed_page
    before = _config(base_url, auth_token)["outbox_scratch_roots"]
    drop = tmp_path / "Drop"
    drop.mkdir()
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings_sheet(page, "foldersSheet")
    rows = page.locator("#outboxRootsList li")
    expect(rows).to_have_count(len(before))

    field = page.locator("#outboxRootInput")
    field.fill(str(drop))
    page.locator("#outboxRootAdd").click()
    expect(rows).to_have_count(len(before) + 1)
    expect(rows.last.locator(".outbox-root-path")).to_have_text(str(drop))
    expect(field).to_have_value("")
    assert _config(base_url, auth_token)["outbox_scratch_roots"] == [*before, str(drop)]

    # No row carries a visible Remove; its kebab is a real 44px target.
    expect(rows.last.get_by_role("button", name=re.compile("remove", re.I))).to_have_count(0)
    kebab = rows.last.locator(".icon-button")
    box = kebab.bounding_box()
    assert box and box["width"] >= 44 - 0.5 and box["height"] >= 44 - 0.5, box

    # A refusal reads in the server's own words and saves nothing.
    field.fill(r"\\host\share")
    page.locator("#outboxRootAdd").click()
    expect(page.locator("#outboxRootInputError")).to_contain_text("network and device paths")
    field.fill(str(tmp_path / "never-made"))
    page.locator("#outboxRootAdd").click()
    expect(page.locator("#outboxRootInputError")).to_contain_text("does not exist")
    field.fill(str(drop))
    page.locator("#outboxRootAdd").click()
    expect(page.locator("#outboxRootInputError")).to_contain_text("already in the list")
    expect(rows).to_have_count(len(before) + 1)

    kebab.click()
    open_row_menu(page).locator("[data-item='outbox-remove-item']").click()
    expect(rows).to_have_count(len(before))
    assert _config(base_url, auth_token)["outbox_scratch_roots"] == before

    if len(before) == 1:
        # The last folder cannot be removed: an empty list reads as unset.
        rows.first.locator(".icon-button").click()
        expect(open_row_menu(page).locator("[data-item='outbox-remove-item']")).to_have_attribute(
            "aria-disabled", "true")

