"""The Team OS tab: skill rows, the launch model and switches, the weekly
recap row and the read-only content browser.

Every Team OS route is route-mocked with fictional data, so the test reads no
real team-os checkout and launches no session: the launch POSTs are captured
and answered with a detached session (which opens no terminal). The model
combo's options come from the real disposable config, whose Copilot model list
``fixtures/launch_target.py`` pins.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.fixtures.pages import lazy_module_url, loopback
from tests.e2e.fixtures.sessions import auth_headers
from tests.e2e.fixtures.ui import open_row_menu
from tests.e2e.fixtures.waits import wait_until

pytestmark = pytest.mark.smoke

_SKILLS = {"available": True, "team_os_dir": "C:/stub/team", "skills": [
    {"id": "weekly-plan", "name": "weekly-plan", "command": "weekly-plan",
     "description": "Plan the week from the backlog.", "skill_md": "x"},
    {"id": "standup", "name": "standup", "command": "standup",
     "description": "Draft the daily standup.", "skill_md": "x"},
]}
_RECAP = {"available": True, "ledger_exists": True, "age_days": 9.0, "staleness": "due",
          "proposal_pending": True, "proposal_name": "2026-01-14.md"}
_FILES = {"files": [
    {"path": ".claude/skills/standup/SKILL.md", "name": "SKILL.md", "category": "skill"},
    {"path": ".claude/skills/standup/conversations/2026-01-14-0900-monday.md",
     "name": "2026-01-14-0900-monday.md", "category": "conversations"},
]}
_DOC = {"path": "x", "name": "SKILL.md", "truncated": False,
        "content": "# Standup\n\nWhat moved **yesterday**, what moves today."}


def _json(body: dict):
    return lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _mock(page: Page) -> list:
    """Serve the Team OS reads; return the launch bodies as they arrive."""
    launches: list = []

    def _launch(route: Route) -> None:
        launches.append((route.request.url, route.request.post_data_json))
        route.fulfill(json={"launched": "x", "session": {
            "session_id": "s-team", "kind": "remote", "name": "standup"}})

    page.route(re.compile(r".*/api/team-os/skills$"), _json(_SKILLS))
    page.route(re.compile(r".*/api/team-os/recap-status$"), _json(_RECAP))
    page.route(re.compile(r".*/api/team-os/skills/standup/files$"), _json(_FILES))
    page.route(re.compile(r".*/api/team-os/file\?.*$"), _json(_DOC))
    page.route(re.compile(r".*/api/team-os/(skills/[^/]+|recap)/launch$"), _launch)
    return launches


def test_team_os_lists_skills_launches_with_the_picked_model_and_reads_files(
    authed_page: Page, base_url: str, auth_token: str
) -> None:
    page = authed_page
    launches = _mock(page)
    models = loopback("GET", base_url + "/api/config",
                      headers=auth_headers(auth_token)).json()["copilot"]["models_available"]
    # The tab's code loads on its first visit: held here, the visit draws no
    # rows until it lands, then reads the skills itself.
    held: list = []
    code = lazy_module_url("team-os.js")
    page.route(code, lambda route: held.append(route))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabTeamOS").click()

    rows = page.locator("#teamOsList li.teamos-item")
    expect(page.locator("#paneTeamOS")).to_be_visible()
    wait_until(page, lambda: bool(held), "the Team OS code request")
    expect(rows).to_have_count(0)
    for route in held:
        route.continue_()
    page.unroute(code)
    expect(rows).to_have_count(2)
    # A to Z, each with its description as the context line.
    expect(rows.locator(".action-row-title")).to_have_text(["standup", "weekly-plan"])
    expect(rows.first).to_contain_text("Draft the daily standup.")
    expect(page.locator("#teamHeadStatus")).to_have_text("recap due")

    # The model combo: Default (Copilot picks) plus the config's models.
    page.locator("#teamOsModelBtn").click()
    options = page.locator("#teamOsModelMenu [role=option]")
    expect(options).to_have_text(["Default", *models])
    options.nth(1).click()
    expect(page.locator("#teamOsModelBtn")).to_have_text(models[0])

    # Detached on, so the answer opens no terminal; a row's tap launches.
    page.locator("#teamOsDetached").click()
    expect(page.locator("#teamOsDetached")).to_have_attribute("aria-checked", "true")
    rows.first.locator(".action-row-main").click()
    expect(page.locator("#toast")).to_contain_text(f"Launched standup ({models[0]}) (detached)")
    url, body = launches[-1]
    assert url.endswith("/api/team-os/skills/standup/launch"), url
    assert body == {"mode": "remote", "model": models[0], "resume": False}, body

    # The kebab's Pick a session… asks for Copilot's picker.
    rows.first.locator(".action-row-kebab").click()
    open_row_menu(page).locator("[data-item=teamos-pick-session-btn]").click()
    expect(page.locator("#toast")).to_contain_text("Opening Copilot’s session picker for standup")
    assert launches[-1][1]["resume"] is True

    # The recap row: its chips name the exceptions, ▶ launches /weekly-recap.
    recap = page.locator("#teamOsRecapList .teamos-recap-item")
    expect(recap).to_contain_text("last run 9 days ago")
    expect(recap.locator(".teamos-recap-chip")).to_have_text(["due", "draft ready"])
    page.locator("#teamOsRecapLaunch").click()
    expect(page.locator("#toast")).to_contain_text("Launched weekly recap")
    assert launches[-1][0].endswith("/api/team-os/recap/launch")

    # Read: the browser lists the files by category; a log opens with its
    # Rename and Delete, any other file reads only.
    rows.first.locator(".action-row-kebab").click()
    open_row_menu(page).locator("[data-item=teamos-browse-btn]").click()
    browser = page.locator("#teamOsBrowser")
    expect(browser).to_be_visible()
    expect(page.locator("#teamOsFileList .teamos-file-cat")).to_have_text(["Skill", "Conversations"])
    page.locator("#teamOsFileList .teamos-file-btn", has_text="SKILL.md").click()
    expect(page.locator("#teamOsFileContent h1")).to_have_text("Standup")
    expect(page.locator("#teamOsDocLinks")).to_be_hidden()
    page.locator("#teamOsDocDone").click()
    page.locator("#teamOsFileList .teamos-file-btn", has_text="monday").click()
    expect(page.locator("#teamOsDocLinks")).to_be_visible()
    page.locator("#teamOsDocDone").click()
    page.locator("#teamOsBrowserDone").click()
    expect(browser).to_be_hidden()
