"""Regression pin for #1522: the page header's CPU reading.

Every tab's header carries a small CPU reading left of the theme toggle: a
muted number at rest, attention after a sustained high, danger when pegged,
"–" when the server could not read it (never "0%"). One tap opens a sheet
with total CPU, memory and the busiest three processes. The poll is one per
page, never per tab, and stops while the page is hidden.

The level is the server's (tests/unit/usage/test_system_load.py pins the rules); this
pins what the header draws for each, the sheet, and the hidden-page gate.
``/api/system/load`` is route-mocked with invented data before ``goto()``
and the poll is jumped on the fake clock (#1376), never waited out. The hub's
machine health beside it (``/api/system/health``) is mocked the same way: the dot
for watch or act, the Health and machine rows, a verdict that could not be read as
"Not confirmed" with its reason, and the 30 s ask on the load poll.
"""
from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.code.test_code_tab_glance import _STALLED, _WAITING
from tests.e2e.code.test_code_tab_glance import _mock as _mock_sessions
from tests.e2e.fixtures.waits import (
    boot_retrying_static_failure,
    flush_requests,
    stable_eval,
)

pytestmark = pytest.mark.smoke

_POLL_MS = 10_000

_TABS = {
    "#tabClaude": "#paneClaude",
    "#tabApps": "#paneApps",
    "#tabJobs": "#paneJobs",
    "#tabTeamOS": "#paneTeamOS",
    "#tabBoard": "#paneBoard",
}


def _reading(level: str, cpu, reason=None) -> dict:
    known = cpu is not None
    return {
        "level": level,
        "reason": reason,
        "cpu_percent": cpu,
        "memory": {"used_bytes": 48 * 1024**3, "total_bytes": 64 * 1024**3, "percent": 75.0}
        if known else None,
        "top": [
            {"name": "python (pytest)", "cores": 3.1},
            {"name": "node (server)", "cores": 1.0},
            {"name": "builder", "cores": 0.42},
        ] if known else [],
        "cores": 16 if known else None,
        "age_seconds": 1.0 if known else None,
    }


def _route_load(page: Page, current: dict, calls: list) -> None:
    def handle(route):
        calls.append(1)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(current["body"]))

    page.route(re.compile(r".*/api/system/load$"), handle)


_IP = "192.168.1.20"


def _route_ip(page: Page, ip: dict) -> None:
    """Mock the two reads of the PC's private IP with invented data.

    ``/api/status`` is fetched for real and only ``private_ip`` is replaced;
    the refresh answers from the same mutable dict, ``discovery_ok`` false
    when the address is None (a read that found nothing).
    """

    def status(route):
        body = route.fetch().json()
        body["private_ip"] = ip["value"]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def refresh(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"private_ip": ip["value"], "discovery_ok": ip["value"] is not None}))

    page.route(re.compile(r".*/api/status$"), status)
    page.route(re.compile(r".*/api/status/refresh$"), refresh)


def _boot(page: Page, base_url: str) -> None:
    """Open the page, and open it again if a ``/static/`` file failed (#1610).

    The reading's markup default is ``–`` / ``data-level="unknown"``; only
    ``system-load.js`` running ever changes it. A module that fails to load
    under gate load stops ``main.js`` outright, so the header stays at its
    default for the page's life and no wait recovers it.
    """
    boot_retrying_static_failure(
        page, lambda: page.goto(f"{base_url}/", wait_until="domcontentloaded")
    )


def _poll(page: Page) -> None:
    with page.expect_response(lambda r: r.url.endswith("/api/system/load")):
        _fake_clock.jump_interval(page, _POLL_MS, "fetchSystemLoad")


def _set_hidden(page: Page, hidden: bool) -> None:
    page.evaluate(
        """(hidden) => {
          Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
          document.dispatchEvent(new Event('visibilitychange'));
        }""",
        hidden,
    )


