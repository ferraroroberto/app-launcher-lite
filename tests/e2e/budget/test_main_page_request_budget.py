"""The main page keeps the fleet's request budget on a real browser clock.

A fixed-rate poll that keeps reading on a hidden tab, or a burst on load, is
invisible to the source ratchet; this loads the page on a fake clock and counts
what it really sends: a visible minute within `.fleet.toml`'s
`[request_budget] visible_requests_per_minute`, a hidden minute of zero, and at
most one catch-up read per path on the return. Mechanism: project-scaffolding
`docs/polling-budget.md`, "The runtime check".
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page

from tests.e2e.budget._request_budget import (
    assert_request_budget,
    measure_request_budget,
    visible_budget,
)
from tests.e2e.fixtures.env import REPO_ROOT

# `iphone`: the conftest keeps a WebKit node only for a test carrying it, and timer
# and poll behaviour is engine-dependent: the WebKit rerun of this budget
# (scripts/tools/webkit_budget_check.py) selects nothing without it.
pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

# The one request allowed in the hidden minute, with its reason: the usage log
# sends its queued taps once at the moment the page hides (a beacon, so they
# survive a closing tab). It is a single flush, not a poll: a second one in the
# minute still fails.
HIDE_FLUSH_PATH = "/api/usage/events"


# The CPU reading is answered "still warming" for the whole minute, the worst case of a box
# too loaded to take the sampler's first sample. The warming retry is bounded, so the
# header sends its 6 polls plus at most 2 retries, never one read every 3 s. The server
# side is mocked: the real sampler's warm-up time depends on the host, and left real it
# made the count vary by engine and load.
SYSTEM_LOAD_PATH = "/api/system/load"
SYSTEM_LOAD_MAX_PER_MINUTE = 6 + 2
_STILL_WARMING = (
    '{"level": "unknown", "reason": "warming", "cpu_percent": null, "memory": null, '
    '"top": [], "other_cores": null, "cores": null, "age_seconds": null}'
)


def test_main_page_keeps_its_request_budget(page: Page, base_url: str) -> None:
    page.route(
        re.compile(r".*/api/system/load$"),
        lambda route: route.fulfill(status=200, content_type="application/json", body=_STILL_WARMING),
    )
    reading = measure_request_budget(page, f"{base_url}/")
    assert reading.hidden[HIDE_FLUSH_PATH] <= 1, f"usage log flushed more than once while hidden ({reading.summary()})"
    del reading.hidden[HIDE_FLUSH_PATH]
    assert reading.visible[SYSTEM_LOAD_PATH] <= SYSTEM_LOAD_MAX_PER_MINUTE, (
        f"the warming retry is not bounded ({reading.summary()})"
    )
    assert_request_budget(reading, visible_budget(REPO_ROOT))
