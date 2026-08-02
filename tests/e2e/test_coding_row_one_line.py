"""Regression pin for issue #8 (one line per Coding project row).

Project rows used to stack into two lines under the 520px breakpoint —
folder name on one, the icon strip wrapped onto another — doubling the
scroll for a list of projects. Now a row is a single line at every width:
name + branch pill on the left, three gapped buttons on the right, with the
branch pill ellipsis-truncating rather than wrapping the row.

The viewport is pinned to 390px here (narrower than the Pixel 8 Pro
projection's 448px) so the assertions bind at the tightest realistic phone
width on both projection legs, not just the phone one.

/api/apps and /api/coding/git-status are mocked so both the no-branch and
the pathological-long-branch cases are deterministic — the real scan would
give whatever happens to be on the developer's disk.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from .conftest import stable_read

pytestmark = pytest.mark.smoke

# Narrowest width we promise to hold. Below the Pixel 8 Pro's 448px.
PHONE_W = 390
PHONE_H = 844

# Long enough that it cannot possibly fit beside the name at 390px.
LONG_BRANCH = "feat/1234-a-deliberately-overlong-branch-slug-for-truncation"

APPS = {
    "scan_root": "E:/automation",
    "apps": [
        {
            "id": "onmain",
            "name": "on-main-project",
            "kind": "coding",
            "project_dir": "E:/automation/on-main-project",
            "added_at": "",
            "is_favorite": False,
            "repo_url": "https://github.com/ferraroroberto/on-main-project",
            "repo_issues_url": "https://github.com/ferraroroberto/on-main-project/issues",
        },
        {
            "id": "offmain",
            "name": "off-main-project",
            "kind": "coding",
            "project_dir": "E:/automation/off-main-project",
            "added_at": "",
            "is_favorite": False,
            "repo_url": "https://github.com/ferraroroberto/off-main-project",
            "repo_issues_url": "https://github.com/ferraroroberto/off-main-project/issues",
        },
    ],
}

GIT_STATUS = {
    "projects": [
        {
            "id": "onmain",
            "is_git": True,
            "branch": "main",
            "default_branch": "main",
            "on_default_branch": True,
            "dirty": False,
        },
        {
            "id": "offmain",
            "is_git": True,
            "branch": LONG_BRANCH,
            "default_branch": "main",
            "on_default_branch": False,
            "dirty": False,
        },
    ]
}


def _install_routes(page: Page) -> None:
    def _json(payload):
        return lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/apps", _json(APPS))
    page.route("**/api/coding/git-status", _json(GIT_STATUS))
    page.route(
        "**/api/agents",
        _json(
            {
                "agents": [
                    {
                        "id": "copilot",
                        "label": "GitHub Copilot CLI",
                        "available": True,
                        "fullscreen": True,
                    }
                ]
            }
        ),
    )


@pytest.fixture()
def rows(authed_page: Page, base_url: str) -> Page:
    """A 390px-wide page with the Projects card open and both rows painted."""
    authed_page.set_viewport_size({"width": PHONE_W, "height": PHONE_H})
    _install_routes(authed_page)
    # Every button visible — the row must hold all three, and a machine with a
    # hidden agent in its real config would otherwise weaken the assertion.
    authed_page.request.post(
        f"{base_url}/api/config", data={"coding_hidden_agents": []}
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    expect(authed_page.locator('.coding-item[data-id="offmain"]')).to_be_visible(
        timeout=5_000
    )
    # Gate on the *pill*, not just the row: git-status is a separate async
    # boot fetch, so a row measured before it lands has no branch tag at all
    # and would prove nothing about the long-branch case.
    expect(
        authed_page.locator('.coding-item[data-id="offmain"] .git-branch-tag')
    ).to_be_visible(timeout=10_000)
    return authed_page


def _box(page: Page, selector: str) -> dict:
    box = stable_read(lambda: page.locator(selector).bounding_box())
    assert box, f"{selector} never laid out"
    return box


@pytest.mark.parametrize("row_id", ["onmain", "offmain"])
def test_project_row_is_one_line(rows: Page, row_id: str) -> None:
    """Name and action strip share a line — on the default branch and off it.

    Proven by vertical overlap (stacked would put them in disjoint bands),
    plus a row height under two stacked 44px targets. `offmain` carries the
    pathological branch name, so this is also the "long branch does not wrap
    the row" case.
    """
    row = _box(rows, f'.coding-item[data-id="{row_id}"]')
    name = _box(rows, f'.coding-item[data-id="{row_id}"] .coding-name')
    actions = _box(rows, f'.coding-item[data-id="{row_id}"] .row-actions.agent-actions')

    assert name["y"] < actions["y"] + actions["height"], (
        f"{row_id}: name sits below the action strip — row is stacked"
    )
    assert actions["y"] < name["y"] + name["height"], (
        f"{row_id}: action strip sits below the name — row is stacked"
    )
    # Two stacked 44px targets would be >= 88; one line is ~44.
    assert row["height"] < 70, (
        f"{row_id}: row is {row['height']}px tall — expected a single ~44px line"
    )


def test_long_branch_pill_truncates_and_buttons_stay_visible(rows: Page) -> None:
    """The pill is cut with an ellipsis; nothing is pushed off the row."""
    sel = '.coding-item[data-id="offmain"] .git-branch-tag'
    expect(rows.locator(sel)).to_be_visible()

    # Full branch name preserved in the DOM + tooltip even though it's cut.
    expect(rows.locator(sel)).to_have_text(LONG_BRANCH)
    assert LONG_BRANCH in (rows.locator(sel).get_attribute("title") or "")

    overflowing = stable_read(
        lambda: rows.locator(sel).evaluate(
            "el => el.scrollWidth > el.clientWidth ? 'yes' : 'no'"
        )
    )
    assert overflowing == "yes", (
        "branch pill is not actually truncated — it fits, so this no longer "
        "exercises the overflow case; lengthen LONG_BRANCH"
    )

    row = _box(rows, '.coding-item[data-id="offmain"]')
    buttons = rows.locator('.coding-item[data-id="offmain"] .icon-btn')
    expect(buttons).to_have_count(3)   # Copilot, GitHub, star
    for i in range(3):
        box = stable_read(lambda i=i: buttons.nth(i).bounding_box())
        assert box, f"button {i} never laid out"
        assert box["x"] >= row["x"] - 0.5, f"button {i} clipped off the left edge"
        assert box["x"] + box["width"] <= row["x"] + row["width"] + 0.5, (
            f"button {i} clipped off the right edge at {PHONE_W}px"
        )


def test_row_buttons_meet_the_44px_target_and_do_not_overlap(rows: Page) -> None:
    """Real geometry, not a ::before expansion — so targets never overlap."""
    buttons = rows.locator('.coding-item[data-id="offmain"] .icon-btn')
    expect(buttons).to_have_count(3)

    boxes = []
    for i in range(3):
        box = stable_read(lambda i=i: buttons.nth(i).bounding_box())
        assert box, f"button {i} never laid out"
        boxes.append(box)

    for i, box in enumerate(boxes):
        assert box["width"] >= 44, f"button {i} is {box['width']}px wide (< 44px)"
        assert box["height"] >= 44, f"button {i} is {box['height']}px tall (< 44px)"

    for i in range(len(boxes) - 1):
        left, right = boxes[i], boxes[i + 1]
        assert left["x"] + left["width"] <= right["x"] + 0.5, (
            f"buttons {i} and {i + 1} overlap horizontally"
        )