@pytest.mark.iphone
def test_every_header_shows_the_reading_and_its_four_states(
    authed_page: Page, base_url: str
) -> None:
    """The first load of ``system-load.js`` is cut (#1610, formerly
    ``test_a_failed_script_load_gets_one_fresh_boot``), or of the bundle's
    entry, which carries it: that page boots without ``main.js`` and the
    header keeps its markup default, so every
    reading below comes from the one fresh boot ``_boot`` gives it."""
    page = authed_page
    current = {"body": _reading("normal", 42.4)}
    calls: list = []
    _route_load(page, current, calls)
    navigations: list = []
    cut: list = []
    page.on("framenavigated", lambda f: navigations.append(1) if f == page.main_frame else None)

    def _cut_first_load(route) -> None:
        # The page asks for the file more than once per load, so cut every ask
        # of the first load, not just the first ask.
        if len(navigations) <= 1:
            cut.append(1)
            route.abort()
        else:
            route.continue_()

    page.route(re.compile(r".*/static/(system-load|js-bundle/main)\.js(\?.*)?$"), _cut_first_load)
    _fake_clock.install(page)
    _boot(page, base_url)
    assert cut, "the script was never requested"
    expect(page.locator("#paneClaude > .home-head .cpu-reading")).to_have_text("42%")

    # One reading per page header, Settings' included.
    expect(page.locator(".home-head .cpu-reading")).to_have_count(6)

    # Every tab: the muted reading, left of the theme toggle, in the one row.
    for tab, pane in _TABS.items():
        page.locator(tab).click()
        reading = page.locator(f"{pane} > .home-head .cpu-reading")
        expect(reading).to_have_text("42%")
        expect(reading).to_have_attribute("data-level", "normal")
        boxes = stable_eval(
            page.locator(f"{pane} > .home-head"),
            """(head) => {
              const r = (sel) => head.querySelector(sel).getBoundingClientRect();
              const h = head.getBoundingClientRect();
              return { head: h.height, overflow: head.scrollWidth - head.clientWidth,
                       cpuRight: r('.cpu-reading').right, toggleLeft: r('.theme-toggle-btn').left,
                       gearRight: r('.settings-open-btn').right, headRight: h.right };
            }""",
        )
        assert boxes["head"] <= 60, f"{pane}: the header wrapped ({boxes['head']}px)"
        assert boxes["overflow"] <= 0, f"{pane}: the header overflows by {boxes['overflow']}px"
        assert boxes["cpuRight"] <= boxes["toggleLeft"], f"{pane}: the reading is not left of the toggle"
        assert boxes["gearRight"] <= boxes["headRight"], f"{pane}: the gear is pushed off the card"

    reading = page.locator("#paneBoard > .home-head .cpu-reading")
    muted = reading.evaluate("el => getComputedStyle(el).color")

    # Attention: the server says the high has held for ~30 s.
    current["body"] = _reading("attention", 76.0)
    _poll(page)
    expect(reading).to_have_text("76%")
    expect(reading).to_have_attribute("data-level", "attention")
    attention = reading.evaluate("el => getComputedStyle(el).color")
    assert attention != muted, "attention must ring in its tone, not stay muted"

    # Danger: pegged.
    current["body"] = _reading("danger", 97.0)
    _poll(page)
    expect(reading).to_have_text("97%")
    expect(reading).to_have_attribute("data-level", "danger")
    danger = reading.evaluate("el => getComputedStyle(el).color")
    assert danger not in (muted, attention), "danger must be its own tone"

    # Unknown: a dash, never 0%.
    current["body"] = _reading("unknown", None, reason="error")
    _poll(page)
    expect(reading).to_have_text("–")
    expect(reading).to_have_attribute("data-level", "unknown")

    # Back to normal: the colour goes quiet again.
    current["body"] = _reading("normal", 12.0)
    _poll(page)
    expect(reading).to_have_text("12%")
    expect(reading).to_have_attribute("data-level", "normal")


