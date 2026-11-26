"""The terminal bar's ⋮ session menu and the floating Latest pill.

The bar carries a ⋮ menu (Rename · Copy link · Stop and kill — the kill path
itself is pinned by ``test_stop_unify_and_terminal_kill.py``) and a pill over
the terminal that only appears while scrolled up.

These pin:

* Copy link writes the session's provider link (the server's ``web_url``),
  else this launcher's own ``?session=<sid>`` deep link with a toast saying it
  only works while the launcher runs.
* Rename from the menu updates the bar title at once, not on the next poll.
* The Latest pill is hidden at the tail and within a screen of it, visible
  once scrolled further up, and a tap returns to the tail and hides it.

The overlay is opened with ``?session=`` rather than ``?terminal=``: the
latter marks a PC mirror window, and neither the menu nor the pill depends on
which, so ``?session=`` runs the same in-page overlay on both projections.

One test opens the overlay once and runs the pill leg, then the menu checks.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._surface import surface
from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

pytestmark = [pytest.mark.smoke, pytest.mark.unbundled]

# Placeholder enterprise host and synthetic ids: the real host is the
# machine's Copilot sign-in, and a stub child has no task and no Claude card,
# so the one field it cannot produce is stamped onto the real list response.
_COPILOT_WEB_URL = "https://ghe.example.com/copilot/tasks/0d4b6f52-1c7e-4a39-b2a8-6e5f9c3d1a77"
_CLAUDE_WEB_URL = "https://claude.ai/code/session_SYNTHETICLINKFIXTURE0682"

_CLIPBOARD_MOCK = """
(() => {
  window.__copied = [];
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {
      writeText: async (text) => { window.__copied.push(text); },
      readText: async () => '',
    },
  });
})()
"""

# The SPA loads its modules cache-busted (`state.js?v=<asset_hash>`); a bare
# import would evaluate a second, empty module instance. Resolve the page's
# real module URL so the import shares the live state (same pattern as
# test_terminal_reconnect.py).
_LIVE_STATE_SETUP = """
async () => {
  const hit = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/state.js?v='));
  const { state } = await import(hit || '/static/state.js');
  window.__state = state;
  window.__term = () => state.terminal && state.terminal.term;
}
"""

# Count frames each terminal socket delivers. The session-host opens every
# PTY stream with a clear frame that erases scrollback (`ESC[3J`,
# server.py _CLEAR_FRAME), so scrollback written before that frame lands is
# wiped — which is what a loaded gate run hit when the connect was slow.
_WS_FRAME_PROBE = """
(() => {
  const Orig = window.WebSocket;
  window.__wsFrames = 0;
  function Wrapped(...args) {
    const ws = new Orig(...args);
    ws.addEventListener('message', () => { window.__wsFrames += 1; });
    return ws;
  }
  Wrapped.prototype = Orig.prototype;
  for (const k of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) Wrapped[k] = Orig[k];
  window.WebSocket = Wrapped;
})();
"""

# The connect's frames have arrived and the repaint batch has written them.
_FRAMES_WRITTEN = (
    "() => window.__wsFrames > 0 && !!window.__term()"
    " && window.__term().element.style.visibility !== 'hidden'"
)

# Settle past the pill's once-per-frame coalescing before reading it.
_TWO_FRAMES = (
    "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"
)

def _inject_web_url(page: Page, sid: str, row: dict) -> None:
    """Stamp ``row`` onto this session in the real sessions list; the dict is
    read on every response, so a leg can switch it."""

    def _handler(route):
        response = route.fetch()
        body = response.json()
        for session in body.get("sessions", []):
            if session.get("session_id") == sid:
                session.update(row)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route(re.compile(r".*/api/claude-code/sessions$"), _handler)


def _copy_link(page: Page) -> None:
    menu = page.locator(".terminal-menu")
    page.locator("#terminalMenu").click()
    expect(menu).to_be_visible()
    page.get_by_role("menuitem", name="Copy session link").click()
    expect(menu).to_be_hidden()


def _leg_copies_the_provider_link(page: Page, row: dict, agent: str, url: str, name: str) -> None:
    """Both the poll's source and the page's current copy switch, so a poll
    landing mid-leg agrees with the page."""
    row.update(agent=agent, web_url=url)
    page.evaluate(
        "([agent, url]) => { const v = window.__state.sessionView.session;"
        " for (const s of [v, ...window.__state.sessions]) {"
        " if (s.session_id === v.session_id) { s.agent = agent; s.web_url = url; } } }",
        [agent, url],
    )
    done = page.evaluate("() => window.__copied.length")
    _copy_link(page)
    page.wait_for_function("(n) => window.__copied.length === n", arg=done + 1, timeout=5_000)
    assert page.evaluate("() => window.__copied.at(-1)") == url
    expect(page.locator("#toast")).to_have_text(re.compile(f"{name} web link copied"))


def _open_overlay(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?session={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)


def _leg_latest_pill_shows_only_when_scrolled_up(authed_page: Page) -> None:
    authed_page.evaluate(_LIVE_STATE_SETUP)
    authed_page.wait_for_function("() => !!window.__term()", timeout=OVERLAY_OPEN_MS)
    # Only write once the connect's clear + replay frame is in and written:
    # the repaint batch holds them until it reveals the canvas.
    authed_page.wait_for_function(_FRAMES_WRITTEN, timeout=OVERLAY_OPEN_MS)
    authed_page.evaluate(_TWO_FRAMES)
    pill = authed_page.locator("#terminalLatest")

    # Several screens of local scrollback (written into xterm, not the PTY).
    authed_page.evaluate(
        "() => new Promise((r) => window.__term().write("
        "Array.from({length: 400}, (_, i) => 'latest-pill line ' + i).join('\\r\\n')"
        " + '\\r\\n', r))"
    )
    authed_page.evaluate("() => window.__term().scrollToBottom()")
    authed_page.evaluate(_TWO_FRAMES)
    expect(pill).to_be_hidden()

    # A couple of lines up is still "at the latest output": no pill.
    authed_page.evaluate("() => window.__term().scrollLines(-2)")
    authed_page.evaluate(_TWO_FRAMES)
    assert authed_page.evaluate("() => document.getElementById('terminalLatest').hidden"), (
        "pill appeared less than a screen above the tail"
    )

    authed_page.evaluate("() => window.__term().scrollToTop()")
    expect(pill).to_be_visible()

    pill.click()
    expect(pill).to_be_hidden()
    at_tail = authed_page.evaluate(
        "() => { const b = window.__term().buffer.active; return b.viewportY === b.baseY; }"
    )
    assert at_tail, "tapping Latest did not return the terminal to its tail"


def test_menu_copy_link_and_rename_update_the_bar(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    sid = launched_pty_session
    authed_page.add_init_script(_CLIPBOARD_MOCK)
    authed_page.add_init_script(_WS_FRAME_PROBE)
    row = {"agent": "copilot", "web_url": _COPILOT_WEB_URL}
    _inject_web_url(authed_page, sid, row)
    _open_overlay(authed_page, base_url, sid)

    _leg_latest_pill_shows_only_when_scrolled_up(authed_page)
    surface(authed_page, "terminal.overlay")

    _leg_copies_the_provider_link(authed_page, row, "copilot", _COPILOT_WEB_URL, "Copilot")
    _leg_copies_the_provider_link(authed_page, row, "claude", _CLAUDE_WEB_URL, "Claude")
    row.update(agent="copilot", web_url="")

    authed_page.locator("#terminalMenu").click()
    expect(authed_page.locator(".terminal-menu")).to_be_visible()
    surface(authed_page, "terminal.session-menu")
    authed_page.get_by_role("menuitem", name="Rename session").click()
    expect(authed_page.locator("#sessionRenameDialog")).to_be_visible()
    surface(authed_page, "terminal.rename-dialog")
    authed_page.locator("#sessionRenameInput").fill("Menu rename 981")
    authed_page.locator("#sessionRenameForm button[type='submit']").click()
    expect(authed_page.locator("#sessionRenameDialog")).to_be_hidden()
    # Shorter than the title poll (SESSIONS_POLL_MS), so a pass means the
    # rename callback updated the bar, not a later sessions fetch.
    expect(authed_page.locator("#terminalTitle")).to_have_text(
        "Menu rename 981", timeout=2_000
    )

    # Last, because it mutates the page's own state: the bare ?session=
    # deep-link shape, where the sessions list does not carry the row and the
    # overlay holds a bare {session_id, name} with no web_url. It must land on
    # the launcher link, never on `undefined`, and say what that link is.
    authed_page.evaluate(
        "(sid) => { window.__state.sessions = [];"
        " window.__state.sessionView.session = {session_id: sid, name: sid}; }",
        sid,
    )
    _copy_link(authed_page)
    authed_page.wait_for_function("() => window.__copied.length === 3", timeout=5_000)
    fallback = authed_page.evaluate("() => window.__copied[2]")
    expected = authed_page.evaluate(
        "(sid) => location.origin + location.pathname + '?session=' + sid", sid
    )
    assert fallback == expected, f"copied {fallback!r}, expected the ?session= link {expected!r}"
    expect(authed_page.locator("#toast")).to_have_text(
        re.compile("Launcher link copied: works only while the launcher runs")
    )
