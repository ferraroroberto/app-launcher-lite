"""History, reached from the Usage card's menu, and the sheet it opens.

The sheet answers from ``/api/progress``'s cache at once, however old, says how
old each source is and when it was last refreshed, and asks for nothing: only
its Refresh button asks the server to re-read. That runs behind a neutral toast
that counts the projects, updates the sheet in place and ends on a summary: a
skipped project is counted there and is not an error, a failed read is the
error style and names its project. A figure that was not measured shows its
word (unknown, skipped), never zero.

Hermetic: ``/api/progress`` and the sessions and git-status lists are
route-mocked before ``goto()`` with invented numbers, so nothing runs git or
reads GitLab. The refresh is driven with the fake page clock, not by waiting
out a poll.
"""

from __future__ import annotations

import json as _json
import re
from typing import Any, Dict, List, Optional

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.fixtures.waits import wait_until

pytestmark = pytest.mark.smoke

_NO_GITLAB = {"state": "skipped", "reason": "no GitLab remote"}


def _day(start: str, git: Optional[Dict[str, int]], gitlab: Optional[Dict[str, int]]) -> Dict[str, Any]:
    return {"start": start, "end": start, "git": git, "gitlab": gitlab}


def _forge(merged: int, opened: int, closed: int) -> Dict[str, int]:
    return {"mrs_merged": merged, "issues_opened": opened, "issues_closed": closed}


def _row(name: str, added: Optional[int], removed: Optional[int], forge: Optional[Dict[str, int]]) -> Dict[str, Any]:
    return {
        "repo": name, "git_state": "ok", "gitlab_state": "ok" if forge else _NO_GITLAB["state"],
        "lines_added": added, "lines_removed": removed,
        **(forge or {"mrs_merged": None, "issues_opened": None, "issues_closed": None}),
    }


_OK = {"status": "ok"}
_SKIP = {"status": "skipped", "reason": "no GitLab remote"}


def _run(done: int, results: Optional[Dict[str, Dict[str, str]]] = None) -> Dict[str, Any]:
    """The server's refresh progress: ``done`` of three projects, the finished ones with a result."""
    finished = len(results or {}) == 3
    return {"run": 1, "state": "done" if finished else "running", "done": done, "total": 3,
            "current": None if finished else "beacon-api", "results": results or {}, "error": None,
            "finished_at": "2026-11-04T10:00:05Z" if finished else None}


def _payload(
    *, added: int, refreshing: bool = False, gitlab_state: str = "ok", age: float = 4000.0,
    refresh: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    forge_ok = gitlab_state == "ok"
    return {
        "bucket": "day", "from": "2026-10-06", "to": "2026-11-04",
        "generated_at": "2026-11-04T10:00:00+00:00", "refreshing": refreshing, "refresh": refresh,
        "git": {"state": "ok", "reason": None, "refreshed_at": "2026-11-04T08:53:00+00:00",
                "age_seconds": age, "refreshing": refreshing, "unknown_repos": []},
        "gitlab": {"state": gitlab_state, "reason": None if forge_ok else "Could not be read.",
                   "refreshed_at": None, "age_seconds": age if forge_ok else None, "refreshing": refreshing,
                   "unknown_repos": [], "skipped": [{"repo": "scratch-pad", "reason": "no GitLab remote"}],
                   "truncated": []},
        "buckets": [
            _day("2026-11-02", {"lines_added": added, "lines_removed": 40}, _forge(2, 3, 1) if forge_ok else None),
            _day("2026-11-03", {"lines_added": 50, "lines_removed": 10}, _forge(1, 0, 2) if forge_ok else None),
        ],
        "repos": [
            _row("atlas-web", added, 40, _forge(2, 3, 1) if forge_ok else None),
            _row("beacon-api", 50, 10, _forge(1, 0, 2) if forge_ok else None),
            _row("scratch-pad", 7, 0, None),
        ],
    }


class _Progress:
    """Stands in for ``GET /api/progress``: the cache, and a slow refresh that reports ``step`` until released."""

    def __init__(self, cached: Dict[str, Any], refreshed: Dict[str, Any]) -> None:
        self.cached = cached
        self.refreshed = refreshed
        self.released = False
        self.step: Optional[Dict[str, Any]] = None
        self.requests: List[str] = []

    def __call__(self, route) -> None:
        url = route.request.url
        self.requests.append(url)
        if self.released:
            body = self.refreshed
        elif any("refresh=true" in u for u in self.requests):
            body = dict(self.cached, refreshing=True, refresh=self.step)
        else:
            body = self.cached
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))


