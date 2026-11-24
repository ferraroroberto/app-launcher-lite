"""Settings tab (issue #383).

Settings moved from an always-visible collapsible card at the bottom of
every tab into a sixth navigation tab. Contract under test:

  * Settings has no tab since #1131: every page header's gear shows
    ``#paneSettings`` with the settings controls and hides the other panes,
    and choosing any tab leaves it.
  * The settings card no longer bleeds into the other tabs — on the
    default Coding tab the panel is hidden.
  * The app theme toggle lives on the Coding tab (issue #392 moved it out
    of the Settings pane; #496 moved it into the home-head summary card's
    toggle slot) and still flips ``html[data-theme]``.

Issue #435 follow-up adds the terminal-scrollback-depth setting and removes
the TLS-badge / tunnel-URL status readout (needless exposure of the tunnel
hostname in the UI) — both covered below.

Issue #719 made the Settings cards collapsible disclosure cards; #1435
turned them into rows that each open a modal sheet (``dialog.settings-sheet``)
with no Save button — fields save as they change. Tests below open the sheet
holding a field before asserting on it, and close it before tapping anything
outside it (an open modal sheet makes the rest of the page inert).
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import (
    close_settings_sheets,
    open_settings,
    open_settings_sheet,
)
from tests.e2e.fixtures.waits import (
    BUILD_READOUT_MS,
    boot_retrying_static_failure,
    flush_requests,
)

pytestmark = pytest.mark.smoke

_SHEETS = [
    "copilotSheet", "chiefSheet", "gitlabSheet", "tokensSheet", "foldersSheet",
    "passkeysSheet", "terminalSheet",
]


def _is_config_post(req) -> bool:
    return req.method == "POST" and req.url.endswith("/api/config")


def _load_with_flaky_version(authed_page: Page, base_url: str) -> None:
    """Load the app with /api/version failing twice and check the build readout recovers."""
    # #1287: the build readout survives a transient /api/version failure.
    # The first two requests fail; the retry's third fills the readout.
    version_calls = []

    def _flaky_version(route) -> None:
        version_calls.append(route.request.url)
        if len(version_calls) <= 2:
            route.abort("connectionreset")
        else:
            route.continue_()

    authed_page.route(re.compile(r".*/api/version$"), _flaky_version)
    authed_page.goto(base_url, wait_until="domcontentloaded")
    expect(authed_page.locator("#buildReadout")).to_contain_text(
        "Build:", timeout=BUILD_READOUT_MS
    )
    assert len(version_calls) == 3, version_calls


def _assert_switch_shape_and_status_readout(authed_page: Page) -> None:
    """Check the vendored switch geometry and that the status readout leaks no TLS or tunnel text."""
    # -- was test_settings_boolean_controls_use_vendored_switch --
    # Settings booleans use the fleet switch track + sliding thumb; Start at
    # log on stays inline on the pane (#1435), no sheet needed.
    for selector in ("#bootAutostartToggle",):
        switch = authed_page.locator(selector)
        expect(switch).to_have_class(re.compile(r"(?:^|\s)toggle(?:\s|$)"))
        expect(switch.locator(".knob")).to_have_count(1)
        box = switch.bounding_box()
        assert box is not None
        assert round(box["width"]) == 44
        assert round(box["height"]) == 26

    # -- was test_settings_status_readout_has_no_tls_or_tunnel_url --
    # The TLS badge + tunnel-URL status line was removed (issue #435
    # follow-up) — needless exposure of the tunnel hostname in the UI. Any
    # reachability warning may still render; TLS/tunnel text must not. It
    # lives in the passkeys sheet now; text_content reads a closed dialog.
    readout = authed_page.locator("#statusReadout")
    text = (readout.text_content() or "").lower()
    assert "tls" not in text
    assert "tunnel" not in text
    assert "http" not in text


def _assert_no_internal_jargon(authed_page: Page) -> None:
    """Read the pane and every sheet and check no internal term reaches the rendered text."""
    # #1238 J-07: the pane and every sheet speak plain language. None of the
    # internal terms it used to show ("Harness support", "Fleet config
    # folder", bearer/minted tokens, the hook's shadow/rewrite modes, issue
    # numbers) may reach the rendered text. A closed dialog renders nothing
    # (#1435), so each sheet is opened in turn and read while it shows.
    shown_parts = [authed_page.locator("#paneSettings").inner_text()]
    for sheet_id in _SHEETS:
        open_settings_sheet(authed_page, sheet_id)
        shown_parts.append(authed_page.locator(f"#{sheet_id}").inner_text())
        close_settings_sheets(authed_page)
    shown = "\n".join(shown_parts)
    jargon = [m.group(0) for m in re.finditer(
        r"\b(?:harness\w*|fleet config|bearer|minted|mint|telemetry|hook|"
        r"shadow|rewrite|pretooluse|middleware)\b|#\d+",
        shown, flags=re.IGNORECASE,
    )]
    assert not jargon, f"internal terms in Settings: {sorted(set(jargon))}"


@pytest.mark.iphone
def test_settings_pane_controls_and_theme_toggle(
    authed_page: Page, base_url: str
) -> None:
    """Every read-only Settings-pane check on one page load (#1215).

    Merged from six tests that each did the same goto: pane routing, the
    panel's absence from the other tabs, the switch shape, the status readout,
    and — last, since it is the only step that mutates page state — the
    Coding-tab theme toggle's flip.
    """
    _load_with_flaky_version(authed_page, base_url)

    # -- was test_theme_toggle_lives_on_coding_tab_and_flips_theme (part 1) --
    toggle = authed_page.locator("#themeToggle")
    # Lives in the home-head card on the Coding tab (#496) — visible on load.
    expect(toggle).to_be_visible()

    # -- was test_settings_panel_absent_from_other_tabs (part 1) --
    # Default tab is Coding — the settings panel must not bleed through.
    expect(authed_page.locator("#paneSettings")).to_be_hidden()

    # -- was test_settings_tab_opens_pane_with_controls --
    authed_page.locator(".pane:not([hidden]) .settings-open-btn").click()
    expect(authed_page.locator("#paneSettings")).to_be_visible()
    expect(authed_page.locator("#paneClaude")).to_be_hidden()
    # No tab is selected while Settings shows (#1131: it has no tab).
    expect(authed_page.locator("nav.tabs .tab[aria-selected='true']")).to_have_count(0)
    expect(authed_page.locator("nav.tabs .tab.active")).to_have_count(0)
    expect(authed_page.locator("nav.tabs")).not_to_have_attribute("data-active-tab", re.compile(".*"))

    # -- was test_theme_toggle_lives_on_coding_tab_and_flips_theme (part 2) --
    # Not duplicated into the Settings pane.
    expect(toggle).to_be_hidden()

    # (test_settings_tab_opens_pane_with_controls, continued)
    # Each Settings sheet is closed until its row is tapped (issue #719,
    # #1435) — its fields are hidden, and there is no Save button: a field
    # saves as it changes.
    expect(authed_page.locator("#projectsDir")).to_be_hidden()
    open_settings_sheet(authed_page, "foldersSheet")
    expect(authed_page.locator("#projectsDir")).to_be_visible()
    expect(authed_page.locator("#saveSettings")).to_have_count(0)
    close_settings_sheets(authed_page)

    _assert_switch_shape_and_status_readout(authed_page)

    _assert_no_internal_jargon(authed_page)

    # -- was test_settings_panel_absent_from_other_tabs (part 2) --
    # (every sheet is closed above — an open one would leave the nav inert)
    authed_page.locator("#tabApps").click()
    expect(authed_page.locator("#paneSettings")).to_be_hidden()

    # -- was test_theme_toggle_lives_on_coding_tab_and_flips_theme (part 3, last:
    # the only step that mutates page state) --
    authed_page.locator("#tabClaude").click()
    expect(toggle).to_be_visible()

    before = authed_page.evaluate(
        "document.documentElement.dataset.theme || 'light'"
    )
    toggle.click()
    after = authed_page.evaluate(
        "document.documentElement.dataset.theme || 'light'"
    )
    assert after != before


def _assert_a_failed_patch_does_not_report_success(authed_page: Page) -> None:
    """Regression for #832: the save handler used to discard `patchConfig`'s
    verdict and always toast success, so a rejected save (POST /api/config
    400) showed the red "Save failed" toast and then immediately overwrote
    it with a green success toast — reporting a failed save as succeeded.
    `patchConfig` already documents that callers must branch on its resolved
    boolean; this proves the per-field save (#1435) now does."""
    field = authed_page.locator("#terminalHistoryLines")
    expect(field).not_to_have_value("")

    authed_page.route(
        "**/api/config",
        lambda route: (
            route.fulfill(
                status=400,
                content_type="application/json",
                body='{"detail": "terminal_history_lines out of range"}',
            )
            if route.request.method == "POST"
            else route.continue_()
        ),
    )

    # A value that differs from the stored one, so the field fires `change`.
    field.fill("7700" if field.input_value() != "7700" else "7800")
    with authed_page.expect_response(lambda r: _is_config_post(r.request)):
        field.press("Tab")
    toast = authed_page.locator("#toast")
    expect(toast).to_contain_text("Save failed")
    expect(authed_page.locator("#terminalHistoryLinesError")).to_be_visible()
    expect(field).to_have_attribute("aria-invalid", "true")

    # The pre-fix code's unconditional success toast would fire right after the
    # failed save settles (a promise chain, no timer) and overwrite the failure
    # toast; on a fix the handler returns early on a falsy patchConfig(). A
    # request sent now is answered after that chain has run.
    flush_requests(authed_page)
    expect(toast).to_contain_text("Save failed")
    expect(toast).not_to_contain_text("saved.")


def test_terminal_history_lines_field_loads_saves_and_reports_a_failed_save(
    authed_page: Page, base_url: str
) -> None:
    """The scrollback-depth field (issue #435 follow-up) loads pre-filled
    from GET /api/config and a new value survives leaving the field (it saves
    on blur, #1435 — no Save button) and a page reload. On the reloaded page
    a refused save then reports the failure and never a success."""
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings_sheet(authed_page, "terminalSheet")
    field = authed_page.locator("#terminalHistoryLines")
    expect(field).to_be_visible()
    # Pre-filled from the server default, not left blank.
    expect(field).not_to_have_value("")

    # A value that differs from the stored one, so the field fires `change`.
    new_value = "5000" if field.input_value() != "5000" else "5100"
    field.fill(new_value)
    # patchConfig() round-trips through GET /api/config on success.
    with authed_page.expect_response(lambda r: _is_config_post(r.request)):
        field.press("Tab")
    expect(authed_page.locator("#toast")).to_contain_text("Terminal history saved.")
    expect(field).to_have_value(new_value)

    # Reload to confirm it actually persisted server-side, not just DOM.
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings_sheet(authed_page, "terminalSheet")
    expect(authed_page.locator("#terminalHistoryLines")).to_have_value(new_value)

    _assert_a_failed_patch_does_not_report_success(authed_page)


def test_boot_autostart_and_lid_keep_awake_switches(
    authed_page: Page, base_url: str
) -> None:
    """The inline switches on the Settings pane (#1435: no sheet).

    "Start App Launcher Lite at log on" (issue #456 part 1/2) is a real
    filesystem side effect (a Startup-folder wrapper bat), not a plain config
    field — click it on, reload, and confirm the switch survives from a fresh
    GET /api/config (not just the optimistic click); then click it off and
    confirm the same across a reload. It ends on the lid keep-awake switch's
    states (``_assert_lid_keep_awake_states``)."""
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings(authed_page)
    toggle = authed_page.locator("#bootAutostartToggle")
    expect(toggle).to_be_visible()
    expect(toggle).to_have_attribute("aria-checked", "false")

    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings(authed_page)
    expect(authed_page.locator("#bootAutostartToggle")).to_have_attribute(
        "aria-checked", "true"
    )

    authed_page.locator("#bootAutostartToggle").click()
    expect(authed_page.locator("#bootAutostartToggle")).to_have_attribute(
        "aria-checked", "false"
    )
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings(authed_page)
    expect(authed_page.locator("#bootAutostartToggle")).to_have_attribute(
        "aria-checked", "false"
    )

    _assert_lid_keep_awake_states(authed_page, base_url)


def _assert_lid_keep_awake_states(authed_page: Page, base_url: str) -> None:
    """The lid switch paints /api/config's live `lid_keep_awake` block: hidden
    on a PC with no lid, `mixed` with the reason when unreadable, never off.
    Both /api/config and the POST are answered here, so no run can reach a
    real power plan."""
    lid = {"state": "unsupported", "reason": "This PC has no lid close setting."}
    posts = []

    def _config(route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        body = route.fetch().json()
        body["lid_keep_awake"] = dict(lid)
        route.fulfill(json=body)

    def _post(route) -> None:
        posts.append(route.request.post_data_json)
        if lid["state"] == "unknown":
            route.fulfill(status=400, json={"detail": "Windows power settings refused the request."})
            return
        lid.update(state="on" if posts[-1]["enabled"] else "off", reason=None)
        route.fulfill(json={"ok": True, "lid_keep_awake": dict(lid)})

    authed_page.route("**/api/config", _config)
    authed_page.route("**/api/settings/lid-keep-awake", _post)
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings(authed_page)
    row = authed_page.locator("#lidKeepAwakeRow")
    toggle = authed_page.locator("#lidKeepAwakeToggle")
    expect(authed_page.locator("#bootAutostartToggle")).to_be_visible()
    expect(row).to_be_hidden()

    lid.update(state="off", reason=None)
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings(authed_page)
    expect(row).to_be_visible()
    expect(toggle).to_have_attribute("aria-checked", "false")
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    assert posts == [{"enabled": True}]
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")
    assert posts[-1] == {"enabled": False}

    lid.update(state="unknown", reason="The lid close setting could not be read.")
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings(authed_page)
    expect(toggle).to_have_attribute("aria-checked", "mixed")
    expect(toggle.locator(".toggle-label")).to_have_text("UNKNOWN")
    status = authed_page.locator("#lidKeepAwakeStatus")
    expect(status).to_contain_text("could not be read")
    toggle.click()
    expect(status).to_have_text("Windows power settings refused the request.")
    expect(toggle).to_have_attribute("aria-checked", "mixed")


def _open_chief_sheet(page: Page, base_url: str) -> None:
    """Boot the page and open Settings' Chief sheet (#1613).

    ``tabs.js`` wires the header gear, so a load of it that fails under gate
    load leaves a page whose gear taps do nothing for its whole life; no wait
    recovers that, so the first failed ``/static/`` load gets one fresh boot.
    """
    boot_retrying_static_failure(
        page, lambda: page.goto(base_url, wait_until="domcontentloaded")
    )
    open_settings_sheet(page, "chiefSheet")



# (toggle id, config key, toast label, a word its help line carries, default)
_CHIEF_SWITCHES = [
    ("chiefAutoDrain", "chief_auto_drain", "Auto-drain queue", "worker slot", True),
    ("chiefDoorbell", "chief_doorbell", "Chief doorbell", "mailbox", False),
    ("chiefJobSelfHeal", "chief_job_self_heal", "Job self-heal", "repair a Job", False),
    ("chiefWeeklyProposals", "chief_weekly_proposals", "Weekly proposals", "weekly proposals", False),
]


def _state(on: bool) -> str:
    return "true" if on else "false"


def _assert_chief_switches_save_flip_and_reload(page: Page, base_url: str, posts: list, saving) -> None:
    """Each Chief switch sits at its default, saves at once through the
    ordinary /api/config POST, survives a reload and flips back to the
    default so the shared webapp is left as found. The doorbell doubles as
    the phone kill switch; the chief reads Auto-drain queue as
    ``auto_drain``. The gate's disposable webapp is not canonical, so its
    doorbell never rings."""
    _open_chief_sheet(page, base_url)
    for toggle, key, label, help_word, default in _CHIEF_SWITCHES:
        switch = page.locator(f"#{toggle}Toggle")
        expect(switch).to_have_attribute("aria-checked", _state(default))
        expect(page.locator(f"#{toggle}Help")).to_contain_text(help_word)
        saving(switch.click)
        expect(switch).to_have_attribute("aria-checked", _state(not default))
        expect(page.locator("#toast")).to_contain_text(f"{label} {'off' if default else 'on'}")
        assert posts[-1] == {key: not default}, (toggle, posts)

    _open_chief_sheet(page, base_url)
    for toggle, key, label, _help, default in _CHIEF_SWITCHES:
        switch = page.locator(f"#{toggle}Toggle")
        expect(switch).to_have_attribute("aria-checked", _state(not default))
        saving(switch.click)
        expect(switch).to_have_attribute("aria-checked", _state(default))
        expect(page.locator("#toast")).to_contain_text(f"{label} {'on' if default else 'off'}")
        assert posts[-1] == {key: default}, (toggle, posts)

    _open_chief_sheet(page, base_url)
    for toggle, _key, _label, _help, default in _CHIEF_SWITCHES:
        expect(page.locator(f"#{toggle}Toggle")).to_have_attribute("aria-checked", _state(default))


def test_chief_sheet_switches_and_auto_compact_threshold_save_and_reload(
    authed_page: Page, base_url: str
) -> None:
    """Settings' Chief sheet, every control on one page.

    #1298: the sheet sets the chief's auto-compact threshold
    (fleet-config#1052), through the ordinary /api/config save. It loads the
    default 30, refuses a value outside 10-90 with a plain message and saves
    nothing, saves and reloads a valid one, and its switch turns the
    feature off by storing 0 (the field then disabled). The field saves on
    blur (#1435), so validation runs when it loses focus."""
    page = authed_page
    posts: list = []
    page.on("request", lambda req: posts.append(req.post_data_json)
            if _is_config_post(req) else None)

    def open_sheet() -> None:
        _open_chief_sheet(page, base_url)

    def saving(act) -> None:
        """Run ``act`` and wait for its save's round-trip to land.

        The sheet repaints only after patchConfig's POST and the GET that
        follows it, so assertions on the repaint wait for that GET, not for
        a 5 s DOM poll a loaded webapp outlasted (#1346: a ~7 s whole-server
        stall left the switch unflipped when the budget ran out).
        """
        with page.expect_response(
            lambda r: r.url.endswith("/api/config") and r.request.method == "GET"
        ):
            act()

    open_sheet()
    field = page.locator("#chiefAutoCompactThreshold")
    switch = page.locator("#chiefAutoCompactToggle")
    expect(switch).to_have_attribute("aria-checked", "true")
    expect(field).to_have_value("30")
    expect(field).to_be_enabled()

    field.fill("5")
    field.press("Tab")
    expect(page.locator("#toast")).to_contain_text("between 10 and 90")
    expect(field).to_have_attribute("aria-invalid", "true")
    assert posts == [], posts

    field.fill("45")
    saving(lambda: field.press("Tab"))
    expect(page.locator("#toast")).to_contain_text("Chief auto-compact at 45%")
    assert posts[-1] == {"chief_auto_compact_threshold": 45}, posts

    open_sheet()
    expect(field).to_have_value("45")
    expect(switch).to_have_attribute("aria-checked", "true")

    saving(switch.click)
    expect(switch).to_have_attribute("aria-checked", "false")
    expect(field).to_be_disabled()
    expect(page.locator("#toast")).to_contain_text("Chief auto-compact off")
    assert posts[-1] == {"chief_auto_compact_threshold": 0}, posts

    open_sheet()
    expect(switch).to_have_attribute("aria-checked", "false")
    expect(field).to_be_disabled()

    # Back on at the default, so the shared webapp is left as found.
    saving(switch.click)
    expect(switch).to_have_attribute("aria-checked", "true")
    expect(field).to_be_enabled()
    assert posts[-1] == {"chief_auto_compact_threshold": 30}, posts

    # The fallback wake: 30 by default, explained, 0 saves as off, and it is
    # put back so the shared webapp is left as found.
    fallback = page.locator("#chiefFallbackMinutes")
    expect(fallback).to_have_value("30")
    expect(page.locator("#chiefFallbackHelp")).to_contain_text("missed doorbell")
    fallback.fill("0")
    saving(lambda: fallback.press("Tab"))
    expect(page.locator("#toast")).to_contain_text("Chief fallback check off")
    assert posts[-1] == {"chief_fallback_minutes": 0}, posts
    open_sheet()
    expect(fallback).to_have_value("0")
    fallback.fill("30")
    saving(lambda: fallback.press("Tab"))
    assert posts[-1] == {"chief_fallback_minutes": 30}, posts

    _assert_chief_switches_save_flip_and_reload(page, base_url, posts, saving)
