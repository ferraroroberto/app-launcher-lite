"""Smoke + regression coverage for the Apps-tab Running apps panel (issue #35).

The panel lists apps spawned from the launcher, binds each to its port,
and offers tap-to-open over Tailscale plus a per-app Stop. A real
Streamlit launch is too heavy + flaky for the smoke suite, so the
running-apps API is route-mocked here; real-spawn integration is covered
by the issue's manual validation. Runs against the live tray (or the
autoboot disposable webapp); skips if neither is up.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import test_listeners_collapse as listeners_collapse
from tests.e2e import test_registered_trays_panel as registered_trays_panel
from tests.e2e._flow import FLOW_TIMEOUT_S

pytestmark = pytest.mark.smoke


def _navigate(page: Page, base_url: str) -> list[str]:
    """Open the SPA, return the list that collects uncaught JS errors."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#runningAppsList", state="attached", timeout=5_000)
    return errors


def _two_row_payload() -> dict:
    return {
        "running": [
            {
                "app_id": "voice-transcriber-webapp",
                "name": "Voice Transcriber",
                "kind": "webapp",
                "pid": 12345,
                "started_at": 1736179200,
                "port": 8501,
                "url": "https://pc.example-tailnet.ts.net:8501/",
                "alive": True,
            },
            {
                "app_id": "photo-ocr-streamlit",
                "name": "Photo OCR",
                "kind": "streamlit",
                "pid": 23456,
                "started_at": 1736179200,
                "port": None,
                "url": None,
                "alive": True,
            },
        ]
    }


def scenario_section_renders_empty(authed_page: Page, base_url: str) -> None:
    authed_page.route(
        "**/api/apps/running",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"running": []}),
        ),
    )
    errors = _navigate(authed_page, base_url)
    authed_page.locator("#tabApps").click()

    expect(authed_page.locator("#runningAppsList")).to_be_attached()
    expect(authed_page.locator("#runningAppsEmpty")).to_be_visible()
    authed_page.wait_for_timeout(300)
    assert errors == [], "JS errors:\n  - " + "\n  - ".join(errors)


def scenario_section_renders_rows(authed_page: Page, base_url: str) -> None:
    authed_page.route(
        "**/api/apps/running",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_two_row_payload()),
        ),
    )
    _navigate(authed_page, base_url)
    authed_page.locator("#tabApps").click()

    rows = authed_page.locator("#runningAppsList li.app-item")
    expect(rows).to_have_count(2, timeout=5_000)

    # Row 0 has a URL — Open enabled; row 1 has none — Open disabled.
    row0, row1 = rows.nth(0), rows.nth(1)
    expect(row0.locator(".name")).to_have_text("Voice Transcriber")
    expect(row0.locator(".kind-pill")).to_have_text("webapp")
    expect(row0.locator(".meta")).to_contain_text(":8501")
    expect(row0.locator(".action-open")).to_be_enabled()

    expect(row1.locator(".name")).to_have_text("Photo OCR")
    expect(row1.locator(".meta")).to_contain_text("binding")
    expect(row1.locator(".action-open")).to_be_disabled()


def scenario_open_button_opens_new_tab(authed_page: Page, base_url: str) -> None:
    target = "https://example.invalid/"
    payload = _two_row_payload()
    payload["running"] = [payload["running"][0]]
    payload["running"][0]["url"] = target

    authed_page.route(
        "**/api/apps/running",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        ),
    )
    # Capture window.open before the SPA loads — don't actually navigate.
    authed_page.add_init_script(
        "window.__opened = [];"
        "window.open = function (u) { window.__opened.push(u); return null; };"
    )
    _navigate(authed_page, base_url)
    authed_page.locator("#tabApps").click()

    open_btn = authed_page.locator("#runningAppsList li.app-item .action-open")
    expect(open_btn).to_be_enabled(timeout=5_000)
    open_btn.click()

    opened = authed_page.evaluate("window.__opened")
    assert opened == [target], f"window.open called with {opened!r}, expected [{target!r}]"


def scenario_stop_confirms_and_posts(authed_page: Page, base_url: str) -> None:
    stop_calls: list[str] = []

    payload = _two_row_payload()
    payload["running"] = [payload["running"][0]]  # one row

    def _running_handler(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload if not stop_calls else {"running": []}),
        )

    def _stop_handler(route):
        stop_calls.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"stopped": 12345}),
        )

    authed_page.route("**/api/apps/running", _running_handler)
    authed_page.route(
        "**/api/apps/*/instances/*/stop", _stop_handler
    )
    authed_page.on("dialog", lambda d: d.accept())

    _navigate(authed_page, base_url)
    authed_page.locator("#tabApps").click()

    rows = authed_page.locator("#runningAppsList li.app-item")
    expect(rows).to_have_count(1, timeout=5_000)
    rows.first.locator(".action-stop-close").click()

    expect(rows).to_have_count(0, timeout=5_000)
    assert len(stop_calls) == 1, f"expected one stop POST, got {stop_calls!r}"
    assert stop_calls[0].endswith(
        "/api/apps/voice-transcriber-webapp/instances/12345/stop"
    ), f"stop POST hit the wrong path: {stop_calls[0]}"


def scenario_polling_pauses_on_other_tab(authed_page: Page, base_url: str) -> None:
    calls: list[float] = []

    def _handler(route):
        calls.append(0.0)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"running": []}),
        )

    authed_page.route("**/api/apps/running", _handler)
    _navigate(authed_page, base_url)

    # Boot lands on the Coding tab — fetchRunningApps self-gates, so
    # no /api/apps/running request should fire there. Wait past one poll.
    authed_page.wait_for_timeout(5_000)
    assert calls == [], f"polled while Apps tab hidden: {len(calls)} call(s)"

    # Switch to Apps tab → polling resumes (tab-click triggers one fetch).
    authed_page.locator("#tabApps").click()
    authed_page.wait_for_timeout(1_000)
    after_open = len(calls)
    assert after_open >= 1, "no /api/apps/running request after opening Apps tab"

    # Switch away again → polling pauses; count must stop climbing.
    authed_page.locator("#tabCoding").click()
    paused_at = len(calls)
    authed_page.wait_for_timeout(5_000)
    assert len(calls) == paused_at, (
        f"polling kept firing after leaving the Apps tab: "
        f"{len(calls) - paused_at} extra call(s)"
    )


@pytest.mark.timeout(FLOW_TIMEOUT_S)
def test_apps_flow(run_flow) -> None:
    """The Apps tab: running apps, port listeners and registered trays.

    Each scenario below ran as its own test before; ``run_flow`` gives each a fresh
    browser context and reports every scenario that fails, not just the first.
    """
    run_flow(
        scenario_section_renders_empty,
        scenario_section_renders_rows,
        scenario_open_button_opens_new_tab,
        scenario_stop_confirms_and_posts,
        scenario_polling_pauses_on_other_tab,
        listeners_collapse.scenario_parent_collapsed_by_default_and_toggles,
        listeners_collapse.scenario_childless_listener_keeps_flat_row,
        listeners_collapse.scenario_kill_works_collapsed_parent_and_expanded_child,
        registered_trays_panel.scenario_only_tray_kind_rows_appear_in_registered_trays_panel,
        registered_trays_panel.scenario_empty_state_shown_with_no_tray_rows,
        registered_trays_panel.scenario_autostart_toggle_reflects_state_and_patches,
    )
