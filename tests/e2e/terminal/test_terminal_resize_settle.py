"""Regression pin for issue #930 — a keyboard sweep must not storm the PTY.

A Copilot terminal is full-screen: once sized, a shrinking box (keyboard up)
pans the canvas and sends nothing, and only a box that outgrows the canvas
grows the rows. Every resize frame repaints the agent, so the frame that does
go out is settled, and floored (an iOS keyboard sweep samples 1- and 6-row
sizes that are never a real viewport). The keyboard can't be raised headlessly,
so this drives the same code path by resizing the terminal host synchronously
and firing ``resize`` — an event ``applySize()`` listens on — then reads the
resize frames the page actually put on the WebSocket. It pins:

* a first size taken in a tiny box is clamped before it is sent;
* nothing is sent mid-sweep;
* a sweep that returns to where it started, or ends smaller, sends nothing;
* a smaller box pans the canvas;
* a sweep that ends bigger sends exactly one settled frame, with more rows.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e import _fake_clock
from tests.e2e.fixtures.ui import RECORD_RESIZE_FRAMES, open_session_row
from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

# terminal.js RESIZE_SETTLE_MS (300), plus one: the clock jump that lets a
# settled frame go out. The settle is a plain setTimeout, so the tests jump the
# fake clock (#1376, #1515) instead of waiting 1.5 s of real time per step — a
# settle that grew past this, or was dropped, turns the assertions below red.
_AFTER_SETTLE_MS = 301


# Resize the terminal host through each height synchronously, firing the
# window 'resize' applySize() is bound to after every step — the same shape as
# a keyboard sweep's burst of visualViewport events. Returns how many frames
# went out *during* the sweep, once the page's own ResizeObserver on the host
# has run: the fake clock does not drive it, and a delivery landing mid-jump
# re-arms the settle past the jump, so a frame still to come reads as none.
_SWEEP = r"""
async (heights) => {
  const host = document.getElementById('terminalHost');
  const before = window.__resizeFrames.length;
  for (const h of heights) {
    host.style.flex = '0 0 ' + h + 'px';
    window.dispatchEvent(new Event('resize'));
  }
  const sent = window.__resizeFrames.length - before;
  // Created after the app's observer, so it is delivered after it.
  await new Promise((done) => {
    const seen = new ResizeObserver(() => { seen.disconnect(); done(); });
    seen.observe(host);
  });
  return sent;
}
"""

# How far the canvas is translated up, in px.
_PANNED_PX = r"""
() => -new DOMMatrix(getComputedStyle(
  document.querySelector('#terminalHost .xterm')).transform).m42
"""


def _frames(page: Page) -> list:
    return page.evaluate("window.__resizeFrames.slice()")


def _clear(page: Page) -> None:
    page.evaluate("window.__resizeFrames.length = 0")


def test_keyboard_sweep_settles_to_one_floored_resize(
    authed_page: Page, base_url: str, browser_name: str, launched_pty_session: str
) -> None:
    if browser_name != "webkit":
        pytest.skip(
            "the in-page terminal is phone-only since #282 — the desktop "
            "row-tap opens a PC mirror window, which never sends resize frames"
        )
    page = authed_page
    _fake_clock.install(page)
    page.add_init_script(RECORD_RESIZE_FRAMES)
    # Open via the session-list row — the phone path. The ?terminal= deep link
    # classifies a loopback open as the PC mirror window (#241), which never
    # sends resize frames at all.
    page.goto(base_url, wait_until="domcontentloaded")
    row = page.locator(
        f'#sessionsList li.session-item[data-session-id="{launched_pty_session}"]'
    )
    # The first size is taken with the host squeezed under the floor.
    squeeze = page.add_style_tag(content="#terminalHost { flex: 0 0 10px !important }")
    open_session_row(page, row, "terminal")
    # The phone's authoritative first size goes out on WS open, unsettled.
    page.wait_for_function(
        "window.__resizeFrames.length >= 1", timeout=OVERLAY_OPEN_MS
    )
    min_rows = page.evaluate("import('/static/terminal.js').then(m => m.PTY_MIN_ROWS)")
    first = _frames(page)[0]
    assert min_rows and first["rows"] == min_rows, (
        f"a 10px host sent rows={first['rows']} (floor {min_rows!r}) — a "
        "sub-viewport size reached the PTY"
    )

    # Establish a known tall baseline and let it settle out.
    squeeze.evaluate("el => el.remove()")
    _clear(page)
    page.evaluate(_SWEEP, [600])
    _fake_clock.advance(page, _AFTER_SETTLE_MS)
    baseline = _frames(page)
    assert baseline, "the terminal never grew to its 600px baseline"
    _clear(page)

    # 1. Keyboard up and back down: nothing mid-sweep, nothing after it.
    sent_mid_sweep = page.evaluate(_SWEEP, [300, 90, 300, 600])
    assert sent_mid_sweep == 0, (
        f"{sent_mid_sweep} resize frame(s) went out mid-sweep — every "
        "intermediate keyboard sample repaints the agent (issue #930)"
    )
    _fake_clock.advance(page, _AFTER_SETTLE_MS)
    assert _frames(page) == [], (
        f"a sweep that returned to its starting size sent {_frames(page)!r}"
    )

    # 2. Keyboard up and staying up: the canvas pans, the PTY is not resized.
    page.evaluate(_SWEEP, [300, 90, 300])
    _fake_clock.advance(page, _AFTER_SETTLE_MS)
    assert _frames(page) == [], f"a shrink resized the PTY: {_frames(page)!r}"
    # A 600px canvas in a 300px box: the pan covers the lost height.
    panned = page.evaluate(_PANNED_PX)
    assert panned > 250, f"a 300px box panned only {panned}px"

    # 3. A box bigger than before: one frame, once it settles.
    sent_mid_sweep = page.evaluate(_SWEEP, [300, 90, 800])
    assert sent_mid_sweep == 0, f"{sent_mid_sweep} resize frame(s) went out mid-sweep"
    _fake_clock.advance(page, _AFTER_SETTLE_MS)
    grown = _frames(page)
    assert len(grown) == 1 and grown[0]["rows"] > baseline[-1]["rows"], (
        f"expected one settled frame above {baseline[-1]!r}, got {grown!r}"
    )