@pytest.mark.iphone
def test_the_reading_never_hides_and_the_status_shortens_instead(
    authed_page: Page, base_url: str
) -> None:
    """The reading shows at every width with every tone (owner decision on
    #1838). Where the two exception parts no longer fit beside it (390px),
    the line wears each part's compact form, whole and uncut (design.md
    page-header: no ellipsis may cut the exception). At 1280px both fit, so
    the full words stay.

    First, formerly ``test_a_short_toned_status_keeps_its_words_beside_the_reading_on_the_phone``:
    a phone header is always narrow, but "1 needs you" alone leaves room
    beside the reading, so both show in full. The compact form is worn only
    when the measured line would be cut, never on the width alone. The
    sessions route reads its list per request, so a boot shows each list."""
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    current = {"body": _reading("normal", 30.0)}
    _route_load(page, current, [])
    sessions = [_WAITING]
    _mock_sessions(page, sessions)
    _boot(page, base_url)

    status = page.locator("#homeHeadStatus")
    reading = page.locator("#paneClaude > .home-head .cpu-reading")
    expect(status).to_have_text("1 needs you")
    expect(reading).to_have_text("30%")
    expect(reading).to_be_visible()
    assert stable_eval(status, "(el) => el.scrollWidth - el.clientWidth") <= 0

    sessions.append(_STALLED)
    for width in (390, 1280):
        page.set_viewport_size({"width": width, "height": 844})
        current["body"] = _reading("normal", 30.0)
        _boot(page, base_url)
        expect(status).to_have_text(
            "1 stalled · 1 needs you" if width > 500 else "1 stalled · 1 you"
        )
        expect(reading).to_have_text("30%")
        expect(reading).to_be_visible()
        assert stable_eval(status, "(el) => el.scrollWidth - el.clientWidth") <= 0, width

        current["body"] = _reading("danger", 96.0)
        with page.expect_response(lambda r: r.url.endswith("/api/system/load")):
            _set_hidden(page, False)  # a foreground return reads at once
        expect(reading).to_have_text("96%")
        expect(reading).to_be_visible()


def _assert_the_sheet_groups_a_burn_and_shows_what_the_rows_leave_out(
    page: Page, current: dict
) -> None:
    """Formerly ``test_the_sheet_groups_a_burn_and_shows_what_the_rows_leave_out``.

    #1646: 16 burners are one row with their count, a lone process keeps a
    plain row (never "×1"), and "Everything else" closes the gap to the CPU
    line. A remainder that rounds to nothing draws no row."""
    body_json = _reading("danger", 100.0)
    body_json["top"] = [
        {"name": "python", "cores": 11.2, "count": 16, "owner": "fleet-config-wt-1440"},
        {"name": "vmmemWSL", "cores": 1.6, "count": 1, "owner": None},
        {"name": "MsMpEng", "cores": 0.9, "count": 1, "owner": None},
    ]
    body_json["other_cores"] = 2.3  # 16.0 - 13.7
    current["body"] = body_json
    _poll(page)

    page.locator("#paneClaude > .home-head .cpu-reading").click()
    body = page.locator("#cpuSheetBody")
    first = body.locator("[data-row='top-0']")
    expect(first).to_contain_text("python ×16 · fleet-config-wt-1440")
    expect(first).to_contain_text("11.2 cores")
    for lone in ("top-1", "top-2"):
        expect(body.locator(f"[data-row='{lone}']")).not_to_contain_text("×")
    other = body.locator("[data-row='other']")
    expect(other).to_have_text(re.compile(r"Everything else\s*2\.3 cores"))
    expect(other).to_have_class(re.compile(r"usage-sheet-row"))  # the sheet's own row style
    # The remainder is the last row, after the processes it completes.
    expect(body.locator("[data-row]").last).to_have_attribute("data-row", "other")

    # Nothing left over: no row, rather than "0 cores".
    current["body"] = {**body_json, "other_cores": 0.0}
    page.locator("#cpuSheetDone").click()
    _poll(page)
    page.locator("#paneClaude > .home-head .cpu-reading").click()
    expect(page.locator("#cpuSheetBody [data-row='top-0']")).to_be_visible()
    expect(page.locator("#cpuSheetBody [data-row='other']")).to_have_count(0)
    page.locator("#cpuSheetDone").click()


