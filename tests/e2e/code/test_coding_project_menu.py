"""The Coding row's ⋯ project menu.

Every Coding-tab project row is exactly three controls wide: the star, the row
itself (which launches Copilot) and this ⋯ anchor. The menu holds everything
else: the GitLab issues entry, then the three project actions (Open in VS
Code, Show changes — the read-only working-tree overlay, pinned separately in
test_coding_changes_overlay.py — and Open folder, Explorer on the PC).

The VS Code item is greyed with a hover hint when the `code` CLI isn't on
PATH; Show changes is hidden for a folder git-status says isn't a repository.

/api/apps, /api/agents and /api/claude-code/git-status are mocked so the
row, the agent set, the `code`-installed flag and the git state are
deterministic; the two POSTs are mocked too so the suite never spawns a real
editor or Explorer on the dev box.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.fixtures.ui import open_row_menu

pytestmark = pytest.mark.smoke

AGENTS = [
    {"id": "copilot", "label": "GitHub Copilot CLI", "available": True, "fullscreen": False},
]


def _app(app_id: str) -> dict:
    return {
        "id": app_id,
        "name": app_id,
        "kind": "claude-code",
        "project_dir": f"E:/stubfolder/{app_id}",
        "added_at": "",
        "is_favorite": False,
        "repo_url": f"https://gitlab.example.com/team/{app_id}",
        "issues_url": f"https://gitlab.example.com/team/{app_id}/-/issues?state=opened",
    }


def _install_routes(page: Page, *, vscode_available: bool) -> dict:
    """Mock the boot fetches; return the POST log.

    Mocked before `goto()` per this repo's #510 convention — a late
    /api/agents or git-status response otherwise re-renders the row out from
    under a click. `alpha` is a git repo, `plain` is not.
    """
    posted: dict = {"vscode": [], "folder": []}

    page.route(
        "**/api/apps",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"scan_root": "E:/stubfolder", "apps": [_app("alpha"), _app("plain")]}),
        ),
    )
    page.route(
        "**/api/agents",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"agents": AGENTS, "vscode_available": vscode_available}),
        ),
    )
    page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"projects": [
                {"id": "alpha", "is_git": True, "branch": "main", "default_branch": "main",
                 "on_default_branch": True, "dirty": False},
                {"id": "plain", "is_git": False, "branch": None, "default_branch": None,
                 "on_default_branch": True, "dirty": False},
            ]}),
        ),
    )

    def _vscode(route):
        posted["vscode"].append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "workspace": "E:/stubfolder/alpha.code-workspace",
                             "created": True, "pid": 4321}),
        )

    def _folder(route):
        posted["folder"].append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "path": "E:/stubfolder/alpha", "pid": 99}),
        )

    page.route("**/api/claude-code/vscode/**", _vscode)
    page.route("**/api/claude-code/folder/**", _folder)
    return posted


def _open_projects(page: Page) -> None:
    # Projects is collapsed by default (#383 review round).
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")


def _anchor(page: Page, app_id: str = "alpha"):
    return page.locator(f'.coding-item[data-id="{app_id}"] .project-menu-anchor')


def _menu(page: Page):
    """The open ⋯ menu, whichever row opened it (it is mounted on <body>)."""
    return open_row_menu(page)


def test_row_carries_three_controls_and_the_menu_holds_the_rest(
    authed_page: Page, base_url: str
) -> None:
    posted = _install_routes(authed_page, vscode_available=True)
    _fake_clock.install(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    expect(_anchor(authed_page)).to_be_enabled(timeout=5_000)

    # Order contract (#1128, the action-row): the leading star, the row
    # itself (which launches Copilot), the ⋯ kebab — three
    # controls, nothing else. One synchronous evaluate() so the read can't
    # straddle the ~4 s apps re-render (#680).
    marks = authed_page.evaluate(
        """() => Array.from(
            document.querySelectorAll('.coding-item[data-id="alpha"] > button')
        ).map(el => el.classList.contains('star-btn')
            ? 'star'
            : (el.dataset.agent || 'menu'))"""
    )
    assert marks == ["star", "copilot", "menu"], marks
    expect(_menu(authed_page)).to_be_hidden()

    _anchor(authed_page).click()
    menu = _menu(authed_page)
    expect(menu).to_be_visible()
    expect(_anchor(authed_page)).to_have_attribute("aria-expanded", "true")
    # Everything that left the row is here: the issues entry, then the three
    # project actions.
    expect(menu.locator(".row-menu-label")).to_have_text(
        [
            "GitLab issues",
            "Open in VS Code",
            "Show changes",
            "Open folder",
        ]
    )

    # -- was test_menu_closes_on_escape_outside_tap_and_survives_a_rerender
    # (merged in #1215; before the POST taps below, which it leaves the menu
    # open for) --
    anchor = _anchor(authed_page)
    authed_page.keyboard.press("Escape")
    expect(_menu(authed_page)).to_be_hidden()
    expect(anchor).to_have_attribute("aria-expanded", "false")

    anchor.click()
    expect(_menu(authed_page)).to_be_visible()
    # An outside tap closes it: the page header's title, outside the menu at
    # every layout (on a wide window the page's top-left corner is the nav
    # rail, #1135).
    authed_page.locator("#paneClaude .home-title").click()
    expect(_menu(authed_page)).to_be_hidden()

    # Survives the list rebuild the apps poll does every few seconds: the
    # open row's key is remembered and the menu reopens on the rebuilt row.
    anchor.click()
    expect(_menu(authed_page)).to_be_visible()
    # Jump the 4 s apps poll (TUNNEL_POLL_MS) and prove it rebuilt the row: a
    # marker on the row's node is gone once the poll has replaced it, so the
    # survival checks below can't pass against a list that never re-rendered.
    authed_page.evaluate(
        "() => { document.querySelector('.coding-item[data-id=\"alpha\"]').__beforePoll = true; }"
    )
    # By name: the Jobs poll shares the 4 s period and is armed first, so the
    # period alone returns before boot's `lists` has armed the apps poll (#1405).
    _fake_clock.wait_for_interval(authed_page, 4_000, calls="fetchApps")
    for _ in range(10):
        _fake_clock.advance(authed_page, 4_000)
        if authed_page.evaluate(
            "() => !document.querySelector('.coding-item[data-id=\"alpha\"]').__beforePoll"
        ):
            break
    else:
        raise AssertionError("the apps poll never rebuilt the Coding list on the fake clock")
    expect(_menu(authed_page)).to_be_visible()
    expect(_anchor(authed_page)).to_have_attribute("aria-expanded", "true")

    # Open in VS Code: #802's POST, unchanged, and the menu closes on the tap.
    menu.locator("[data-item=project-vscode-btn]").click()
    expect(authed_page.locator("#toast")).to_contain_text("alpha.code-workspace", timeout=5_000)
    assert posted["vscode"] and posted["vscode"][0].endswith("/api/claude-code/vscode/alpha")
    expect(menu).to_be_hidden()

    # Open folder: its own POST, toast names Explorer.
    _anchor(authed_page).click()
    menu.locator("[data-item=project-folder-btn]").click()
    expect(authed_page.locator("#toast")).to_contain_text("Explorer", timeout=5_000)
    assert posted["folder"] and posted["folder"][0].endswith("/api/claude-code/folder/alpha")
    expect(menu).to_be_hidden()


def test_vscode_item_disabled_when_cli_missing_and_show_changes_needs_a_repo(
    authed_page: Page, base_url: str
) -> None:
    """The VS Code item is greyed with a hint when the `code` CLI is missing,
    and Show changes is offered only for a git repository."""
    posted = _install_routes(authed_page, vscode_available=False)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    _anchor(authed_page).click()
    item = _menu(authed_page).locator("[data-item=project-vscode-btn]")
    expect(item).to_be_disabled(timeout=5_000)
    expect(item).to_have_attribute("title", "Visual Studio Code is not installed")
    expect(_menu(authed_page).locator("[data-item=project-changes-btn]")).to_have_count(1)
    authed_page.keyboard.press("Escape")
    assert posted["vscode"] == []

    # `plain` isn't a git repo (mocked git-status): no Show changes item,
    # the other two still there.
    _anchor(authed_page, "plain").click()
    menu = _menu(authed_page)
    expect(menu).to_be_visible()
    expect(menu.locator("[data-item=project-changes-btn]")).to_have_count(0)
    expect(menu.locator("[data-item=project-folder-btn]")).to_have_count(1)
    authed_page.keyboard.press("Escape")
