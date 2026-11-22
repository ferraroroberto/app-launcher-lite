"""Settings > Appearance: Full screen and Install as app.

A browser tab keeps its address bar and toolbar on a phone, and a managed
phone's browser will not install a page served with a certificate it does not
trust. Settings now carries a Full screen switch (Fullscreen API, kept per
device, re-entered on the first tap after a reload, left by the switch or the
back gesture) and an Install as app row that says whether the browser can
install Lite and why not.

Both checks share one page load and so one node. Runs on both projections: whether the engine offers full screen at all is
part of what is under test, so the test reads it and asserts the matching
contract (a working switch, or a disabled one that says why).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import open_settings
from tests.e2e.fixtures.waits import wait_until

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_SWITCH = "#fullscreenToggle"
_KEY = "launcher.fullscreen"
_IN_FULLSCREEN = "!!(document.fullscreenElement || document.webkitFullscreenElement)"
_SUPPORTED = "!!(document.fullscreenEnabled || document.webkitFullscreenEnabled)"


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
    switch = page.locator(_SWITCH)
    expect(switch).to_be_visible()

    if not page.evaluate(_SUPPORTED):
        # A browser with no full screen for web pages says so; it never shows
        # a switch that looks live and does nothing.
        expect(switch).to_be_disabled()
        expect(page.locator("#fullscreenStatus")).to_contain_text("doesn't offer full screen")
        return

    # The switch enters full screen and remembers the choice.
    expect(switch).to_have_attribute("aria-checked", "false")
    switch.click()
    _wait_fullscreen(page, True)
    expect(switch).to_have_attribute("aria-checked", "true")
    assert _stored(page) == "1"

    # The back gesture (an exit the page did not ask for) is a choice: the
    # switch turns off and the next reload does not pull the person back in.
    page.evaluate("document.exitFullscreen()")
    _wait_fullscreen(page, False)
    expect(switch).to_have_attribute("aria-checked", "false")
    assert _stored(page) is None

    # Turning it on, then off with the switch, leaves full screen too.
    switch.click()
    _wait_fullscreen(page, True)
    switch.click()
    _wait_fullscreen(page, False)
    expect(switch).to_have_attribute("aria-checked", "false")
    assert _stored(page) is None

    # A stored choice survives a reload, which leaves full screen; the switch
    # stays on, says a tap re-enters, and the first tap anywhere does.
    page.evaluate(f"localStorage.setItem('{_KEY}', '1')")
    page.reload(wait_until="domcontentloaded")
    expect(switch).to_have_attribute("aria-checked", "true")
    expect(page.locator("#fullscreenStatus")).to_contain_text("Tap anywhere")
    assert page.evaluate(_IN_FULLSCREEN) is False
    open_settings(page)  # the gear tap is the first tap anywhere
    _wait_fullscreen(page, True)
    expect(page.locator("#fullscreenStatus")).to_be_hidden()
    page.evaluate("document.exitFullscreen()")