_VIEWPORTS = {"desktop": {"width": 1280, "height": 900}, "android": {"width": 412, "height": 915}}
_BLOCK_WRITE_TEXT = (
    "() => Object.defineProperty(navigator.clipboard, 'writeText',"
    " { configurable: true, value: () => Promise.reject(new Error('NotAllowedError')) })"
)


def _clipboard(page: Page) -> str:
    return page.evaluate("() => navigator.clipboard.readText()")


def _assert_the_sheet_toast_is_painted_on_screen(page: Page) -> None:
    """The toast is in the sheet's footer and nothing paints over it: it once
    rendered under the Screen row, so a copy gave no visible feedback."""
    toast = page.locator("#cpuSheetToast")
    expect(toast).to_be_visible()
    box, view = toast.bounding_box(), page.viewport_size
    assert box and 0 <= box["x"] and box["x"] + box["width"] <= view["width"], box
    assert 0 <= box["y"] and box["y"] + box["height"] <= view["height"], box
    assert toast.evaluate(
        "(el) => { const r = el.getBoundingClientRect();"
        " return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)); }"
    ), "the toast is covered by another element"


def _assert_copy_really_copies_and_survives_a_poll(
    page: Page, current: dict, browser_name: str
) -> None:
    """Reads the real Chromium clipboard back, on a desktop and an Android-sized
    viewport. Copy toasts on the sheet; a poll landing between press and release
    does not lose the tap (the sheet's rows rebuild, the IP row does not);
    a refused ``writeText`` falls back to a selection copy; a clipboard that
    refuses both says to copy by hand."""
    if browser_name != "chromium":
        return  # WebKit cannot read the clipboard back; the mocked check covers it
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    button = page.locator("#cpuSheetBody [data-row='ip']").get_by_role("button", name="Copy")
    toast = page.locator("#cpuSheetToast")
    for size in _VIEWPORTS.values():
        page.set_viewport_size(size)
        page.evaluate("() => navigator.clipboard.writeText('not the ip')")
        button.click()
        expect(toast).to_contain_text("IP address copied")
        assert _clipboard(page) == _IP
        _assert_the_sheet_toast_is_painted_on_screen(page)

        page.evaluate("() => navigator.clipboard.writeText('not the ip')")
        box = button.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        _poll(page)  # the load poll rebuilds the open sheet's rows
        page.mouse.up()
        assert _clipboard(page) == _IP, "a poll between press and release lost the tap"
    page.set_viewport_size(_VIEWPORTS["desktop"])

    page.evaluate("() => navigator.clipboard.writeText('not the ip')")
    page.evaluate(_BLOCK_WRITE_TEXT)
    button.click()
    assert _clipboard(page) == _IP, "no selection-copy fallback when writeText is refused"

    page.evaluate("() => { document.execCommand = () => false; }")
    button.click()
    expect(toast).to_contain_text("Could not copy the IP address. Copy it by hand")
    expect(page.locator("#cpuSheetBody [data-row='ip']")).to_contain_text(_IP)
    page.evaluate("() => { delete document.execCommand; delete navigator.clipboard.writeText; }")


def _assert_the_sheet_shows_the_ip_and_copy_puts_exactly_it_on_the_clipboard(
    page: Page, body
) -> None:
    """The IP row is the sheet's first row, whether or not the CPU read, and
    Copy writes the raw address and nothing else, confirmed in the sheet's own
    toast (the page's toast sits under a modal dialog)."""
    row = body.locator("[data-row='ip']")
    expect(row).to_have_text(re.compile(rf"IP\s*{re.escape(_IP)}\s*Copy"))
    expect(body.locator("[data-row]").first).to_have_attribute("data-row", "ip")
    page.evaluate(
        "() => { window.__copied = []; Object.defineProperty(navigator, 'clipboard',"
        " { configurable: true, value: { writeText: async (t) => { window.__copied.push(t); } } }); }"
    )
    row.get_by_role("button", name="Copy").click()
    expect(page.locator("#cpuSheetToast")).to_contain_text("IP address copied")
    assert page.evaluate("() => window.__copied") == [_IP]


