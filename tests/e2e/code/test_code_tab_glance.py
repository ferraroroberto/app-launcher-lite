"""The Code tab's quieter Sessions list and header (issue #1434, step 2/7 of #1432).

The Code rows speak the Board's status vocabulary without polling the Board:
``/api/claude-code/sessions`` carries each session's Board column and status
(``board_column``, ``board_status``), and a session the Board files under
Your turn shows an attention "needs you" chip, a stalled one a danger
"stalled" chip. An unknown placement (``null``) shows no chip: unknown is
never turned into "needs you". The page header follows the header rule:
only the exceptions, each in its tone, else the plain session count, and
never the running-apps or git counts. The Usage card above the list draws
the Copilot month from ``/api/copilot-usage`` (bar, pace tick, percent,
reset date and pace words in their tone), and an unreadable plan figure
reads "unknown" with its reason, never 0%, while this machine's line stays.

Hermetic: the session list and the usage read are route-mocked before
``goto()`` (#510), with fictional sessions and numbers.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board.test_board_nav_badge import _session
from tests.e2e.fixtures.waits import stable_eval

pytestmark = pytest.mark.smoke


_CHIEF = _session("s-chief", "claude_turn", "awaiting-input", label="chief",
                  name="chief", manual_title="chief")
_WAITING = _session("s-wait", "your_turn", "awaiting-decision")
_STALLED = _session("s-stall", "your_turn", "stalled")
_WORKING = _session("s-work", "claude_turn", "working")
_UNKNOWN = _session("s-unknown", None, None)

_USAGE = {"allowance": 300, "pace_pct": 40.0, "cycle_start": "2026-11-01T00:00:00+00:00",
          "reset_at": "2026-12-01T00:00:00+00:00", "source": "a fictional log", "scope": "this machine"}
_USAGE_AHEAD = {**_USAGE, "state": "ok", "used_credits": 180.0, "today_credits": 4.0,
                "used_pct": 60.0, "reason": ""}
_USAGE_UNKNOWN = {**_USAGE, "state": "unknown", "used_credits": None, "today_credits": 2.0,
                  "used_pct": None, "reason": "the Copilot CLI has not cached your plan on this machine"}


def _mock(page: Page, sessions: list[dict], usage: dict = _USAGE_AHEAD) -> None:
    """Route-mock the list and the usage read; each route reads its body per
    request, so a test swaps the contents and reloads to show another one."""
    def json_route(pattern: str, body: dict) -> None:
        page.route(re.compile(pattern), lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)))
    json_route(r".*/api/claude-code/sessions$", {"sessions": sessions})
    json_route(r".*/api/claude-code/git-status$", {"projects": []})
    json_route(r".*/api/copilot-usage$", usage)


def _row(page: Page, sid: str):
    return page.locator(f'#sessionsList li[data-session-id="{sid}"]')


def test_rows_carry_the_boards_chips_and_the_header_falls_back_to_the_plain_count(
    authed_page: Page, base_url: str
) -> None:
    sessions = [_CHIEF, _WAITING, _STALLED, _WORKING, _UNKNOWN]
    usage = dict(_USAGE_AHEAD)
    _mock(authed_page, sessions, usage)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # The Usage card: 60% used with 40% of the month gone is ahead of pace.
    meter = authed_page.locator("#codingUsage .um-row")
    expect(meter.locator(".um-pct")).to_have_text("60%")
    expect(meter).to_have_attribute("data-tone", "attention")
    expect(meter.locator(".um-reset")).to_have_text("resets Dec 1")
    expect(meter.locator(".um-fill")).to_have_attribute("style", re.compile(r"width: 60%"))
    expect(meter.locator(".um-tick")).to_have_attribute("style", re.compile(r"left: 40%"))
    expect(authed_page.locator("#codingUsageMeta")).to_have_text("ahead of pace")
    expect(authed_page.locator("#codingUsageFooter")).to_have_text("4 credits today on this machine.")

    waiting = _row(authed_page, "s-wait").locator(".session-attention")
    expect(waiting).to_have_text("needs you")
    expect(waiting).to_have_attribute("data-tone", "attention")
    stalled = _row(authed_page, "s-stall").locator(".session-attention")
    expect(stalled).to_have_text("stalled")
    expect(stalled).to_have_attribute("data-tone", "danger")

    # A normal state gets no chip, and neither does an unknown one.
    for sid in ("s-chief", "s-work", "s-unknown"):
        expect(_row(authed_page, sid).locator(".session-attention")).to_have_count(0)

    # The header names the exceptions in their tones, nothing else.
    head = authed_page.locator("#homeHeadStatus")
    expect(head).to_have_text("1 stalled · 1 needs you")
    parts = head.locator(".head-exception")
    expect(parts).to_have_count(2)
    expect(parts.nth(0)).to_have_attribute("data-tone", "danger")
    expect(parts.nth(1)).to_have_attribute("data-tone", "attention")

    # Nothing waits on the user (one row's placement unknown): the plain
    # count, with no running-apps or git part (#1434).
    # The same reload finds the usage log unreadable: unknown, never 0%.
    sessions[:] = [_CHIEF, _WORKING, _UNKNOWN]
    usage.clear()
    usage.update(_USAGE_UNKNOWN)
    authed_page.reload(wait_until="domcontentloaded")
    expect(meter.locator(".um-pct")).to_have_text("unknown")
    expect(meter).to_have_attribute("data-tone", "none")
    expect(meter.locator(".um-fill")).to_have_count(0)
    expect(authed_page.locator("#codingUsageFooter")).to_have_text(
        "Plan usage unknown: the Copilot CLI has not cached your plan on this machine."
        " 2 credits today on this machine.")
    expect(head).to_have_text("3 sessions")
    expect(head.locator(".head-exception")).to_have_count(0)
    expect(head).not_to_contain_text("running")
    expect(head).not_to_contain_text("dirty")


@pytest.mark.iphone
def test_header_exceptions_and_a_stopped_chief_row_fit_at_390px(
    authed_page: Page, base_url: str
) -> None:
    """The header rule's two parts fit the phone's header line uncut, in their
    compact form beside the CPU reading, which never hides. With no chief
    running, its row's title and meta stay uncut beside the Start and Resume
    verbs (#1434)."""
    authed_page.set_viewport_size({"width": 390, "height": 844})
    sessions = [_CHIEF, _WAITING, _STALLED]
    _mock(authed_page, sessions)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    head = authed_page.locator("#homeHeadStatus")
    expect(head).to_have_text("1 stalled · 1 you")
    widths = stable_eval(
        head, "el => el.isConnected && el.clientWidth ? [el.clientWidth, el.scrollWidth] : null"
    )
    assert widths[1] <= widths[0], f"the header line is cut at 390px: {widths}"

    sessions[:] = [_WORKING]
    authed_page.reload(wait_until="domcontentloaded")
    row = authed_page.locator("#sessionsList .chief-stopped-row")
    expect(row.locator(".srow-title")).to_have_text("Chief")
    expect(row.locator(".srow-meta-text")).to_have_text("stopped")
    cut = stable_eval(row, """el => {
      if (!el.isConnected) return null;
      const parts = [el.querySelector('.srow-title'), el.querySelector('.srow-meta-text')];
      if (parts.some(p => !p || !p.clientWidth)) return null;
      return parts.filter(p => p.scrollWidth > p.clientWidth).map(p => p.textContent);
    }""")
    assert cut == [], f"the stopped chief row cuts {cut} at 390px"