def _boot(page: Page, base_url: str, progress: _Progress) -> None:
    def json_route(pattern: str, body: Dict[str, Any]) -> None:
        page.route(re.compile(pattern), lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)))
    json_route(r".*/api/claude-code/sessions$", {"sessions": []})
    json_route(r".*/api/claude-code/git-status$", {"projects": []})
    json_route(r".*/api/copilot-usage$", {
        "state": "ok", "used_credits": 180.0, "allowance": 300, "used_pct": 60.0, "pace_pct": 40.0,
        "today_credits": 4.0, "reset_at": "2026-12-01T00:00:00+00:00", "source": "a fictional log",
        "scope": "this machine", "reason": ""})
    page.route(re.compile(r".*/api/progress(\?.*)?$"), progress)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")


def _open_history(page: Page) -> None:
    """History is reached from the Usage card's menu, not from a button of its own."""
    page.locator("#codingUsageKebab").click()
    page.locator('.row-menu [data-item="usage-history-btn"]').click()
    expect(page.locator("#progressSheet")).to_be_visible()


def _usage_card_menu_offers_details_and_history(page: Page) -> None:
    """A tap on the Usage card body (not only its kebab) opens a menu of two
    choices; the Projects toolbar has no History button of its own."""
    expect(page.locator("#codingUsage .um-pct")).to_have_text("60%")
    expect(page.locator("#historyOpen")).to_have_count(0)
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    expect(page.locator("#gitToolbarActions button[aria-label*='History']")).to_have_count(0)
    page.locator("#codingUsageFooter").click()
    expect(page.locator(".row-menu.usage-menu [role='menuitem']")).to_have_text(["Usage details", "History"])
    # Usage details writes the card's figures out and closes with Done.
    page.locator('.row-menu [data-item="usage-details-btn"]').click()
    sheet = page.locator("#usageSheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator(".context-sheet-row").first).to_contain_text("180 of 300 AI credits")
    page.locator("#usageSheetDone").click()
    expect(sheet).to_be_hidden()


def _tile(page: Page, key: str):
    return page.locator(f'#progressTiles .progress-tile[data-tile="{key}"]')


def _refresh_counts_then_summarises(page: Page, progress: _Progress) -> None:
    """Two refreshes: one whose skip is a neutral summary, one whose failed read is the error style."""
    # Refresh toasts while the server recalculates, counting projects in a neutral toast.
    progress.step = _run(0)
    page.locator("#progressRefresh").click()
    toast = page.locator("#progressToast")
    expect(toast).to_be_visible()
    expect(toast).to_have_text(re.compile(r"Checking 1 of 3"))
    expect(toast).not_to_have_class(re.compile(r"error"))
    expect(page.locator("#progressRefresh")).to_be_disabled()
    progress.step = _run(1)

    def second_project_counted() -> bool:
        _fake_clock.advance(page, 4000)
        return "Checking 2 of 3" in toast.inner_text()

    wait_until(page, second_project_counted, "the toast to count the second project")

    # It ends on a summary where the skip is counted and is not an error, and the sheet updates in place.
    progress.released = True

    def next_poll_landed() -> bool:
        _fake_clock.advance(page, 4000)
        return "+300" in _tile(page, "lines").inner_text()

    wait_until(page, next_poll_landed, "the sheet to read the finished refresh")
    expect(toast).to_have_text(re.compile(r"Refreshed 2 of 3 · 1 skipped \(no GitLab remote\)"))
    expect(toast).not_to_have_class(re.compile(r"error"))
    expect(page.locator("#progressRefresh")).to_be_enabled()
    expect(page.locator('#progressSources li[data-source="git"]')).to_contain_text("just now")
    asks = [u for u in progress.requests if "refresh=true" in u]
    assert len(asks) == 1, asks  # one tap, one recalculation

    # A read that really failed is the error style and names the project.
    progress.released = False
    progress.step = _run(0)
    progress.refreshed = _payload(
        added=250, age=5.0,
        refresh=_run(3, {"atlas-web": _OK, "beacon-api": {"status": "failed", "error": "gitlab: 401"}, "scratch-pad": _SKIP}),
    )
    page.locator("#progressRefresh").click()
    expect(toast).to_have_text(re.compile(r"Checking 1 of 3"))
    progress.released = True

    def failure_reported() -> bool:
        _fake_clock.advance(page, 4000)
        return "could not read" in toast.inner_text()

    wait_until(page, failure_reported, "the summary to name the failed project")
    expect(toast).to_have_text(re.compile(r"Refreshed 1 of 3 · 1 skipped \(no GitLab remote\) · could not read beacon-api"))
    expect(toast).to_have_class(re.compile(r"error"))


def _assert_by_project_header_fits(page: Page) -> None:
    """At 360 px, in both themes, the title does not wrap and the count is a subtitle under it, not drawn over it."""
    page.set_viewport_size({"width": 360, "height": 740})
    for theme in ("light", "dark"):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        boxes = page.locator("#progressReposTitle").evaluate("""title => {
          const meta = document.getElementById('progressReposMeta');
          const line = parseFloat(getComputedStyle(title).lineHeight) || title.getBoundingClientRect().height;
          const t = title.getBoundingClientRect();
          const m = meta.getBoundingClientRect();
          return { titleHeight: t.height, line: line, titleBottom: t.bottom, metaTop: m.top, metaRight: m.right,
                   cardRight: title.closest('.card').getBoundingClientRect().right };
        }""")
        assert boxes["titleHeight"] <= boxes["line"] * 1.5, (theme, boxes)  # one line, not two
        assert boxes["metaTop"] >= boxes["titleBottom"] - 1, (theme, boxes)  # below it, not over it
        assert boxes["metaRight"] <= boxes["cardRight"] + 1, (theme, boxes)
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")


@pytest.mark.iphone
def test_history_sheet_answers_from_the_cache_refreshes_on_request_and_never_reads_an_unknown_as_zero(
    authed_page: Page, base_url: str
) -> None:
    """One flow on a phone-width page: the cached figures at once, a bucket or
    range change that rereads the cache only, a Refresh that toasts and updates
    in place, then GitLab unreadable and a project with no remote."""
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    _fake_clock.install(page)
    progress = _Progress(
        _payload(added=100),
        _payload(added=250, age=5.0, refresh=_run(3, {"atlas-web": _OK, "beacon-api": _OK, "scratch-pad": _SKIP})),
    )
    _boot(page, base_url, progress)
    _usage_card_menu_offers_details_and_history(page)
    _open_history(page)

    # The cached figures, however old, with each source's age, and no recalculation asked.
    expect(page.locator("#progressSheetTitle")).to_have_text("History")
    expect(_tile(page, "lines")).to_contain_text("+150")
    expect(page.locator('#progressSources li[data-source="git"]')).to_contain_text("1 h ago")
    expect(page.locator("#progressRefreshed")).to_have_text("Last refreshed 1 h ago. Tap Refresh to update.")
    expect(page.locator("#progressToast")).to_be_hidden()
    assert [u for u in progress.requests if "refresh=true" in u] == []

    # A bucket or range change rereads the cache only.
    before = len(progress.requests)
    page.locator('#progressSheet [data-bucket="week"]').click()
    wait_until(page, lambda: len(progress.requests) > before, "the week read")
    assert "bucket=week" in progress.requests[-1]
    page.locator('#progressSheet [data-range="90"]').click()
    wait_until(page, lambda: len(progress.requests) > before + 1, "the 90d read")
    expect(page.locator('#progressSheet [data-range="90"]')).to_have_attribute("aria-pressed", "true")
    assert [u for u in progress.requests if "refresh=true" in u] == []

    # It fits the phone: no horizontal overflow, charts drawn, a 44px primary.
    expect(page.locator("#progressChartLoc svg")).to_be_visible()
    fits = page.locator("#progressSheet").evaluate("""d => {
      const box = d.getBoundingClientRect();
      const wide = Array.from(d.querySelectorAll('.progress-body *')).filter(
        e => e.getBoundingClientRect().right > box.right + 1 && !e.closest('.progress-table-wrap'));
      return { overflowX: d.scrollWidth - d.clientWidth, wide: wide.map(e => e.className.toString()) };
    }""")
    assert fits["overflowX"] <= 1 and fits["wide"] == [], fits
    done = page.locator("#progressSheetDone").bounding_box()
    assert done is not None and done["height"] >= 44, done

    _refresh_counts_then_summarises(page, progress)

    # GitLab unreadable: its tiles carry the word and never a zero, local git still draws.
    progress.cached = progress.refreshed = _payload(added=100, gitlab_state="unavailable")
    page.locator('#progressSheet [data-bucket="month"]').click()
    mrs = _tile(page, "mrs")
    expect(mrs).to_contain_text("unavailable")
    expect(mrs).not_to_contain_text("0")
    expect(_tile(page, "lines")).to_contain_text("+150")
    expect(page.locator('#progressSources li[data-source="gitlab"]')).to_contain_text("unavailable")

    # A project with no GitLab remote reads skipped, not 0, and is listed under the source.
    scratch = page.locator('#progressRepos tr[data-repo="scratch-pad"]')
    expect(scratch.locator('[data-metric="mrs_merged"]')).to_have_text("skipped")
    expect(scratch.locator('[data-metric="lines_added"]')).to_have_text("+7")
    expect(page.locator('#progressSources li[data-source="gitlab"]')).to_contain_text("scratch-pad")

    # The "By project" header holds at the narrowest phone, light and dark.
    _assert_by_project_header_fits(page)