def _assert_an_unknown_ip_reads_unknown_and_cannot_be_copied(
    page: Page, base_url: str, current: dict, ip: dict
) -> None:
    """A PC whose address could not be read shows Unknown and a disabled Copy,
    and the sheet still draws its CPU rows."""
    ip["value"] = None
    current["body"] = _reading("normal", 20.0)
    _boot(page, base_url)
    page.locator("#paneClaude > .home-head .cpu-reading").click()
    row = page.locator("#cpuSheetBody [data-row='ip']")
    expect(row).to_contain_text("Unknown")
    expect(row.get_by_role("button", name="Copy")).to_be_disabled()
    expect(page.locator("#cpuSheetBody [data-row='cpu']")).to_be_visible()
    page.locator("#cpuSheetDone").click()


def _assert_an_unreadable_sample_says_why_in_the_sheet(page: Page, current: dict) -> None:
    """Formerly ``test_an_unreadable_sample_says_why_in_the_sheet``: a dash on
    the header, and the sheet names why."""
    current["body"] = _reading("unknown", None, reason="stale")
    _poll(page)
    reading = page.locator("#paneClaude > .home-head .cpu-reading")
    expect(reading).to_have_text("–")
    reading.click()
    expect(page.locator("#cpuSheetBody [data-row='cpu']")).to_contain_text("Last reading is too old")
    expect(page.locator("#cpuSheetBody [data-row^='top-']")).to_have_count(0)
    page.locator("#cpuSheetClose").click()


def _assert_the_poll_stops_while_the_page_is_hidden(
    page: Page, current: dict, calls: list
) -> None:
    """Formerly ``test_the_poll_stops_while_the_page_is_hidden``: one poll for
    the page, never per tab, and none while the page is hidden."""
    current["body"] = _reading("normal", 20.0)
    _poll(page)
    expect(page.locator("#paneClaude > .home-head .cpu-reading")).to_have_text("20%")
    seen = len(calls)

    # A tab switch adds no request of its own: one poll for the page.
    page.locator("#tabApps").click()
    page.locator("#tabBoard").click()
    flush_requests(page)
    assert len(calls) == seen, "a tab switch fetched the reading"

    # Hidden: two whole periods pass and nothing is asked.
    _set_hidden(page, True)
    _fake_clock.advance(page, _POLL_MS)
    _fake_clock.advance(page, _POLL_MS)
    flush_requests(page)
    assert len(calls) == seen, f"polled {len(calls) - seen}x while hidden"

    # Back in the foreground: one read at once.
    with page.expect_response(lambda r: r.url.endswith("/api/system/load")):
        _set_hidden(page, False)
    assert len(calls) == seen + 1


def _route_health(page: Page, current: dict, calls: list) -> None:
    """The hub's verdict, route-mocked; a page route wins over the context's quiet one."""

    def handle(route) -> None:
        calls.append(1)
        route.fulfill(status=current.get("status", 200), content_type="application/json",
                      body=json.dumps(current["body"]))

    page.route(re.compile(r".*/api/system/health$"), handle)


def _poll_until_the_verdict_is_asked(page: Page, calls: list) -> None:
    """Jump load polls until one of them asks for the verdict (at most every third)."""
    before = len(calls)
    for _ in range(4):
        _poll(page)
        if len(calls) > before:
            return
    raise AssertionError("the verdict was never asked for in four load polls")


_ACT_HEALTH = {
    "verdict": "act", "why": None, "reason": "Ports in use are at 84%",
    "fix": "Close the program holding the most ports",
    "machine": {
        "host": "example-host", "ports_percent": 84.0, "commit_percent": 68.0,
        "holders": [{"name": "builder.exe", "pid": 4242, "value": 900.0, "tcp": 880.0, "udp": 20.0}],
        "memory_holders": [], "last_event": None,
        "disks": [
            {"disk": "PhysicalDrive0", "drives": ["C:"], "state": "alert", "busy_percent": 98.0,
             "queue": 7.5, "read_ms": 120.4, "write_ms": 310.0},
            {"disk": "PhysicalDrive1", "drives": ["D:"], "state": "ok", "busy_percent": 1.0,
             "queue": 0.0, "read_ms": 0.1, "write_ms": 0.3},
        ],
    },
}
_OK_HEALTH = {"verdict": "ok", "why": None, "reason": None, "fix": None}


