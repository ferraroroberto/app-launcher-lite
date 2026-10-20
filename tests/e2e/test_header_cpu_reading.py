"""Regression pin for #65: the Coding header's CPU reading and PC load sheet.

The home-head carries a small CPU reading left of the theme toggle: a muted
number at rest, attention after a sustained high, danger when pegged, "–"
when the server could not read it (never "0%"). One tap opens a sheet with
total CPU, memory and the busiest processes. The poll is one per page and
stops while the page is hidden.

The level is the server's (tests/test_system_load.py pins the rules); this
pins what the header draws for each, the sheet, and the hidden-page gate.
``/api/system/load`` is route-mocked with invented data before ``goto()``.
A foreground return (visibilitychange) reads at once, so the tests drive the
reading with that instead of waiting out the 10 s poll.

Runs in both projections: the reading must not break the one-row header at
phone width.
"""
from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _reading(level: str, cpu, reason=None) -> dict:
    known = cpu is not None
    return {
        "level": level,
        "reason": reason,
        "cpu_percent": cpu,
        "memory": {"used_bytes": 48 * 1024**3, "total_bytes": 64 * 1024**3, "percent": 75.0}
        if known else None,
        "top": [
            {"name": "python (pytest)", "cores": 3.1, "count": 1, "owner": None},
            {"name": "node (server)", "cores": 1.0, "count": 1, "owner": None},
            {"name": "builder", "cores": 0.42, "count": 1, "owner": None},
        ] if known else [],
        "other_cores": None,
        "cores": 16 if known else None,
        "age_seconds": 1.0 if known else None,
    }


def _route_load(page: Page, current: dict, calls: list) -> None:
    def handle(route):
        calls.append(1)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(current["body"]))

    page.route(re.compile(r".*/api/system/load$"), handle)


def _set_hidden(page: Page, hidden: bool) -> None:
    page.evaluate(
        """(hidden) => {
          Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
          document.dispatchEvent(new Event('visibilitychange'));
        }""",
        hidden,
    )


def _foreground_read(page: Page) -> None:
    """Return to the foreground: the page reads the load once at once."""
    with page.expect_response(lambda r: r.url.endswith("/api/system/load")):
        _set_hidden(page, False)


