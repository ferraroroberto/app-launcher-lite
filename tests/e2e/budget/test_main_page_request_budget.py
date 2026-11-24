"""The main page keeps the fleet's request budget on a real browser clock.

A fixed-rate poll that keeps reading on a hidden tab, or a burst on load, is
invisible to the source ratchet; this loads the page on a fake clock and counts
what it really sends: a visible minute within `.fleet.toml`'s
`[request_budget] visible_requests_per_minute`, a hidden minute of zero, and at
most one catch-up read per path on the return. Mechanism: project-scaffolding
`docs/polling-budget.md`, "The runtime check".
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e.budget._request_budget import (
    assert_request_budget,
    measure_request_budget,
    visible_budget,
)
from tests.e2e.fixtures.env import REPO_ROOT

pytestmark = pytest.mark.smoke

# The one request allowed in the hidden minute, with its reason: the usage log
# sends its queued taps once at the moment the page hides (a beacon, so they
# survive a closing tab). It is a single flush, not a poll: a second one in the
# minute still fails.
HIDE_FLUSH_PATH = "/api/usage/events"


def test_main_page_keeps_its_request_budget(page: Page, base_url: str) -> None:
    reading = measure_request_budget(page, f"{base_url}/")
    assert reading.hidden[HIDE_FLUSH_PATH] <= 1, f"usage log flushed more than once while hidden ({reading.summary()})"
    del reading.hidden[HIDE_FLUSH_PATH]
    assert_request_budget(reading, visible_budget(REPO_ROOT))
