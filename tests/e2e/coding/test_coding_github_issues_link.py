"""Regression pin for issue #297 (Coding tab repo icon → issues-page link).

The feature: the Coding tab's repo icon opens the repo's issues page in a
new tab. Since Phase 5 the URL is precomputed server-side
(``repo_issues_url`` — GitHub keeps ``/issues``, any other host gets
GitLab's ``/-/issues``; the host check lives in ``src/registry.py`` /
``src/scanner.py``) and the client uses it verbatim — no URL synthesis in
apps.js.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _row(slug: str, repo_url: str | None, repo_issues_url: str | None) -> dict:
    return {
        "id": slug,
        "name": slug,
        "kind": "coding",
        "project_dir": f"E:/automation/{slug}",
        "added_at": "",
        "is_favorite": False,
        "repo_url": repo_url,
        "repo_issues_url": repo_issues_url,
    }


def _install_routes(
    page: Page, repo_url: str | None, repo_issues_url: str | None
) -> None:
    def _apps(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "scan_root": "E:/automation",
                    "apps": [_row("alpha", repo_url, repo_issues_url)],
                }
            ),
        )

    page.route("**/api/apps", _apps)


def _open_projects(page: Page) -> None:
    # Projects is collapsed by default (#383 review round) — expand it so
    # the tile buttons are clickable.
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")


def _repo_btn(page: Page):
    # The glyph is the fork's only forge, GitLab (issue #13) — the config
    # pseudo-id behind the button is still `github` for back-compat, but
    # nothing user-visible says GitHub any more.
    return page.locator('.coding-item[data-id="alpha"] .agent-btn').filter(
        has=page.locator('img[alt="GitLab"]')
    )


def test_repo_icon_opens_server_computed_issues_url(
    authed_page: Page, base_url: str
) -> None:
    repo_url = "https://gitlab.com/testgroup/app-launcher"
    issues_url = "https://gitlab.com/testgroup/app-launcher/-/issues"
    _install_routes(authed_page, repo_url, issues_url)
    # Capture window.open before the SPA loads — don't actually navigate.
    authed_page.add_init_script(
        "window.__opened = [];"
        "window.open = function (u) { window.__opened.push(u); return null; };"
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    expect(authed_page.locator("#codingList .coding-item")).to_have_count(1)
    btn = _repo_btn(authed_page)
    expect(btn).to_be_enabled(timeout=5_000)
    expect(btn).to_have_attribute("aria-label", "Repository issues")
    btn.click()

    opened = authed_page.evaluate("window.__opened")
    # Verbatim — the client must not append query/synthesize anything.
    assert opened == [issues_url], (
        f"window.open called with {opened!r}, expected [{issues_url!r}]"
    )


def test_repo_icon_disabled_without_issues_url(
    authed_page: Page, base_url: str
) -> None:
    _install_routes(authed_page, None, None)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    expect(authed_page.locator("#codingList .coding-item")).to_have_count(1)
    expect(_repo_btn(authed_page)).to_be_disabled()