def _assert_an_act_verdict_draws_the_dot_and_the_machine_rows(
    page: Page, health: dict, health_calls: list
) -> None:
    """The hub's verdict beside the reading: a dot in the danger tone, then the
    sheet's Health row under the address, the hub's reason and fix, and the ports,
    commit, disk and holder rows ahead of the CPU. A watch verdict wears the
    attention dot, and an OK one draws none."""
    reading = page.locator("#paneClaude > .home-head .cpu-reading")
    health["body"] = _ACT_HEALTH
    _poll_until_the_verdict_is_asked(page, health_calls)
    expect(reading).to_have_attribute("data-pc-health", "danger")
    expect(reading).to_have_attribute("aria-label", re.compile(r"PC health: needs action"))
    dot = reading.evaluate("el => getComputedStyle(el, '::after').backgroundColor")
    assert dot not in ("rgba(0, 0, 0, 0)", "transparent"), "the dot must be painted"

    reading.click()
    body = page.locator("#cpuSheetBody")
    expect(body.locator("[data-row='health']")).to_contain_text("Needs action")
    expect(body.locator("[data-row='health'] strong")).to_have_attribute("data-tone", "danger")
    expect(body).to_contain_text("Ports in use are at 84%")
    expect(body).to_contain_text("Suggested: Close the program holding the most ports")
    expect(body.locator("[data-row='ports']")).to_contain_text("84")
    expect(body.locator("[data-row='commit']")).to_contain_text("68")
    expect(body.locator("[data-row='disk-0']")).to_contain_text("C:")
    expect(body.locator("[data-row='disk-0'] strong")).to_have_attribute("data-tone", "danger")
    expect(body.locator("[data-row='holder-0']")).to_contain_text("builder.exe")
    rows = body.locator("[data-row]").evaluate_all("els => els.map(e => e.dataset.row)")
    assert rows.index("ip") < rows.index("health") < rows.index("ports") < rows.index("cpu"), rows
    page.locator("#cpuSheetDone").click()

    health["body"] = {**_ACT_HEALTH, "verdict": "watch"}
    _poll_until_the_verdict_is_asked(page, health_calls)
    expect(reading).to_have_attribute("data-pc-health", "attention")

    health["body"] = _OK_HEALTH
    _poll_until_the_verdict_is_asked(page, health_calls)
    expect(reading).to_have_attribute("data-pc-health", "")
    reading.click()
    expect(page.locator("#cpuSheetBody [data-row='health']")).to_contain_text("OK")
    expect(page.locator("#cpuSheetBody [data-row='ports']")).to_have_count(0)
    page.locator("#cpuSheetDone").click()


_UNREAD_HEALTH = [
    ("disabled", None, "No hub is set up"),
    ("unreachable", "local-llm-hub unreachable", "Couldn't reach the hub"),
    ("warming", None, "Reading the hub"),
    ("overrun", "the scan has run 6.0s", "taking too long"),
    ("stale", "the last scan is 95s old", "too old"),
    ("hub", "no sample yet", "no sample yet"),
    ("unreadable", None, "wasn't a verdict"),
]


