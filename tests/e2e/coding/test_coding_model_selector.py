"""Coding-tab launch model selector (lite fork: Copilot-only).

The old Projects-summary combo + Claude segmented-control pair (#540) is
gone — the single source of truth is the Copilot options card's
``#copilotModel`` <select>, fed by the config-driven ``copilot_models``
list plus a leading "Default (auto)" option ('' — launch without
``--model``, Copilot picks).

Hermetic: /api/config is route-mocked with a tiny stateful handler that
stores ``copilot_model`` on POST and echoes it on GET, exactly as the real
``patchConfig`` round-trip does — so the persistence contract is exercised
without mutating the live disposable webapp's config.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_MODELS = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]


def _config(model: str) -> dict:
    """A minimal /api/config payload — enough for fetchConfig +
    renderCopilotOptions."""
    return {
        "projects_dir": "E:/automation",
        "projects_ignore": [],
        "apps_scan_root": "",
        "team_os_dir": "",
        "copilot": {
            "skip_permissions": False,
            "model": model,
            "models_available": list(_MODELS),
            "autopilot": True,
            "context": "long_context",
            "contexts_available": ["", "default", "long_context"],
            "effort": "xhigh",
            "efforts_available": ["", "low", "high", "xhigh"],
            "computed_flags": "",
        },
    }


def _mock_config(page: Page) -> dict:
    """Route /api/config with a stateful GET/POST pair mimicking patchConfig.
    Returns the mutable state dict so a test can read the last-persisted model."""
    state = {"model": "gpt-5.6-luna"}

    def _route(route):
        req = route.request
        if req.method == "POST":
            body = _json.loads(req.post_data or "{}")
            if "copilot_model" in body:
                state["model"] = body["copilot_model"]
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps(_config(state["model"])),
            )

    page.route(re.compile(r".*/api/config$"), _route)
    return state


def test_copilot_model_select_lists_config_models_and_persists(
    authed_page: Page, base_url: str
) -> None:
    """The options-card model <select> offers Default (auto) plus exactly the
    config-driven ``copilot_models`` list, and a pick persists
    ``copilot_model`` through the patchConfig round-trip."""
    state = _mock_config(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # Coding (#tabCoding) is the default active tab; the options card is
    # collapsed by default.
    authed_page.locator("#codingOptions").evaluate("el => { el.open = true; }")
    select = authed_page.locator("#copilotModel")
    expect(select).to_be_attached(timeout=5_000)

    # Options = Default (value '') + the config-driven list, in order.
    values = select.locator("option").evaluate_all(
        "opts => opts.map(o => o.value)"
    )
    assert values == [""] + _MODELS, (
        f"model select options {values!r} do not match Default + the "
        f"config-driven copilot_models list {_MODELS!r}"
    )
    expect(select).to_have_value("gpt-5.6-luna")

    # Picking another model persists copilot_model and survives the
    # round-trip re-render.
    select.select_option("gpt-5.6-sol")
    expect(select).to_have_value("gpt-5.6-sol", timeout=5_000)
    assert state["model"] == "gpt-5.6-sol"

    # Default (auto) is a real, persistable choice — '' means "no --model".
    select.select_option("")
    expect(select).to_have_value("", timeout=5_000)
    assert state["model"] == ""
