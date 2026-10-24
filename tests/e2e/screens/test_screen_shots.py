"""The screenshot baseline's capture: the five work tabs at phone and desktop, light and dark.

`scripts/shot_baseline.py` compares what this writes under `tests/e2e/shots`
with the committed PNGs, so a move-only refactor proves it changed no pixel.
Every fetch that carries data, a clock or a build identity is route-mocked
with fictional values, so a shot never shows this machine's apps, jobs,
sessions or folders, and two runs of one commit write the same pixels.
Chromium only: this app is Android-only, so the phone variant is Chromium's
own 390x844 mobile emulation rather than a device profile.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e._shot import PINNED_NOW, shot, variant_page, variants

pytestmark = pytest.mark.smoke

SHOTS_DIR = Path(__file__).resolve().parents[1] / "shots"
NOW = int(PINNED_NOW.timestamp())
STAMP = "2026-01-15T08:50:00Z"


def _job(job_id: str, name: str, **extra) -> dict:
    job = {
        "id": job_id, "name": name, "target_kind": "python", "schedule_chip": "daily 03:00",
        "next_run": "tomorrow 03:00", "next_run_epoch": NOW + 6 * 3600, "running": False,
        "stuck": False, "paused": False, "args": "", "params": [],
        "schedule": {"type": "daily", "at": "03:00"}, "last_run": None,
        "stats": {"p50": None, "p95": None, "success_rate_30d": None, "completed_count": 0, "last7": []},
    }
    return {**job, **extra}


def _app(app_id: str, name: str, kind: str, **extra) -> dict:
    return {"id": app_id, "name": name, "kind": kind, "bat_path": f"C:\\stub\\{app_id}\\run.bat",
            "added_at": "2026-01-01T00:00:00", "autostart": False, **extra}


def _skill(name: str) -> dict:
    return {"id": name, "name": name, "command": name, "description": "A fictional skill.",
            "skill_md": f".claude/skills/{name}/SKILL.md"}


_SESSIONS = [
    {"session_id": f"s-{name}", "kind": "pty", "agent": "copilot", "project_dir": f"C:/stub/{name}",
     "name": name, "alive": True, "started_at": NOW - uptime, "live_title": title, "prompt_title": "",
     "manual_title": ""}
    for name, title, uptime in (("alpha", "a fictional task", 600), ("beta", "a fictional question", 3900))
]

_BOARD = {
    "generated_at": STAMP,
    "columns": {
        "backlog": [{"kind": "issue", "repo": "demo-project", "number": 12, "title": "A synthetic queued item",
                     "url": "https://example.invalid/12", "updated_at": STAMP, "labels": ["enhancement"]}],
        "bot_turn": [{"session_id": "s-alpha", "kind": "pty", "agent": "copilot", "project_dir": "C:/stub/alpha",
                      "name": "alpha", "alive": True, "started_at": STAMP, "live_title": "a fictional task",
                      "prompt_title": "", "project": "alpha", "status": "working", "age_seconds": 240}],
        "your_turn": [{"session_id": "s-beta", "kind": "pty", "agent": "copilot", "project_dir": "C:/stub/beta",
                       "name": "beta", "alive": True, "started_at": STAMP, "live_title": "a fictional question",
                       "prompt_title": "", "project": "beta", "status": "awaiting-input", "age_seconds": 720}],
        "done": [{"kind": "issue", "repo": "demo-project", "number": 11, "title": "A synthetic finished item",
                  "url": "https://example.invalid/11", "updated_at": STAMP, "state": "closed", "labels": []}],
    },
    "gitlab": {"fetched_at": STAMP, "error": None},
    "sessions_state": {"available": True, "stale": False, "updated_at": STAMP},
}

_APPS = {"scan_root": "C:\\stub", "apps": [
    _app("alpha-dash", "Alpha Dashboard", "streamlit"), _app("beta-web", "Beta Web", "webapp"),
    _app("gamma-tunnel", "Gamma Tunnel", "tunnel", tunnel_url="https://gamma.example.invalid/", health="up"),
    _app("omega-tray", "Omega Tray", "tray", autostart=True)]}
_JOBS = {"jobs": [
    _job("ledger", "Ledger reconciliation", alert_on_failure=True),
    _job("mailbox", "Mailbox sweep", schedule_chip="every 15 min", next_run_epoch=NOW + 300),
    _job("planner", "Planner sync", paused=True)]}
_CONFIG = {
    "projects_dir": "C:/stub", "projects_ignore": [], "apps_scan_root": "", "team_os_dir": "C:/stub/team-os",
    "copilot": {
        "skip_permissions": False, "model": "default", "models_available": ["default", "model-a", "model-b"],
        "autopilot": False, "context": "default", "contexts_available": ["", "default", "long_context"],
        "effort": "", "efforts_available": ["", "low", "high"], "computed_flags": "",
    },
}

#: endpoint pattern -> body; every fetch the five tabs make that is not static.
_MOCKS = {
    r"/api/version": {"git_sha": "0000000", "built_at": "2026-01-15 08:00"},
    r"/api/status": {"terminal": {"reachable": True, "reason": None}},
    r"/api/config": _CONFIG,
    r"/api/agents": {"agents": [{"id": "copilot", "label": "GitHub Copilot CLI", "available": True,
                                 "fullscreen": True}]},
    r"/api/system/load": {"level": "normal", "reason": None, "cpu_percent": 12.0, "cores": 8, "top": [],
                          "other_cores": None, "age_seconds": 1.0,
                          "memory": {"used_bytes": 8 * 1024**3, "total_bytes": 32 * 1024**3, "percent": 25.0}},
    r"/api/coding/sessions": {"sessions": _SESSIONS},
    r"/api/coding/git-status": {"projects": []},
    r"/api/apps": _APPS,
    r"/api/apps/running": {"running": []},
    r"/api/ports/probe": {"listeners": []},
    r"/api/jobs": _JOBS,
    r"/api/jobs/agenda": {"days": 7, "generated_epoch": NOW, "occurrences": [], "frequent": []},
    r"/api/team-os/skills": {"available": True, "team_os_dir": "C:/stub/team-os",
                             "skills": [_skill("garden-notes"), _skill("reading-list")]},
    r"/api/team-os/recap-status": {"available": False, "ledger_exists": False, "age_days": None,
                                   "staleness": "fresh", "proposal_pending": False, "proposal_name": None},
    r"/api/board": _BOARD,
    r"/api/board/gitlab/refresh": {"fetched_at": STAMP, "error": None},
}

#: tab name -> (tab button, the first row that proves the tab rendered its fixture).
_TABS = {
    "coding": ("#tabCoding", "#sessionsList li"),
    "apps": ("#tabApps", "#appsList li"),
    "jobs": ("#tabJobs", "#jobsList li"),
    "team-os": ("#tabTeamOS", "#teamOsList li"),
    "board": ("#tabBoard", "#boardColumns li.board-item"),
}


def _json_route(body: dict):
    return lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _mock_data(page: Page) -> None:
    for pattern, body in _MOCKS.items():
        page.route(re.compile(rf".*{pattern}(\?.*)?$"), _json_route(body))


@pytest.mark.parametrize("name", _TABS)
def test_tab_shots(browser: Browser, base_url: str, phone_projection: bool, name: str) -> None:
    # The suite runs every test once per projection, but this one captures phone
    # and desktop itself; the phone leg would only rewrite the same files.
    if phone_projection:
        pytest.skip("captures both viewports itself; the desktop leg writes them all")
    tab, ready = _TABS[name]
    for variant in variants():
        with variant_page(browser, variant) as page:
            page.add_init_script(f"localStorage.setItem('app-launcher.theme', '{variant.theme}');")
            _mock_data(page)
            page.goto(f"{base_url}/", wait_until="domcontentloaded")
            page.locator(tab).click()
            # A list inside a collapsed card would shoot as an empty card.
            page.locator(ready.split()[0]).evaluate("el => { const d = el.closest('details'); if (d) d.open = true; }")
            expect(page.locator(ready).first).to_be_visible()
            shot(page, SHOTS_DIR / f"{name}-{variant.slug}.png")
