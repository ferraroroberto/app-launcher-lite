"""Settings > Appearance: Full screen on Edge and Install as app.

A browser tab keeps its address bar and toolbar on a phone, and a managed
phone's browser will not install a page served with a certificate it does not
trust. Settings carries the fleet's vendored "Full screen on Edge" row (shown
only in Edge, kept per device, re-entered on the first tap after a reload,
left by the switch) and an Install as app row that says whether the browser can
install Lite and why not.

Both checks share one page load and so one node. Runs on both projections:
whether the engine offers full screen at all is part of what is under test, so
the test reads it and asserts the matching contract (a working switch, or a
row that stays hidden).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import open_settings
from tests.e2e.fixtures.waits import wait_until

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_ROW = "#fullscreenRow"
_SWITCH = "#fullscreenRow [data-fullscreen-toggle]"
_KEY = "app-launcher.fullscreen"
_IN_FULLSCREEN = "!!document.fullscreenElement"
_SUPPORTED = "!!document.fullscreenEnabled"
# The row is shown only in Edge, which the test cannot run on, so the page is
# told it is Edge before any script reads the user agent.
_EDGE_UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36 EdgA/130.0"
_AS_EDGE = f"Object.defineProperty(navigator, 'userAgent', {{get: () => '{_EDGE_UA}'}});"


def _stored(page: Page):
    return page.evaluate(f"localStorage.getItem('{_KEY}')")


def _wait_fullscreen(page: Page, want: bool) -> None:
    wait_until(
        page,
        lambda: page.evaluate(_IN_FULLSCREEN) is want,
        f"document full screen to be {want}",
    )


def _open(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    open_settings(page)


def _install_row_says_where_it_stands(page: Page) -> None:
    row = page.locator("#installReadinessRow")
    value = page.locator("#installReadinessValue")
    sentence = page.locator("#installReadinessStatus")

    # Whatever this browser can do, the row never stays blank or silent.
    expect(value).not_to_have_text("")
    expect(sentence).not_to_have_text("")
    assert row.get_attribute("data-install-state") in {
        "installed", "blocked", "offered", "unsupported", "not-offered",
    }

    # The browser's own signal flips it to Ready, and an install to Installed.
    page.evaluate("window.dispatchEvent(new Event('beforeinstallprompt'))")
    if row.get_attribute("data-install-state") != "installed":
        expect(row).to_have_attribute("data-install-state", "offered")
        expect(value).to_have_text("Ready")
    page.evaluate("window.dispatchEvent(new Event('appinstalled'))")
    expect(row).to_have_attribute("data-install-state", "installed")
    expect(value).to_have_text("Installed")


def test_full_screen_and_install_readiness(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _open(page, base_url)
    page.evaluate(f"localStorage.removeItem('{_KEY}')")
    _install_row_says_where_it_stands(page)

    # Any browser that is not Edge never shows the row.
    expect(page.locator(_ROW)).to_be_hidden()

    page.add_init_script(_AS_EDGE)
    page.reload(wait_until="domcontentloaded")
    open_settings(page)
    switch = page.locator(_SWITCH)

    if not page.evaluate(_SUPPORTED):
        # An engine with no full screen for web pages keeps the row hidden
        # even in Edge: it never shows a switch that does nothing.
        expect(page.locator(_ROW)).to_be_hidden()
        return

    # In Edge the row is there, off, with no "tap anywhere" sentence yet.
    expect(page.locator(_ROW)).to_be_visible()
    expect(page.locator(_ROW)).to_contain_text("Full screen on Edge")
    expect(switch).to_have_attribute("aria-checked", "false")
    expect(page.locator(f"{_ROW} .fullscreen-armed")).to_be_hidden()

    # The switch enters full screen and remembers the choice.
    switch.click()
    _wait_fullscreen(page, True)
    expect(switch).to_have_attribute("aria-checked", "true")
    assert _stored(page) == "1"

    # The back gesture is not a choice to turn it off: the setting stays on
    # and says the next tap goes back in.
    page.evaluate("document.exitFullscreen()")
    _wait_fullscreen(page, False)
    expect(switch).to_have_attribute("aria-checked", "true")
    expect(page.locator(f"{_ROW} .fullscreen-armed")).to_be_visible()
    assert _stored(page) == "1"

    # Turning it off with the switch clears the choice and leaves full screen.
    page.locator(_SWITCH).click()
    expect(switch).to_have_attribute("aria-checked", "false")
    assert _stored(page) is None
    _wait_fullscreen(page, False)

    # A stored choice survives a reload, which leaves full screen; the switch
    # stays on and the first tap anywhere enters again.
    page.evaluate(f"localStorage.setItem('{_KEY}', '1')")
    page.reload(wait_until="domcontentloaded")
    expect(switch).to_have_attribute("aria-checked", "true")
    assert page.evaluate(_IN_FULLSCREEN) is False
    open_settings(page)  # the gear tap is the first tap anywhere
    _wait_fullscreen(page, True)
    page.evaluate("document.exitFullscreen()")