def _assert_a_verdict_that_could_not_be_read_is_not_confirmed_with_its_reason(
    page: Page, health: dict, health_calls: list
) -> None:
    """Never OK, never a dot, no machine rows, and each cause in its own words;
    a failed ask of the launcher itself reads the same way."""
    reading = page.locator("#paneClaude > .home-head .cpu-reading")
    body = page.locator("#cpuSheetBody")
    cases = [({"verdict": "unknown", "why": why, "reason": reason, "fix": None}, words)
             for why, reason, words in _UNREAD_HEALTH]
    for payload, words in cases:
        health["body"] = payload
        _poll_until_the_verdict_is_asked(page, health_calls)
        expect(reading).to_have_attribute("data-pc-health", "")
        reading.click()
        expect(body.locator("[data-row='health']")).to_contain_text("Not confirmed")
        expect(body).to_contain_text(words)
        expect(body.locator("[data-row='health']")).not_to_contain_text("OK")
        expect(body.locator("[data-row='ports']")).to_have_count(0)
        page.locator("#cpuSheetDone").click()

    health["status"], health["body"] = 500, {}
    _poll_until_the_verdict_is_asked(page, health_calls)
    reading.click()
    expect(body.locator("[data-row='health']")).to_contain_text("Not confirmed")
    expect(body).to_contain_text("Couldn't reach the launcher")
    page.locator("#cpuSheetDone").click()
    health["status"], health["body"] = 200, _OK_HEALTH


def _assert_the_verdict_is_asked_for_every_thirty_seconds_not_every_poll(
    page: Page, health_calls: list
) -> None:
    """The server answers from a cache it refreshes once per 30 s, so the page
    asks on every third load poll: two polls after an ask add no request."""
    _poll_until_the_verdict_is_asked(page, health_calls)
    asked = len(health_calls)
    _poll(page)
    _poll(page)
    flush_requests(page)
    assert len(health_calls) == asked, "the load poll re-asked for the verdict inside 30 s"
    _poll(page)
    flush_requests(page)
    assert len(health_calls) == asked + 1


def test_the_tap_sheet_lists_cpu_memory_and_the_top_three(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    """The sheet's rows, then on the same load (each reading a poll on the
    fake clock) a grouped burn, an unreadable sample and the hidden-page
    gate, which runs last because it leaves the Board open."""
    page = authed_page
    current = {"body": _reading("danger", 93.0)}
    calls: list = []
    ip = {"value": _IP}
    health = {"body": _OK_HEALTH}
    health_calls: list = []
    _route_load(page, current, calls)
    _route_health(page, health, health_calls)
    _route_ip(page, ip)
    _fake_clock.install(page)
    _boot(page, base_url)

    reading = page.locator("#paneClaude > .home-head .cpu-reading")
    expect(reading).to_have_text("93%")
    reading.click()
    sheet = page.locator("#cpuSheet")
    expect(sheet).to_be_visible()
    body = sheet.locator("#cpuSheetBody")
    _assert_copy_really_copies_and_survives_a_poll(page, current, browser_name)
    _assert_the_sheet_shows_the_ip_and_copy_puts_exactly_it_on_the_clipboard(page, body)
    expect(body.locator("[data-row='cpu']")).to_contain_text("93% of 16 cores")
    expect(body.locator("[data-row='cpu'] .usage-sheet-value")).to_have_attribute("data-tone", "danger")
    expect(body.locator("[data-row='memory']")).to_contain_text("48 of 64 GB (75%)")
    expect(body.locator("[data-row^='top-']")).to_have_count(3)
    expect(body.locator("[data-row='top-0']")).to_contain_text("python (pytest)")
    expect(body.locator("[data-row='top-0']")).to_contain_text("3.1 cores")
    expect(body.locator("[data-row='top-1']")).to_contain_text("1 core")
    page.locator("#cpuSheetDone").click()
    expect(sheet).to_be_hidden()

    _assert_the_sheet_groups_a_burn_and_shows_what_the_rows_leave_out(page, current)
    _assert_an_unreadable_sample_says_why_in_the_sheet(page, current)
    current["body"] = _reading("normal", 20.0)
    _assert_an_act_verdict_draws_the_dot_and_the_machine_rows(page, health, health_calls)
    _assert_a_verdict_that_could_not_be_read_is_not_confirmed_with_its_reason(page, health, health_calls)
    _assert_the_verdict_is_asked_for_every_thirty_seconds_not_every_poll(page, health_calls)
    _assert_an_unknown_ip_reads_unknown_and_cannot_be_copied(page, base_url, current, ip)
    _assert_the_poll_stops_while_the_page_is_hidden(page, current, calls)
