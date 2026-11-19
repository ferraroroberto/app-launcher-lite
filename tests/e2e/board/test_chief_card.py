"""The chief's Board card: its wording, the drawer reply, Stop, the settings sheet and a drawer that survives polling."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board._chief_support import (
    _board_payload,
    _mock_board,
)
from tests.e2e.fixtures.ui import open_settings_sheet
from tests.e2e.fixtures.waits import flush_requests, wait_until

pytestmark = pytest.mark.smoke


def _mock_chief_settings(authed_page: Page) -> tuple[dict, list[dict]]:
    """Patch GET /api/config's chief fields and the chief settings endpoint with a
    stored block; returns ``(stored, puts)`` where ``puts`` records each PUT body."""
    stored = {"model": "demo-model-a", "worker_cap": 3, "worker_cap_scope": "chief",
              "context": "", "effort": ""}
    puts: list[dict] = []

    def _config(route):
        if route.request.method != "GET":
            route.continue_()
            return
        resp = route.fetch()
        body = resp.json()
        body.setdefault("copilot", {})["models_available"] = ["demo-model-a", "demo-model-b"]
        body.update(
            chief_model=stored["model"], chief_worker_cap=stored["worker_cap"],
            chief_context=stored["context"], chief_effort=stored["effort"],
            chief_model_problem="",
            chief_worker_cap_scope=stored["worker_cap_scope"],
            chief_worker_cap_min=1, chief_worker_cap_max=10,
        )
        route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body),
        )

    def _settings(route):
        if route.request.method == "PUT":
            patch = route.request.post_data_json
            puts.append(patch)
            stored.update(patch)
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps({"settings": dict(stored)}),
            )
        else:
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps({"settings": dict(stored)}),
            )

    authed_page.route(re.compile(r".*/api/config$"), _config)
    authed_page.route(re.compile(r".*/api/board/chief/settings$"), _settings)
    return stored, puts


def test_chief_settings_sheet_roundtrip(
    authed_page: Page, base_url: str
) -> None:
    """Gear -> Settings > Chief sheet filled from GET /api/config; picking a
    model PUTs ``{"model"}``; committing the worker cap (blur) PUTs
    ``{"worker_cap"}``; an out-of-range cap sends nothing and says why;
    the context and effort selects each PUT only their own field.

    The Board's chief settings dialog became Settings > Chief in #1435: no
    Save button, each field saves on its own (#616 retired the daily-respawn
    fields — model and worker cap are all that is left to round-trip). The
    sheet reads the chief fields off GET /api/config now, so that read is
    patched here (every other /api/config call passes through to the
    disposable server) to keep the starting values deterministic."""
    _mock_board(authed_page, _board_payload(with_chief=True))

    stored, puts = _mock_chief_settings(authed_page)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # The Board has no chief card (#1436): Settings › Chief is reached from
    # Settings itself, or the Code tab's chief row (test_coding_chief.py).
    open_settings_sheet(authed_page, "chiefSheet")

    expect(authed_page.locator("#paneSettings")).to_be_visible()
    expect(authed_page.locator("#chiefSheet")).to_be_visible()
    expect(authed_page.locator("#chiefSettingsDialog")).to_have_count(0)
    expect(authed_page.locator("#chiefModelSelect")).to_have_attribute(
        "data-value", "demo-model-a"
    )
    expect(authed_page.locator("#chiefWorkerCap")).to_have_value("3")

    # Model: one PUT carrying only the model.
    authed_page.locator("#chiefModelSelect .model-combo-trigger").click()
    authed_page.locator("#chiefModelMenu [data-value='demo-model-b']").click()
    wait_until(authed_page, lambda: puts == [{"model": "demo-model-b"}],
               "the chief model PUT")
    expect(authed_page.locator("#toast")).to_contain_text("Chief model:")
    expect(authed_page.locator("#chiefModelSelect")).to_have_attribute(
        "data-value", "demo-model-b"
    )

    # Worker cap: saves when the field is committed, carrying only the cap.
    cap = authed_page.locator("#chiefWorkerCap")
    cap.fill("5")
    cap.blur()
    wait_until(authed_page, lambda: puts == [{"model": "demo-model-b"}, {"worker_cap": 5}],
               "the worker cap PUT")
    expect(authed_page.locator("#toast")).to_contain_text("Worker cap saved.")

    # Out of range: nothing is sent, the toast and the field say why.
    cap.fill("11")
    cap.blur()
    expect(authed_page.locator("#toast")).to_contain_text(
        "Worker cap must be a whole number between 1 and 10."
    )
    expect(cap).to_have_attribute("aria-invalid", "true")
    flush_requests(authed_page)
    assert puts == [{"model": "demo-model-b"}, {"worker_cap": 5}], puts

    # Context and effort sit beside the model on "Same as Coding agent" until
    # set; each pick PUTs only its own field.
    context, effort = authed_page.locator("#chiefContext"), authed_page.locator("#chiefEffort")
    expect(context).to_have_value("")
    expect(context.locator("option").first).to_have_text("Same as Coding agent")
    expect(effort).to_have_value("")
    expect(effort).to_be_enabled()  # the mocked chief model is an explicit one
    context.select_option("default")
    wait_until(authed_page, lambda: puts[-1] == {"context": "default"}, "the chief context PUT")
    expect(context).to_have_value("default")
    effort.select_option("high")
    wait_until(authed_page, lambda: puts[-1] == {"effort": "high"}, "the chief effort PUT")
    expect(effort).to_have_value("high")
    assert len(puts) == 4, puts


def test_chief_worker_cap_scope_switch(authed_page: Page, base_url: str) -> None:
    """Settings > Chief > "Count only chief-dispatched sessions": on by default
    (scope ``chief``); each flip PUTs only ``worker_cap_scope`` and the switch
    settles on what is stored."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    _, puts = _mock_chief_settings(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    open_settings_sheet(authed_page, "chiefSheet")

    scope = authed_page.locator("#chiefWorkerCapScopeToggle")
    expect(scope).to_have_attribute("aria-checked", "true")

    # Off counts every session ('all'); back on is 'chief'.
    scope.click()
    wait_until(authed_page, lambda: puts == [{"worker_cap_scope": "all"}],
               "the worker cap scope PUT (all)")
    expect(scope).to_have_attribute("aria-checked", "false")
    expect(authed_page.locator("#toast")).to_contain_text("Worker cap counts every session.")
    scope.click()
    wait_until(authed_page, lambda: puts == [{"worker_cap_scope": "all"}, {"worker_cap_scope": "chief"}],
               "the worker cap scope PUT (chief)")
    expect(scope).to_have_attribute("aria-checked", "true")

