"""Regression pin for issue #8 (compact Coding project rows).

Project rows used to stack into two lines under the 520px breakpoint —
folder name on one, the *icon strip* wrapped onto another — doubling the
scroll for a list of projects. Now:

* a project on its default branch is a flat single ~44px line;
* a project parked off its default branch puts the branch pill on its own
  line *beneath* the name (a deliberate, accepted extra ~13px), so the pill
  gets the tile's full text width instead of collapsing to an unreadable
  "fea…" stub beside the name;
* the action strip is never what wraps — three 44px targets, gapped, on one
  line, at every width.

The viewport is pinned to 390px here (narrower than the Pixel 8 Pro
projection's 448px) so the assertions bind at the tightest realistic phone
width on both projection legs, not just the phone one.

/api/apps and /api/coding/git-status are mocked so the default-branch,
realistic-branch and pathological-branch cases are all deterministic — the
real scan would give whatever happens to be on the developer's disk.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from .conftest import stable_read

pytestmark = pytest.mark.smoke

# Narrowest width we promise to hold. Below the Pixel 8 Pro's 448px.
PHONE_W = 390
PHONE_H = 844

# A real branch name from this fleet's own naming convention. The whole point
# of moving the pill onto its own line is that this fits UNCUT at 390px.
REAL_BRANCH = "fix/28-terminal-reconnect"
# Long enough that not even a full-width pill can hold it.
LONG_BRANCH = "feat/1234-a-deliberately-overlong-branch-slug-for-truncation"


def _app(app_id: str, name: str) -> dict:
    return {
        "id": app_id,
        "name": name,
        "kind": "coding",
        "project_dir": f"E:/automation/{name}",
        "added_at": "",
        "is_favorite": False,
        "repo_url": f"https://github.com/ferraroroberto/{name}",
        "repo_issues_url": f"https://github.com/ferraroroberto/{name}/issues",
    }


APPS = {
    "scan_root": "E:/automation",
    "apps": [
        _app("onmain", "on-main-project"),
        _app("realbranch", "app-launcher-lite"),
        _app("offmain", "off-main-project"),
    ],
}


def _status(app_id: str, branch: str, on_default: bool) -> dict:
    return {
        "id": app_id,
        "is_git": True,
        "branch": branch,
        "default_branch": "main",
        "on_default_branch": on_default,
        "dirty": False,
    }


GIT_STATUS = {
    "projects": [
        _status("onmain", "main", True),
        _status("realbranch", REAL_BRANCH, False),
        _status("offmain", LONG_BRANCH, False),
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
    """A 390px-wide page with the Projects card open and all rows painted."""
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
    # Gate on the *pills*, not just the rows: git-status is a separate async
    # boot fetch, so a row measured before it lands has no branch tag at all
    # and would prove nothing about either off-main case.
    expect(authed_page.locator("#codingList .git-branch-tag")).to_have_count(
        2, timeout=10_000
    )
    return authed_page


def _box(page: Page, selector: str) -> dict:
    box = stable_read(lambda: page.locator(selector).bounding_box())
    assert box, f"{selector} never laid out"
    return box


def _button_boxes(page: Page, row_id: str) -> list[dict]:
    buttons = page.locator(f'.coding-item[data-id="{row_id}"] .icon-btn')
    expect(buttons).to_have_count(3)   # Copilot, GitHub, star
    boxes = []
    for i in range(3):
        box = stable_read(lambda i=i: buttons.nth(i).bounding_box())
        assert box, f"{row_id}: button {i} never laid out"
        boxes.append(box)
    return boxes


def test_default_branch_row_is_a_single_line(rows: Page) -> None:
    """No pill → name and action strip share one flat ~44px line."""
    row = _box(rows, '.coding-item[data-id="onmain"]')
    name = _box(rows, '.coding-item[data-id="onmain"] .coding-name')
    actions = _box(rows, '.coding-item[data-id="onmain"] .row-actions.agent-actions')

    expect(rows.locator('.coding-item[data-id="onmain"] .git-branch-tag')).to_have_count(0)
    assert name["y"] < actions["y"] + actions["height"], "name below the strip — stacked"
    assert actions["y"] < name["y"] + name["height"], "strip below the name — stacked"
    # Two stacked 44px targets would be >= 88; one line is ~44.
    assert row["height"] < 70, (
        f"default-branch row is {row['height']}px tall — expected a single ~44px line"
    )


@pytest.mark.parametrize("row_id", ["realbranch", "offmain"])
def test_off_main_row_puts_the_pill_under_the_name(rows: Page, row_id: str) -> None:
    """The pill stacks beneath the name — never beside it, never wrapping.

    The action strip still shares the row (it is not what wraps), and the row
    stays bounded: taller than a bare line, nowhere near two stacked targets.
    """
    row = _box(rows, f'.coding-item[data-id="{row_id}"]')
    name = _box(rows, f'.coding-item[data-id="{row_id}"] .coding-name-text')
    pill = _box(rows, f'.coding-item[data-id="{row_id}"] .git-branch-tag')
    actions = _box(rows, f'.coding-item[data-id="{row_id}"] .row-actions.agent-actions')

    assert pill["y"] >= name["y"] + name["height"] - 1, (
        f"{row_id}: pill at y={pill['y']} is not below the name "
        f"(name ends at {name['y'] + name['height']}) — still sharing the line"
    )
    # The strip stays on the row, vertically alongside the two text lines.
    assert actions["y"] < row["y"] + row["height"], f"{row_id}: strip fell out of the row"
    assert 44 < row["height"] < 85, (
        f"{row_id}: row is {row['height']}px — expected a two-line-text row "
        "(one accepted extra line, not a stacked action strip)"
    )


# Visible pill width the stacked layout must buy back at 390px. Measured at
# 115px (~17 chars: "fix/28-terminal-…"); the pre-#8 inline pill collapsed to
# a 3.5em / 44px stub (~4 chars, "fea…"), which is what made it useless.
# 115 rather than the 133 the first cut managed: aligning the row buttons
# with the summary's controls reserves an 18px chevron column, and that comes
# out of the text column. Alignment was the explicitly preferred trade.
# A ~25-char branch is therefore still cut at 390px and fits whole from
# ~470px — that is the width budget, not a defect.
MIN_PILL_PX = 110


def test_stacked_pill_is_wide_enough_to_read(rows: Page) -> None:
    """The reason the pill moved onto its own line: it must be readable.

    Pinned as a width floor rather than "nothing is ever cut" — at 390px the
    tile's text column is 133px, so a long branch still ellipsizes. What must
    not regress is the pill collapsing back to a few-character stub.
    """
    sel = '.coding-item[data-id="realbranch"] .git-branch-tag'
    expect(rows.locator(sel)).to_have_text(REAL_BRANCH)

    visible = stable_read(
        lambda: rows.locator(sel).evaluate("el => el.clientWidth")
    )
    assert isinstance(visible, int) and visible >= MIN_PILL_PX, (
        f"branch pill shows only {visible}px at {PHONE_W}px — expected "
        f">= {MIN_PILL_PX}px. It is sharing the line with the name again, or "
        "something re-capped its width."
    )


def test_pathological_branch_truncates_and_keeps_the_full_name(rows: Page) -> None:
    """Longer than the tile → ellipsis, with the full name still in `title`."""
    sel = '.coding-item[data-id="offmain"] .git-branch-tag'
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


@pytest.mark.parametrize("row_id", ["onmain", "offmain"])
def test_row_buttons_meet_the_44px_target_and_do_not_overlap(
    rows: Page, row_id: str
) -> None:
    """Real geometry, not a ::before expansion — so targets never overlap."""
    row = _box(rows, f'.coding-item[data-id="{row_id}"]')
    boxes = _button_boxes(rows, row_id)

    for i, box in enumerate(boxes):
        assert box["width"] >= 44, f"{row_id}: button {i} is {box['width']}px wide"
        assert box["height"] >= 44, f"{row_id}: button {i} is {box['height']}px tall"
        assert box["x"] >= row["x"] - 0.5, f"{row_id}: button {i} clipped on the left"
        assert box["x"] + box["width"] <= row["x"] + row["width"] + 0.5, (
            f"{row_id}: button {i} clipped off the right edge at {PHONE_W}px"
        )

    for i in range(len(boxes) - 1):
        left, right = boxes[i], boxes[i + 1]
        assert left["x"] + left["width"] <= right["x"] + 0.5, (
            f"{row_id}: buttons {i} and {i + 1} overlap horizontally"
        )


def test_agent_launch_is_the_rightmost_button(rows: Page) -> None:
    """Order is Repository · Star · Copilot (issue #8).

    The launch is the row's primary action and takes the far-right slot, the
    easiest thumb reach. It cannot sit further right than this — the strip
    already ends at the list's content edge, with only the summary's chevron
    column beyond it (too narrow for a --row-sm target).
    """
    buttons = rows.locator('.coding-item[data-id="onmain"] .icon-btn')
    expect(buttons).to_have_count(3)
    expect(buttons.nth(2)).to_have_attribute("data-agent", "copilot")
    expect(buttons.nth(1)).to_have_class(re.compile(r"\bstar-btn\b"))
    expect(buttons.nth(0)).to_have_attribute("aria-label", "Repository issues")

    boxes = _button_boxes(rows, "onmain")
    assert boxes[2]["x"] > boxes[1]["x"] > boxes[0]["x"], (
        "DOM order and visual order disagree — the strip is not left-to-right"
    )


def test_row_buttons_align_with_the_summary_controls(rows: Page) -> None:
    """The three row buttons sit directly under the summary's three controls.

    Cloud / rotate / star in the Projects summary head, Copilot / GitHub /
    star in every row — three straight vertical lines. They only line up
    because both clusters reserve the same trailing column for the
    disclosure chevron (--action-cluster-trail) and step at the same pitch;
    a change to either side alone shows up here.

    Phone-width only, by design: above 520px the Favorites filter regains
    its text label and is deliberately wider than a row button.
    """
    header_ids = ["#codingDetached", "#codingResume", "#favFilterBtn"]
    header = []
    for sel in header_ids:
        box = stable_read(lambda sel=sel: rows.locator(sel).bounding_box())
        assert box, f"{sel} never laid out"
        header.append(round(box["x"] + box["width"] / 2, 1))

    row_centres = [
        round(b["x"] + b["width"] / 2, 1) for b in _button_boxes(rows, "onmain")
    ]

    assert header == row_centres, (
        f"summary controls are centred at {header} but the row buttons at "
        f"{row_centres} — the two clusters have drifted out of alignment"
    )


def test_all_three_glyphs_render_on_the_same_grid(rows: Page) -> None:
    """The star is an inline SVG, its neighbours are <img> — same box anyway.

    Regression for the "haphazard" look: the star used to render 20px and
    chip-less beside two 24px chipped brand logos.
    """
    glyphs = rows.locator(
        '.coding-item[data-id="onmain"] .icon-btn .agent-icon, '
        '.coding-item[data-id="onmain"] .icon-btn .icon'
    )
    expect(glyphs).to_have_count(3)

    boxes = []
    for i in range(3):
        box = stable_read(lambda i=i: glyphs.nth(i).bounding_box())
        assert box, f"glyph {i} never laid out"
        boxes.append(box)

    widths = {round(b["width"], 1) for b in boxes}
    heights = {round(b["height"], 1) for b in boxes}
    assert widths == {24.0}, f"glyphs are not all 24px wide: {sorted(widths)}"
    assert heights == {24.0}, f"glyphs are not all 24px tall: {sorted(heights)}"

    # Same vertical centre line — "perfectly aligned", not just same-sized.
    centres = {round(b["y"] + b["height"] / 2, 1) for b in boxes}
    assert len(centres) == 1, f"glyphs sit on different centre lines: {sorted(centres)}"
