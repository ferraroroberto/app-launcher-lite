"""Coding tab: the repo-issues entry in a project row's menu.

The backend gives each Coding project a ``repo_url`` (any host) and a
server-built, pre-filtered ``issues_url`` (GitLab ``/-/issues?...``). The menu
item opens ``issues_url`` verbatim, is labelled "Open GitLab issues", and is
disabled ("No GitLab remote") when ``issues_url`` is absent.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import open_row_menu

pytestmark = pytest.mark.smoke


def _row(slug: str, issues_url: str | None) -> dict:
    return {
        "id": slug,
        "name": slug,
        "kind": "claude-code",
        "project_dir": f"E:/automation/{slug}",
        "added_at": "",
        "is_favorite": False,
        "repo_url": issues_url.split("/-/issues")[0] if issues_url else None,
        "issues_url": issues_url,
    }


def _install_routes(page: Page, repo: dict) -> None:
    """Mock /api/apps with one row whose ``issues_url`` is read per request,
    so a test can drop the remote and reload."""
    def _apps(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"scan_root": "E:/automation", "apps": [_row("alpha", repo["url"])]}
            ),
        )

    page.route("**/api/apps", _apps)


def _open_projects(page: Page) -> None:
    # Projects is collapsed by default (#383 review round) — expand it so
    # the tile buttons are clickable.
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")


def _issues_item(page: Page):
    """The issues row in the ⋯ menu, with the menu opened."""
    anchor = page.locator('.coding-item[data-id="alpha"] .project-menu-anchor')
    expect(anchor).to_be_enabled(timeout=5_000)
    anchor.click()
    menu = open_row_menu(page)
    expect(menu).to_be_visible()
    return menu.locator("[data-item=project-github-btn]")


def test_issues_item_opens_issues_url_verbatim_and_is_disabled_without_a_remote(
    authed_page: Page, base_url: str
) -> None:
    issues_url = (
        "https://gitlab.example.com/team/app-launcher/-/issues"
        "?state=opened&sort=updated_desc&not%5Blabel_name%5D%5B%5D=audit-meta"
    )
    repo = {"url": issues_url}
    _install_routes(authed_page, repo)
    # Capture window.open before the SPA loads — don't actually navigate.
    authed_page.add_init_script(
        "window.__opened = [];"
        "window.open = function (u) { window.__opened.push(u); return null; };"
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    expect(authed_page.locator("#claudeList .coding-item")).to_have_count(1)
    gh_btn = _issues_item(authed_page)
    expect(gh_btn).to_be_enabled()
    expect(gh_btn).to_have_attribute("aria-label", "Open GitLab issues")
    gh_btn.click()

    opened = authed_page.evaluate("window.__opened")
    assert opened == [issues_url], f"window.open called with {opened!r}, expected [{issues_url!r}]"

    # No issues_url: the row stays, its issues item greyed.
    repo["url"] = None
    authed_page.reload(wait_until="domcontentloaded")
    _open_projects(authed_page)
    expect(authed_page.locator("#claudeList .coding-item")).to_have_count(1)
    item = _issues_item(authed_page)
    expect(item).to_be_disabled()
    expect(item).to_have_attribute("aria-label", "No GitLab remote")