def test_header_shows_the_reading_and_its_four_states(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    current = {"body": _reading("normal", 42.4)}
    _route_load(page, current, [])
    page.goto(f"{base_url}/", wait_until="domcontentloaded")

    reading = page.locator("#paneCoding > .home-head .cpu-reading")
    expect(reading).to_have_text("42%")
    expect(reading).to_have_attribute("data-level", "normal")

    # The reading sits between the status line and the theme toggle, and the
    # one-row header neither wraps nor overflows with it.
    boxes = page.locator("#paneCoding > .home-head").evaluate(
        """(head) => {
          const r = (sel) => head.querySelector(sel).getBoundingClientRect();
          const h = head.getBoundingClientRect();
          return { head: h.height, overflow: head.scrollWidth - head.clientWidth,
                   statusRight: r('#homeHeadStatus').right, cpuLeft: r('.cpu-reading').left,
                   cpuRight: r('.cpu-reading').right, toggleLeft: r('#themeToggle').left };
        }"""
    )
    assert boxes["head"] <= 60, f"the header wrapped ({boxes['head']}px)"
    assert boxes["overflow"] <= 0, f"the header overflows by {boxes['overflow']}px"
    assert boxes["statusRight"] <= boxes["cpuLeft"], "the reading overlaps the status line"
    assert boxes["cpuRight"] <= boxes["toggleLeft"], "the reading is not left of the toggle"

    muted = reading.evaluate("el => getComputedStyle(el).color")

    # Attention: the server says the high has held for ~30 s.
    current["body"] = _reading("attention", 76.0)
    _foreground_read(page)
    expect(reading).to_have_text("76%")
    expect(reading).to_have_attribute("data-level", "attention")
    attention = reading.evaluate("el => getComputedStyle(el).color")
    assert attention != muted, "attention must ring in its tone, not stay muted"

    # Danger: pegged.
    current["body"] = _reading("danger", 97.0)
    _foreground_read(page)
    expect(reading).to_have_text("97%")
    expect(reading).to_have_attribute("data-level", "danger")
    danger = reading.evaluate("el => getComputedStyle(el).color")
    assert danger not in (muted, attention), "danger must be its own tone"

    # Unknown: a dash, never 0%.
    current["body"] = _reading("unknown", None, reason="error")
    _foreground_read(page)
    expect(reading).to_have_text("–")
    expect(reading).to_have_attribute("data-level", "unknown")

    # Back to normal: the colour goes quiet again.
    current["body"] = _reading("normal", 12.0)
    _foreground_read(page)
    expect(reading).to_have_text("12%")
    expect(reading).to_have_attribute("data-level", "normal")


def test_the_tap_sheet_lists_cpu_memory_and_the_top_three(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _route_load(page, {"body": _reading("danger", 93.0)}, [])
    page.goto(f"{base_url}/", wait_until="domcontentloaded")

    reading = page.locator("#paneCoding > .home-head .cpu-reading")
    expect(reading).to_have_text("93%")
    reading.click()
    sheet = page.locator("#cpuSheet")
    expect(sheet).to_be_visible()
    body = sheet.locator("#cpuSheetBody")
    expect(body.locator("[data-row='cpu']")).to_contain_text("93% of 16 cores")
    expect(body.locator("[data-row='cpu'] .cpu-sheet-value")).to_have_attribute("data-tone", "danger")
    expect(body.locator("[data-row='memory']")).to_contain_text("48 of 64 GB (75%)")
    expect(body.locator("[data-row^='top-']")).to_have_count(3)
    expect(body.locator("[data-row='top-0']")).to_contain_text("python (pytest)")
    expect(body.locator("[data-row='top-0']")).to_contain_text("3.1 cores")
    expect(body.locator("[data-row='top-1']")).to_contain_text("1 core")
    # Names only: the sheet never carries a path.
    assert not re.search(r"[A-Za-z]:\\|/usr/|/home/", body.inner_text())
    page.locator("#cpuSheetDone").click()
    expect(sheet).to_be_hidden()


def test_the_sheet_groups_a_burn_and_says_why_a_reading_is_unknown(
    authed_page: Page, base_url: str
) -> None:
    """A group is one row with its count (never "×1" for a lone process),
    "Everything else" closes the gap to the CPU line, and an unreadable
    sample says why in the sheet."""
    page = authed_page
    grouped = _reading("danger", 100.0)
    grouped["top"] = [
        {"name": "python", "cores": 11.2, "count": 16, "owner": "some-repo-wt-1"},
        {"name": "vmmemWSL", "cores": 1.6, "count": 1, "owner": None},
        {"name": "MsMpEng", "cores": 0.9, "count": 1, "owner": None},
    ]
    grouped["other_cores"] = 2.3  # 16.0 - 13.7
    current = {"body": grouped}
    _route_load(page, current, [])
    page.goto(f"{base_url}/", wait_until="domcontentloaded")

    page.locator("#paneCoding > .home-head .cpu-reading").click()
    body = page.locator("#cpuSheetBody")
    first = body.locator("[data-row='top-0']")
    expect(first).to_contain_text("python ×16 · some-repo-wt-1")
    expect(first).to_contain_text("11.2 cores")
    for lone in ("top-1", "top-2"):
        expect(body.locator(f"[data-row='{lone}']")).not_to_contain_text("×")
    expect(body.locator("[data-row='other']")).to_contain_text("Everything else")
    expect(body.locator("[data-row='other']")).to_contain_text("2.3 cores")
    # The remainder is the last row, after the processes it completes.
    expect(body.locator("[data-row]").last).to_have_attribute("data-row", "other")

    # An open sheet follows the next reading: a stale sample says why.
    current["body"] = _reading("unknown", None, reason="stale")
    _foreground_read(page)
    expect(body.locator("[data-row='cpu']")).to_contain_text("Last reading is too old")
    expect(body.locator("[data-row^='top-']")).to_have_count(0)
    page.locator("#cpuSheetClose").click()
    expect(page.locator("#cpuSheet")).to_be_hidden()


def test_a_hidden_page_does_not_read_the_load(authed_page: Page, base_url: str) -> None:
    page = authed_page
    calls: list = []
    _route_load(page, {"body": _reading("normal", 20.0)}, calls)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(page.locator("#paneCoding > .home-head .cpu-reading")).to_have_text("20%")

    # Going hidden asks for nothing, however often the page is nudged.
    seen = len(calls)
    _set_hidden(page, True)
    _set_hidden(page, True)
    page.wait_for_timeout(500)
    assert len(calls) == seen, f"read the load {len(calls) - seen}x while hidden"

    # Back in the foreground: exactly one read, at once.
    _foreground_read(page)
    assert len(calls) == seen + 1
