"""Regression pin for commit 142e2b4 / issue #28 (live terminal WS reconnect).

The bug: iOS aggressively suspends backgrounded PWAs; uvicorn's ping
timeout then closes the half-dead WebSocket and the overlay was left
frozen on "Disconnected." until the user manually re-opened the
session. The fix factors the ws setup into ``connectWs(t)`` and
re-runs it after non-final close codes with 1s/2s/4s/8s backoff.

This test exercises the JS half of the fix by:
  1. Wrapping ``window.WebSocket`` in an init script so the test can
     observe every WS the SPA opens (no product change).
  2. Opening the terminal via ``?terminal=<sid>`` deep-link.
  3. Force-closing the open WS from the page (code 1005). Per
     ``terminal.js:113-114`` this is the "iOS-suspend" path — same
     code reached when uvicorn's ping timeout fires.
  4. Asserting the SPA opens a fresh WS and that input sent on the
     fresh socket reaches ``webapp/sessions/<sid>.log``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e._flow import FLOW_TIMEOUT_S
from tests.e2e.terminal import (
    test_repaint_batch_reconnect_race as repaint_batch_reconnect_race,
)
from tests.e2e.terminal import test_shutdown_frame as shutdown_frame
from tests.e2e.terminal import test_terminal_native_scroll as terminal_native_scroll
from tests.e2e.terminal import test_warm_terminal_reopen as warm_terminal_reopen

pytestmark = pytest.mark.smoke

# Wrap WebSocket so we can see every instance the SPA constructs. Runs
# before any page script, so connectWs() in terminal.js uses the wrapped
# constructor without any code change.
_WS_PROBE = """
(() => {
  const orig = window.WebSocket;
  const instances = [];
  function Wrapped(...args) {
    const ws = new orig(...args);
    // First text frame per socket (#444): the reconnect-replay tests assert
    // the server's clear-frame preamble arrives in the same frame as the
    // ring snapshot. Registered here so no frame can slip by before the
    // SPA's own onmessage is assigned.
    ws.__firstMsg = null;
    ws.addEventListener('message', (ev) => {
      if (ws.__firstMsg === null && typeof ev.data === 'string') {
        ws.__firstMsg = ev.data.slice(0, 64);
      }
    });
    instances.push(ws);
    return ws;
  }
  Wrapped.prototype = orig.prototype;
  for (const k of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) {
    Wrapped[k] = orig[k];
  }
  window.WebSocket = Wrapped;
  window.__wsInstances = instances;
})();
"""


def scenario_terminal_reconnects_after_ws_drop(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    wait_for_session_log,
) -> None:
    sid = launched_pty_session
    authed_page.add_init_script(_WS_PROBE)
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")

    # Wait for the first WS to reach OPEN. The deep-link path calls
    # openTerminal automatically after fetchSessions resolves.
    authed_page.wait_for_function(
        "() => window.__wsInstances && window.__wsInstances.length >= 1 "
        "&& window.__wsInstances[0].readyState === 1",
        timeout=10_000,
    )

    # Force-drop the live socket. close() with no args fires onclose with
    # code 1005, which terminal.js routes to scheduleReconnect (line 113).
    authed_page.evaluate("window.__wsInstances.at(-1).close()")

    # Reconnect budget in the SPA is 30s with 1s/2s/4s/8s backoff. Allow
    # 15s here — comfortably inside the first two backoff cycles.
    authed_page.wait_for_function(
        "() => window.__wsInstances.length >= 2 "
        "&& window.__wsInstances.at(-1).readyState === 1",
        timeout=15_000,
    )

    # Send a recognisable string on the new socket and confirm it lands
    # in the per-session log. This proves the reconnect carried real
    # I/O, not just a TCP handshake.
    marker = "rec0nn3ct-marker-32\n"
    authed_page.evaluate(
        "(text) => window.__wsInstances.at(-1).send("
        "JSON.stringify({ type: 'input', data: text }))",
        marker,
    )

    # Session log is buffered; poll for a couple of seconds.
    assert wait_for_session_log(authed_page, sid, "rec0nn3ct-marker-32"), (
        f"input sent on the reconnected ws did not appear in "
        f"webapp/sessions/{sid}.log — the reconnect handshake succeeded but "
        "the new ws isn't carrying input"
    )


# --------------------------------------------------------------- issue #610

# A WebSocket that opens (after a tick, same async shape as a real socket)
# but never sends a single frame — the exact condition the #610 first-paint
# watchdog exists to catch. Deliberately not the real session-host: this
# isolates the pure client-side timer logic (arm on open, never disarmed
# because nothing ever arrives) from server behavior, which is what the fix
# actually is — the root cause on the server side was never pinned down.
_SILENT_WS = """
(() => {
  class SilentWs extends EventTarget {
    constructor(url) {
      super();
      this.url = url;
      this.readyState = 0;
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      this.onclose = null;
      window.__silentWs = this;
      window.__silentWsCount = (window.__silentWsCount || 0) + 1;
      setTimeout(() => {
        this.readyState = 1;
        if (this.onopen) this.onopen({});
      }, 0);
    }
    send() {}
    close() {
      if (this.readyState === 3) return;
      this.readyState = 3;
      if (this.onclose) this.onclose({ code: 1000, reason: '', wasClean: true });
    }
  }
  SilentWs.CONNECTING = 0; SilentWs.OPEN = 1; SilentWs.CLOSING = 2; SilentWs.CLOSED = 3;
  window.WebSocket = SilentWs;
})();
"""


def scenario_terminal_watchdog_fires_when_nothing_ever_paints(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
) -> None:
    """#610: a terminal that opens a WS but receives no frame at all within
    the watchdog window must surface an explicit, actionable status — never
    stay silently blank forever. Real wall-clock wait (no virtual clock):
    installing one globally from page load risks freezing unrelated page
    timers (polling loops, the #374 first-fit defer) that this test has no
    business touching — the ~8s cost is in the same order as this file's
    other real-timing waits."""
    sid = launched_pty_session
    authed_page.add_init_script(_SILENT_WS)
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")

    # Confirm the fake socket actually reached OPEN (proves ws.onopen ran,
    # which is what arms the watchdog) before waiting out its window.
    # Generous bound: page boot + the terminal-token fetch run before the
    # socket is created, and a starved runner stretches both well past 10 s
    # (reproduced under CPU burn, #58). It is a ceiling, not a delay.
    authed_page.wait_for_function(
        "() => window.__silentWs && window.__silentWs.readyState === 1",
        timeout=45_000,
    )

    # Past PAINT_WATCHDOG_MS (8000ms in terminal-connection.js) with margin.
    authed_page.wait_for_function(
        "() => { const s = document.getElementById('terminalStatus'); "
        "return s && !s.hidden && s.textContent.indexOf('No response yet') !== -1; }",
        timeout=11_000,
    )

    # And it must be recoverable — tapping it starts a fresh connect attempt
    # (a new WebSocket instance), not a dead end. Assert the new-socket fact
    # itself, not the transient 'Connecting…' status text: the tap handler
    # sets that text synchronously but ws.onopen clears it again one macrotask
    # later, so its visible window is only as long as the terminal-token fetch
    # — reliably observable on a fast dev box, reliably MISSED on the slower
    # CI runner (two deterministic CI reds, 2026-08-02).
    count_before = authed_page.evaluate("() => window.__silentWsCount")
    authed_page.locator("#terminalStatus").click()
    authed_page.wait_for_function(
        "(prev) => window.__silentWsCount > prev"
        " && window.__silentWs && window.__silentWs.readyState === 1",
        arg=count_before,
        timeout=10_000,
    )


@pytest.mark.timeout(FLOW_TIMEOUT_S)
def test_terminal_session_flow(run_flow) -> None:
    """A terminal's session life: reconnect after a drop, the first-paint watchdog, repaint races, shutdown frames, warm reopen and native scroll.

    Each scenario below ran as its own test before; ``run_flow`` gives each a fresh
    browser context and reports every scenario that fails, not just the first.
    """
    run_flow(
        scenario_terminal_reconnects_after_ws_drop,
        scenario_terminal_watchdog_fires_when_nothing_ever_paints,
        repaint_batch_reconnect_race.scenario_reconnect_repaint_batch_discards_stale_state,
        shutdown_frame.scenario_route_frame_classifies_shutdown_vs_output,
        warm_terminal_reopen.scenario_warm_reopen_reuses_terminal_and_ws,
        terminal_native_scroll.scenario_native_touch_scroll_routing,
    )
