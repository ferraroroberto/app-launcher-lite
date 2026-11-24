"""Regression pin for #2414: ahead/behind chips, the pull-first gate, Sync all.

A project behind its remote carries a "behind N" chip apart from the dirty
chip, and launching it asks to pull first (one tap: Pull and launch). A
behind project that cannot be pulled (dirty, diverged) says why and offers
only "Launch anyway", which is the one way past the server's refusal. Sync
all counts the projects while it runs and keeps a report until Done.

Every endpoint is mocked before navigation with fictional projects, so no
real repo is fetched or pulled.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock

pytestmark = pytest.mark.smoke

_APPS = [
    {"id": p, "name": p, "kind": "claude-code", "project_dir": "", "added_at": "",
     "is_favorite": False, "repo_url": None}
    for p in ("alpha-site", "beta-tools")
]
_GIT = {"projects": [
    {"id": "alpha-site", "is_git": True, "branch": "main", "default_branch": "main",
     "on_default_branch": True, "dirty": False, "restart_needed": False,
     "upstream": {"state": "behind", "ahead": 0, "behind": 2, "reason": ""}},
    {"id": "beta-tools", "is_git": True, "branch": "main", "default_branch": "main",
     "on_default_branch": True, "dirty": True, "restart_needed": True,
     "upstream": {"state": "diverged", "ahead": 1, "behind": 3, "reason": ""}},
]}
def _fulfil(body: dict):
    return lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _mock(page: Page, calls: list, sync: dict) -> None:
    """Sync all is slow: its POST is held in ``sync["held"]`` until the test answers it,
    and the progress GET reads ``sync["progress"]``."""
    page.route("**/api/apps", _fulfil({"scan_root": "", "apps": _APPS}))
    page.route("**/api/apps/running", _fulfil({"running": []}))
    page.route("**/api/agents", _fulfil({"agents": [
        {"id": "copilot", "label": "GitHub Copilot CLI", "available": True, "fullscreen": False},
    ], "vscode_available": False}))
    page.route(re.compile(r".*/api/claude-code/git-status(\?.*)?$"), _fulfil(_GIT))

    def record(body: dict):
        def handler(route):
            calls.append((route.request.url, route.request.post_data or ""))
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        return handler

    page.route("**/api/claude-code/pull/*", record({"outcome": "pulled", "commits": 2, "reason": ""}))
    page.route("**/api/apps/*/launch", record({"launched": "x"}))

    def sync_route(route):
        if route.request.method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(sync["progress"]))
        else:
            sync["held"].append(route)

    page.route("**/api/claude-code/sync-all", sync_route)
    page.route("**/api/ports/probe", lambda route: None)


def test_behind_chips_pull_first_gate_and_sync_all(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _fake_clock.install(page)
    running = {"run": 1, "state": "running", "done": 0, "total": 4, "current": "alpha-site", "results": [],
               "started_at": "2026-01-15T08:50:00Z", "finished_at": None}
    sync: dict = {"held": [], "progress": running}
    calls: list = []
    _mock(page, calls, sync)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")

    alpha = page.locator('.coding-item[data-id="alpha-site"]')
    beta = page.locator('.coding-item[data-id="beta-tools"]')
    expect(alpha.locator(".git-behind")).to_have_text("behind 2")
    expect(alpha.locator(".git-behind")).to_have_attribute("data-tone", "attention")
    expect(alpha.locator(".git-uncommitted")).to_have_count(0)
    expect(beta.locator(".git-uncommitted")).to_have_text("uncommitted")
    expect(beta.locator(".git-diverged")).to_have_text("diverged")
    expect(beta.locator(".git-restart-needed")).to_have_text("restart needed")

    # A pullable behind repo: Pull and launch pulls, then launches.
    dialog = page.locator("#confirmDialog")
    alpha.locator(".action-row-main").click()
    expect(dialog.locator("#confirmDialogTitle")).to_have_text("Pull alpha-site first?")
    expect(dialog.locator("#confirmDialogMessage")).to_contain_text("2 commits behind origin/main")
    with page.expect_request("**/api/apps/alpha-site/launch") as launched:
        dialog.locator("#confirmDialogOk").click()
    assert "ignore_behind" not in (launched.value.post_data or "")
    # The pull was answered before the launch was even sent.
    assert any("/api/claude-code/pull/alpha-site" in u for u, _ in calls)

    # An unpullable one says why, and only "Launch anyway" sends ignore_behind.
    beta.locator(".action-row-main").click()
    expect(dialog.locator("#confirmDialogTitle")).to_have_text("beta-tools is behind")
    expect(dialog.locator("#confirmDialogMessage")).to_contain_text("uncommitted changes")
    expect(dialog.locator("#confirmDialogOk")).to_have_text("Launch anyway")
    with page.expect_request("**/api/apps/beta-tools/launch") as launched:
        dialog.locator("#confirmDialogOk").click()
    assert json.loads(launched.value.post_data or "{}").get("ignore_behind") is True

    # Sync all counts the projects while it runs, then keeps a report.
    page.locator("#gitSyncAllBtn").click()
    sheet = page.locator("#gitSyncSheet")
    state = sheet.locator("#gitSyncState")
    expect(state).to_contain_text("Syncing projects")
    _fake_clock.jump_interval(page, 1000, "sync-all")
    expect(state).to_contain_text("Syncing 1 of 4 · alpha-site")

    # Closed, the count stays on the page in a toast and follows the run.
    sheet.locator("#gitSyncClose").click()
    expect(page.locator("#toast")).to_contain_text("Syncing 1 of 4 · alpha-site")
    sync["progress"] = {**running, "done": 2, "current": "gamma-docs"}
    _fake_clock.jump_interval(page, 1000, "sync-all")
    expect(page.locator("#toast")).to_contain_text("Syncing 3 of 4 · gamma-docs")

    # The run ends: the toast carries the totals and opens the full report.
    row = {"restored": [], "kept_aside": []}
    sync["held"][0].fulfill(status=200, content_type="application/json", body=json.dumps({"results": [
        {"id": "a", "name": "alpha-site", "outcome": "pulled", "reason": "", "commits": 3, **row},
        {"id": "b", "name": "beta-tools", "outcome": "up-to-date", "reason": "", "commits": 0, **row},
        {"id": "c", "name": "gamma-docs", "outcome": "skipped", "reason": "dirty", "commits": 0, **row},
        {"id": "d", "name": "delta-lab", "outcome": "failed", "reason": "index.lock exists", "commits": 0, **row},
    ]}))
    expect(page.locator("#toast")).to_contain_text("Synced 4: 1 fast-forwarded · 1 current · 1 skipped · 1 failed")
    page.locator("#toast .toast-action").click()
    summary = sheet.locator("#gitSyncSummary")
    expect(summary.locator('li[data-summary="pulled"]')).to_contain_text("Fast-forwarded1")
    expect(summary.locator('li[data-summary="current"]')).to_contain_text("Already current1")
    expect(summary.locator('li[data-summary="skipped"]')).to_contain_text("uncommitted changes (1)")
    expect(summary.locator('li[data-summary="failed"]')).to_contain_text("Failed1")
    expect(sheet.locator('li[data-outcome="failed"]')).to_contain_text("index.lock exists")
    sheet.locator("#gitSyncDone").click()
    expect(sheet).not_to_be_visible()
