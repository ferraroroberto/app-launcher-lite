"""Regression pin for #60: the Screen view, against a stub frame source.

The Settings tab's Screen row opens the view full screen in the page (lite
has no desktop pop-out). The work PC's three displays list as a picker named
by where each sits, the view draws the stub's frame and acks it, a pick
reconnects to the other display, and the x closes it.

Nothing here touches the real screen: ``/api/screen/monitors`` and the
``/api/screen/ws`` socket are routed before navigation, and the frame is a
tiny invented JPEG.
"""
from __future__ import annotations

import io
import json
import re
import time
from typing import Callable, Dict, List

import pytest
from PIL import Image
from playwright.sync_api import Page, WebSocketRoute, expect

pytestmark = pytest.mark.smoke

# The work PC's layout: primary, one to its right, one above and left of it
# at a negative origin (virtual desktop 4330x2160 from (-490, -1080)).
_MONITORS = {
    "monitors": [
        {"id": 0, "left": 0, "top": 0, "width": 1920, "height": 1080, "primary": True},
        {"id": 1, "left": 1920, "top": 0, "width": 1920, "height": 1080, "primary": False},
        {"id": 2, "left": -490, "top": -1080, "width": 1920, "height": 1080, "primary": False},
    ]
}
_FRAME_SIZE = (160, 90)


def _frame() -> bytes:
    img = Image.new("RGB", _FRAME_SIZE, (40, 90, 160))
    img.paste((240, 240, 240), (10, 10, 70, 40))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _stub_screen(page: Page) -> Dict[str, List]:
    seen: Dict[str, List] = {"urls": [], "acks": []}
    page.route(
        re.compile(r".*/api/screen/monitors$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_MONITORS)
        ),
    )

    def handler(ws: WebSocketRoute) -> None:
        seen["urls"].append(ws.url)
        ws.on_message(lambda msg: seen["acks"].append(msg) if msg == "ack" else None)
        ws.send(_frame())

    page.route_web_socket(re.compile(r".*/api/screen/ws.*"), handler)
    return seen


def _wait_for(page: Page, done: Callable[[], bool], what: str, timeout_s: float = 5.0) -> None:
    """Pump Playwright's event loop until ``done()``; the route handlers above
    record into Python lists, which only fill while the page is being driven."""
    deadline = time.monotonic() + timeout_s
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        page.wait_for_timeout(50)


def test_settings_opens_a_live_view_of_the_chosen_display(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    seen = _stub_screen(page)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#tabSettings").click()
    row = page.locator("#screenOpen")
    expect(row).to_be_visible()
    row.click()

    dialog = page.locator("#screenView")
    expect(dialog).to_be_visible()
    image = page.locator("#screenImage")
    expect(image).to_be_visible()
    _wait_for(page, lambda: len(seen["acks"]) >= 1, "the frame's ack")
    assert page.evaluate("document.getElementById('screenImage').naturalWidth") == _FRAME_SIZE[0]
    assert "monitor=0" in seen["urls"][0], "no pick yet: the primary"
    expect(page.locator("#screenState")).to_be_hidden()

    # The view fills the window and nothing (the nav included) sits over it.
    geo = dialog.evaluate("""(el) => {
      const r = el.getBoundingClientRect();
      const nav = document.querySelector('nav.tabs').getBoundingClientRect();
      const x = Math.min(innerWidth - 1, Math.max(0, nav.left + nav.width / 2));
      const y = Math.min(innerHeight - 1, Math.max(0, nav.top + nav.height / 2));
      const hit = document.elementFromPoint(x, y);
      return { w: r.width, h: r.height, vw: innerWidth, vh: innerHeight,
               navSpotInView: !!hit && el.contains(hit) };
    }""")
    assert abs(geo["w"] - geo["vw"]) <= 1 and abs(geo["h"] - geo["vh"]) <= 1, geo
    assert geo["navSpotInView"], "the nav must not sit over the view"

    # Every display as a picker, named by where it sits; the upper-left one
    # (negative origin) reconnects.
    picker = page.locator("#screenPicker .segmented-item")
    expect(picker).to_have_text(["Main", "Right", "Above"])
    expect(picker.nth(0)).to_have_attribute("aria-pressed", "true")
    picker.nth(2).click()
    _wait_for(page, lambda: len(seen["urls"]) == 2, "the reconnect")
    assert "monitor=2" in seen["urls"][1]
    expect(picker.nth(2)).to_have_attribute("aria-pressed", "true")
    expect(image).to_be_visible()
    _wait_for(page, lambda: len(seen["acks"]) >= 2, "the second frame's ack")

    page.locator("#screenViewClose").click()
    expect(dialog).to_be_hidden()
