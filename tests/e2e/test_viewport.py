"""Sanity-check that the phone projection actually applies the Android descriptor.

Confirms `browser_context_args` in conftest.py merged in
`playwright.devices["Pixel 8 Pro"]` — without this, the phone-projection run
would silently use a desktop viewport and test_smoke.py wouldn't actually
be exercising an Android-shaped target (issue #6).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

# Matches playwright.devices["Pixel 8 Pro"]["viewport"]["width"].
_PIXEL_8_PRO_WIDTH = 448


def test_android_viewport_active_on_phone_projection(
    authed_page: Page, base_url: str, phone_projection: bool
) -> None:
    if not phone_projection:
        pytest.skip("Android projection only applies to the phone leg")
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    width = authed_page.evaluate("window.innerWidth")
    assert width == _PIXEL_8_PRO_WIDTH, (
        f"expected Pixel 8 Pro width {_PIXEL_8_PRO_WIDTH}, got {width} — "
        "the device descriptor merge in conftest.py didn't take effect"
    )
