"""The History button in the Projects card and the sheet it opens.

The sheet answers from ``/api/progress``'s cache at once, however old, says how
old each source is, and asks for nothing: only its Refresh button asks the
server to re-read, shows a toast while that runs and updates in place when it
ends. A figure that was not measured shows its word (unknown, skipped), never
zero.

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


def _payload(*, added: int, refreshing: bool = False, gitlab_state: str = "ok", age: float = 4000.0) -> Dict[str, Any]:
    forge_ok = gitlab_state == "ok"
    return {
        "bucket": "day", "from": "2026-10-06", "to": "2026-11-04",
        "generated_at": "2026-11-04T10:00:00+00:00", "refreshing": refreshing,
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
    """Stands in for ``GET /api/progress``: the cache, and a refresh that runs until released."""

    def __init__(self, cached: Dict[str, Any], refreshed: Dict[str, Any]) -> None:
        self.cached = cached
        self.refreshed = refreshed
        self.released = False
        self.requests: List[str] = []

    def __call__(self, route) -> None:
        url = route.request.url
        self.requests.append(url)
        if self.released:
            body = self.refreshed
        elif any("refresh=true" in u for u in self.requests):
            body = dict(self.cached, refreshing=True)
        else:
            body = self.cached
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))


def _boot(page: Page, base_url: str, progress: _Progress) -> None:
    def json_route(pattern: str, body: Dict[str, Any]) -> None:
        page.route(re.compile(pattern), lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)))
    json_route(r".*/api/claude-code/sessions$", {"sessions": []})
    json_route(r".*/api/claude-code/git-status$", {"projects": []})
    page.route(re.compile(r".*/api/progress(\?.*)?$"), progress)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")


def _open_history(page: Page) -> None:
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    page.locator("#historyOpen").click()
    expect(page.locator("#progressSheet")).to_be_visible()


def _tile(page: Page, key: str):
    return page.locator(f'#progressTiles .progress-tile[data-tile="{key}"]')


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
    progress = _Progress(_payload(added=100), _payload(added=250, age=5.0))
    _boot(page, base_url, progress)
    _open_history(page)

    # The cached figures, however old, with each source's age, and no recalculation asked.
    expect(page.locator("#progressSheetTitle")).to_have_text("History")
    expect(_tile(page, "lines")).to_contain_text("+150")
    expect(page.locator('#progressSources li[data-source="git"]')).to_contain_text("1 h ago")
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

    # Refresh toasts while the server recalculates, then the sheet updates in place.
    page.locator("#progressRefresh").click()
    toast = page.locator("#progressToast")
    expect(toast).to_be_visible()
    expect(toast).to_contain_text("Refreshing")
    expect(page.locator("#progressRefresh")).to_be_disabled()
    progress.released = True

    def next_poll_landed() -> bool:
        _fake_clock.advance(page, 4000)
        return "+300" in _tile(page, "lines").inner_text()

    wait_until(page, next_poll_landed, "the sheet to read the finished refresh")
    expect(toast).to_be_hidden()
    expect(page.locator("#progressRefresh")).to_be_enabled()
    expect(page.locator('#progressSources li[data-source="git"]')).to_contain_text("just now")
    asks = [u for u in progress.requests if "refresh=true" in u]
    assert len(asks) == 1, asks  # one tap, one recalculation

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
