"""Leg of ``test_overlay_phone_composer.py``: a phone's Terminal view stays
whole when a PC browser opens the same session.

A desktop browser's in-page terminal sizes the shared PTY. A full-screen agent
then draws for that width, and the phone, still on its narrow grid, shows
scattered fragments while the PC renders fully. The phone has to take its size
back. The stub child's ``/fullscreen`` frame names the size it was drawn for in
every row, so a grid that disagrees with the PTY reads as wrong rows.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, Error, Page, expect

from tests.e2e.fixtures.ui import (
    RECORD_RESIZE_FRAMES,
    open_session_row,
    stub_session_mirror,
)
from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

# One sessions poll (5 s) notices the foreign size, then the agent repaints.
_RECLAIM_MS = 15000

# Init script: the page's one terminal, caught as xterm defines its global, so
# the bundled page needs no module import.
_CAPTURE = """(() => {
  let Terminal;
  Object.defineProperty(window, 'Terminal', {
    configurable: true,
    get: () => Terminal,
    set: (Real) => {
      Terminal = class extends Real {
        constructor(options) { super(options); window.__xterm = this; }
      };
    },
  });
})();"""

# Every visible row of the grid is the agent's row for exactly this grid.
_WHOLE = """() => {
  const t = window.__xterm;
  if (!t) return false;
  const b = t.buffer.active;
  for (let i = 0; i < t.rows; i++) {
    const line = b.getLine(b.viewportY + i);
    const want = 'row ' + String(i + 1).padStart(2, '0') + ' of ' + t.rows + 'x' + t.cols;
    if (!line || !line.translateToString(true).startsWith(want)) return false;
  }
  return true;
}"""

_ROWS = """(count) => {
  const b = window.__xterm.buffer.active;
  return Array.from({ length: count },
    (_, i) => b.getLine(b.viewportY + i).translateToString(true).trimEnd());
}"""

_GRID = "() => ({ rows: window.__xterm.rows, cols: window.__xterm.cols })"


def _open_terminal(page: Page, base_url: str, sid: str) -> None:
    stub_session_mirror(page)
    page.add_init_script(_CAPTURE)
    page.goto(base_url, wait_until="domcontentloaded")
    row = page.locator(f'#sessionsList li.session-item[data-session-id="{sid}"]')
    open_session_row(page, row, "terminal")
    expect(page.locator("#terminalHost .xterm-screen")).to_be_visible(
        timeout=OVERLAY_OPEN_MS
    )


def _expect_whole(page: Page, what: str) -> None:
    try:
        page.wait_for_function(_WHOLE, timeout=_RECLAIM_MS)
    except Error:
        grid = page.evaluate(_GRID)
        pytest.fail(f"{what}: grid {grid} shows {page.evaluate(_ROWS, grid['rows'])}")


def leg_phone_terminal_survives_a_pc_browser_joining(
    page: Page, browser: Browser, base_url: str, sid: str
) -> None:
    """``page`` is a phone (coarse pointer). Run last: the stub stays full-screen."""
    page.add_init_script(RECORD_RESIZE_FRAMES)
    page.set_viewport_size({"width": 390, "height": 760})
    _open_terminal(page, base_url, sid)
    page.locator("#terminalComposeBar .composer-input").fill("/fullscreen")
    page.locator("#terminalComposeBar .composer-send").click()
    _expect_whole(page, "phone alone")
    grid = page.evaluate(_GRID)
    sent = page.evaluate("window.__resizeFrames.length")

    pc =browser.new_context(ignore_https_errors=True, viewport={"width": 1600, "height": 900})
    try:
        desktop = pc.new_page()
        _open_terminal(desktop, base_url, sid)
        assert desktop.evaluate(_GRID)["cols"] > grid["cols"]
        # The phone's own size going out again is what says the PC's reached the
        # agent: before that its old, still whole frame would pass the check.
        try:
            page.wait_for_function(
                "(sent) => window.__resizeFrames.length > sent", arg=sent,
                timeout=_RECLAIM_MS,
            )
        except Error:
            pytest.fail(
                "the phone never took its size back: it shows "
                f"{page.evaluate(_ROWS, grid['rows'])}"
            )
        _expect_whole(page, "phone after a PC browser joined")
        assert page.evaluate(_GRID) == grid
        # The PC view shows the same frame, in the top of its larger grid.
        desktop.wait_for_function(
            "([want, count]) => window.__xterm.buffer.active"
            ".getLine(window.__xterm.buffer.active.viewportY + count - 1)"
            ".translateToString(true).startsWith(want)",
            arg=[f"row {grid['rows']:02d} of {grid['rows']}x{grid['cols']}", grid["rows"]],
            timeout=_RECLAIM_MS,
        )
        assert desktop.evaluate(_ROWS, grid["rows"]) == page.evaluate(_ROWS, grid["rows"])

        page.locator("#sessionModeChat").click()
        page.locator("#sessionModeTerminal").click()
        _expect_whole(page, "phone after Chat and back")
        page.locator("#terminalComposeBar .composer-input").fill(
            "\n".join(f"line {i}" for i in range(12))
        )
        _expect_whole(page, "phone with a grown composer")
    finally:
        pc.close()
