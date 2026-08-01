"""Regression pin for issue #666 (Coding row per-agent visibility toggles).

The feature: the ⚙️ Coding options card carries a "Visible agents" list —
one vendored switch per registry agent plus the GitHub issues button —
generated from /api/agents, never hand-written per agent. Toggling one off
drops that button from every project row immediately and persists as
`coding_hidden_agents` in the webapp config, so it stays hidden across a
reload.

Reduced to the lite fork's single-agent registry: the Copilot launch button
is present, and the GitHub pseudo-button hides/persists/restores.

Approach: /api/apps and /api/agents are mocked for a deterministic row and
agent set, but the config write is **real** — the e2e conftest points the
webapp at a throwaway `webapp_config.json`, so the reload assertion proves
actual server-side persistence rather than a client-side illusion.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

AGENTS = [
    {"id": "copilot", "label": "GitHub Copilot CLI", "available": True,
     "fullscreen": True},
]


def _install_routes(page: Page) -> None:
    page.route(
        "**/api/apps",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "scan_root": "E:/automation",
                    "apps": [
                        {
                            "id": "alpha",
                            "name": "alpha",
                            "kind": "coding",
                            "project_dir": "E:/automation/alpha",
                            "added_at": "",
                            "is_favorite": False,
                            "repo_url": "https://github.com/ferraroroberto/alpha",
                        }
                    ],
                }
            ),
        ),
    )
    page.route(
        "**/api/agents",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"agents": AGENTS}),
        ),
    )


def _open_surfaces(page: Page) -> None:
    # Projects and the options card are both collapsed by default.
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    page.locator("#codingOptions").evaluate("el => { el.open = true; }")


def _copilot_btn(page: Page):
    return page.locator(
        '.coding-item[data-id="alpha"] .agent-btn[data-agent="copilot"]'
    )


def _github_btn(page: Page):
    return page.locator('.coding-item[data-id="alpha"] .agent-btn').filter(
        has=page.locator('img[alt="GitHub"]')
    )


def _reset_visibility(page: Page, base_url: str) -> None:
    """Start from a known baseline — every button visible.

    The autoboot fixture boots the disposable webapp from a *copy of the real
    config* (issue #441) so values are realistic, which means whatever the
    developer has hidden in their own launcher is the starting state here.
    Asserting an empty default would fail on any machine with a hidden agent,
    so the baseline is set rather than assumed. Loopback bypasses the bearer
    middleware, so a plain request needs no token.
    """
    page.request.post(
        f"{base_url}/api/config", data={"coding_hidden_agents": []}
    )


def test_copilot_button_present_and_github_toggle_persists(
    authed_page: Page, base_url: str
) -> None:
    _install_routes(authed_page)
    _reset_visibility(authed_page, base_url)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_surfaces(authed_page)

    # The list is generated from the registry: one row per agent + GitHub.
    copilot_toggle = authed_page.locator('[data-visibility-toggle="copilot"]')
    github_toggle = authed_page.locator('[data-visibility-toggle="github"]')
    expect(copilot_toggle).to_have_attribute("aria-checked", "true", timeout=5_000)
    expect(github_toggle).to_have_count(1)
    expect(_copilot_btn(authed_page)).to_have_count(1)
    expect(_github_btn(authed_page)).to_have_count(1)

    # Toggling GitHub off drops its button from the row with no reload.
    github_toggle.click()
    expect(_github_btn(authed_page)).to_have_count(0)
    # Copilot stays — hiding is per button, not all-or-nothing.
    expect(_copilot_btn(authed_page)).to_have_count(1)
    # The favorite star is never hideable.
    expect(authed_page.locator('.coding-item[data-id="alpha"] .star-btn')).to_have_count(1)

    # Persisted server-side: a reload keeps it hidden and the switch off.
    authed_page.reload(wait_until="domcontentloaded")
    _open_surfaces(authed_page)
    expect(authed_page.locator('[data-visibility-toggle="github"]')).to_have_attribute(
        "aria-checked", "false", timeout=5_000
    )
    expect(_github_btn(authed_page)).to_have_count(0)
    expect(_copilot_btn(authed_page)).to_have_count(1)

    # Toggling back on restores the button.
    authed_page.locator('[data-visibility-toggle="github"]').click()
    expect(_github_btn(authed_page)).to_have_count(1)
