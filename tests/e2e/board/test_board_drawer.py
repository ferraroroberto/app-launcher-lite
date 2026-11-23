"""Board tab e2e: the card drawer, its composer and menus."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.board._support import (
    _board_payload,
    _mock_board,
    _mock_exchange,
    _open_board,
)
from tests.e2e.fixtures.ui import open_row_menu
from tests.e2e.fixtures.waits import wait_until

pytestmark = pytest.mark.smoke


def test_board_copilot_card_drawer_shows_agent_native_exchange(
    authed_page: Page, base_url: str
) -> None:
    """#457: a Copilot card opens its own structured exchange, rather than
    degrading to an empty message. The drawer's code is held on the way in,
    so the card tap beats it: the card opens on a drawer reading its
    exchange, never a tap that does nothing, and the drawer replaces it once
    the code lands."""
    payload = _board_payload()
    payload["columns"]["claude_turn"].append({
        "session_id": "s-copilot", "kind": "pty", "agent": "copilot",
        "project_dir": "E:/automation/app-launcher", "name": "app-launcher",
        "alive": True, "started_at": "2026-07-02T11:57:00Z",
        "live_title": "app-launcher | fix/457", "prompt_title": "fix the drawer",
        "project": "app-launcher", "status": "unknown", "age_seconds": None,
    })
    _mock_board(authed_page, payload)
    _mock_exchange(authed_page, sid="s-copilot", payload={
        "available": True, "source": "copilot", "reason": None,
        "user": {"text": "fix the drawer", "timestamp": None},
        "assistant": {"text": "Copilot exchange resolved.", "timestamp": None},
    })
    held: list = []
    code = re.compile(r".*/static/board/drawer\.js(\?.*)?$")
    authed_page.route(code, lambda route: held.append(route))
    _open_board(authed_page, base_url)
    card = authed_page.locator(
        '.board-list[data-col="claude_turn"] li.board-item',
        has_text="app-launcher",
    ).locator("button.board-card")
    card.click()
    expect(card).to_have_attribute("aria-expanded", "true")
    expect(authed_page.locator(".board-drawer-loading")).to_have_text("Reading last exchange…")
    wait_until(authed_page, lambda: bool(held), "the drawer code request")
    for route in held:
        route.continue_()
    authed_page.unroute(code)
    expect(authed_page.locator(".board-drawer-loading")).to_have_count(0)
    exchange = authed_page.locator(".board-exchange")
    expect(exchange).to_have_attribute("data-state", "ready")
    expect(exchange).to_contain_text("fix the drawer")
    expect(exchange).to_contain_text("Copilot exchange resolved.")


@pytest.mark.parametrize("reason, expected_state, expected_text", [
    ("no_exchange", "empty", "No exchange yet."),
    ("capture_unparseable", "error", "Conversation preview unavailable"),
])
def test_board_drawer_distinguishes_empty_from_source_failure(
    authed_page: Page, base_url: str, reason: str, expected_state: str,
    expected_text: str,
) -> None:
    """#457: a genuinely new conversation is not described as an unlinked
    transcript, and a source failure is a distinct sanitized error state."""
    _mock_board(authed_page)
    _mock_exchange(authed_page, payload={
        "available": False, "source": None, "reason": reason,
        "user": None, "assistant": None,
    })
    _open_board(authed_page, base_url)
    authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card").click()
    exchange = authed_page.locator(".board-exchange")
    expect(exchange).to_have_attribute("data-state", expected_state)
    expect(exchange).to_contain_text(expected_text)


@pytest.mark.parametrize("state_sid", [
    pytest.param(None, id="session-id"),
    pytest.param("t-uuid-wait", id="state-sid"),
])
def test_board_deep_link_opens_drawer(
    authed_page: Page, base_url: str, state_sid: str | None
) -> None:
    """#301: ?board=<sid> lands on the Board with that card's drawer open —
    the target of the Slack-ping deep link. #307: a Slack ping's ?board=<sid>
    carries the hook's transcript UUID, not the card's session_id — resolve it
    via the card's state_sid instead, and expand the drawer keyed by the card's
    real session_id."""
    payload = _board_payload()
    if state_sid:
        payload["columns"]["your_turn"][0]["state_sid"] = state_sid
    _mock_board(authed_page, payload)
    _mock_exchange(authed_page)  # keyed by the real session_id, s-wait

    link_sid = state_sid or "s-wait"
    authed_page.goto(f"{base_url}/?board={link_sid}", wait_until="domcontentloaded")
    expect(authed_page.locator("#paneBoard")).to_be_visible(timeout=10_000)
    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible(timeout=10_000)
    expect(drawer).to_contain_text("Merge fixed — tests green. Ship it?")


def _detached_board_payload() -> dict:
    payload = _board_payload()
    payload["columns"]["your_turn"] = [{
        "session_id": "s-remote", "kind": "remote", "agent": "copilot",
        "project_dir": "E:/automation/whatsapp-radar", "name": "whatsapp-radar",
        "alive": True, "started_at": "2026-07-02T11:30:00Z",
        "live_title": "detached console", "prompt_title": "",
        "project": "whatsapp-radar", "status": "awaiting-input", "age_seconds": 660,
    }]
    return payload


def test_board_drawer_detached_session_sends_through_the_shared_composer(
    authed_page: Page, base_url: str
) -> None:
    """#984: a detached (remote) card gets the same composer as a full-control
    one — before, the drawer offered no reply at all. ➤ Send posts to the
    kind-agnostic /input route and the toast keeps the detached "Sent, not
    confirmed" wording (sessions.js::sendOutcome). ⌨ keys are disabled with
    the way to get them; Terminal stays in the row, aria-disabled, and a tap
    toasts why; Stop and Chat stay available."""
    _mock_board(authed_page, _detached_board_payload())
    _mock_exchange(authed_page, sid="s-remote")

    captured: dict = {}

    def _capture_input(route):
        captured["body"] = route.request.post_data_json
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "delivered": "unconfirmed"}),
        )

    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/s-remote/input$"), _capture_input
    )

    _open_board(authed_page, base_url)
    authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card").click()
    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()

    composer = drawer.locator(".board-drawer-composer")
    keys = composer.locator(".composer-keys")
    expect(keys).to_be_disabled()
    expect(keys).to_have_attribute("title", "Open the terminal for keys")

    terminal = drawer.locator(".board-open-terminal")
    expect(terminal).to_have_attribute("aria-disabled", "true")
    drawer.locator(".board-drawer-kebab").click()
    expect(open_row_menu(authed_page).locator("[data-item=board-stop-btn]")).not_to_have_attribute(
        "aria-disabled", "true")
    authed_page.keyboard.press("Escape")
    expect(drawer.locator(".board-open-chat")).not_to_have_attribute("aria-disabled", "true")
    # aria-disabled keeps the tap reachable (it toasts the reason), but
    # Playwright treats it as not enabled, hence force (#982's gotcha).
    terminal.click(force=True)
    expect(authed_page.locator("#toast")).to_contain_text("Detached session — no terminal")
    expect(authed_page.locator("#terminalOverlay")).to_be_hidden()

    composer.locator(".composer-input").fill("carry on")
    composer.locator(".composer-send").click()
    expect(authed_page.locator("#toast")).to_contain_text(
        "Sent, not confirmed: typed into the PC console"
    )
    assert captured.get("body") == {"data": "carry on", "submit": True}
    expect(drawer).to_be_hidden()


def test_board_drawer_chat_opens_chat_mode_for_the_same_session(
    authed_page: Page, base_url: str
) -> None:
    """#984: the drawer's Chat button opens the session overlay in Chat mode
    (#982) for the card's own session, closing the drawer."""
    _mock_board(authed_page)
    _mock_exchange(authed_page)
    transcript_calls: list = []

    def _transcript(route):
        transcript_calls.append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "available": True, "source": "native", "reason": None,
                "session_id": "s-wait", "next_cursor": None,
                "entries": [
                    {"kind": "assistant", "timestamp": "2026-07-02T11:59:00Z",
                     "text": "Merge fixed", "truncated": False, "sidechain": False},
                ],
            }),
        )

    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/s-wait/transcript(\?.*)?$"), _transcript
    )

    _open_board(authed_page, base_url)
    authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card").click()
    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    drawer.locator(".board-open-chat").click()

    overlay = authed_page.locator("#terminalOverlay")
    expect(overlay).to_be_visible()
    expect(overlay).to_have_attribute("data-mode", "chat")
    expect(authed_page.locator("#transcriptList")).to_contain_text("Merge fixed")
    assert transcript_calls, "Chat mode never read the session's transcript"
    assert all("/sessions/s-wait/" in u for u in transcript_calls)


def test_board_drawer_survives_a_board_poll_mid_interaction(
    authed_page: Page, base_url: str
) -> None:
    """#512 / #958: a re-render must not rebuild an open worker drawer out
    from under an in-progress interaction — renderBoard() keeps the open
    drawer's node. The git-status refresh stopped re-rendering the Board in
    #1436 (the Backlog draws no git state), so the re-render here is the
    Board's own poll. Reproduced with the #510 diagnostic technique: tag the
    rename button's live DOM node once the drawer is open, wait for a whole
    poll to land and render, then confirm the tagged node (and the drawer)
    survive untouched — on the poll's own response, not a fixed sleep. On the
    action-row (#1502) the drawer's kebab menu is part of the kept node: one
    left open stays open across the poll."""
    _mock_board(authed_page)
    _mock_exchange(authed_page)

    _fake_clock.install(authed_page)
    _open_board(authed_page, base_url)
    authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card").click()

    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    terminal = drawer.locator(".board-open-terminal")
    expect(terminal).to_be_visible()
    # The shared composer (#984) lives inside the kept node: a draft typed
    # into it must survive the re-render too.
    draft = drawer.locator(".board-drawer-composer .composer-input")
    draft.fill("half-typed reply")

    # Tag the live DOM node — if renderBoard() rebuilds the drawer, the tag
    # is lost even though the drawer stays open (boardExpanded is preserved
    # either way).
    terminal.evaluate("el => { el.dataset.e2eTag = 'pre-poll'; }")
    drawer.locator(".board-drawer-kebab").click()
    menu = open_row_menu(authed_page)
    expect(menu.locator("[data-item=board-rename-btn]")).to_be_visible()

    # One poll: the Board and the chief's plan are fetched together, and the
    # render follows both answers; two frames later it has run.
    with authed_page.expect_response(
        lambda resp: resp.url.endswith("/api/board/chief-plan")
    ):
        with authed_page.expect_response(lambda resp: resp.url.endswith("/api/board")):
            _fake_clock.jump_interval(authed_page, 5_000, "fetchBoard")
    authed_page.evaluate(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")

    expect(drawer).to_be_visible()
    expect(terminal).to_have_attribute("data-e2e-tag", "pre-poll")
    expect(draft).to_have_value("half-typed reply")
    expect(menu.locator("[data-item=board-rename-btn]")).to_be_visible()
    # The kept drawer gets a fresh header (#958); it still declares the
    # disclosure open (#1259).
    header = authed_page.locator("li.board-item.expanded > button.board-card")
    expect(header).to_have_attribute("aria-expanded", "true")
    expect(header).to_have_attribute("aria-controls", "board-drawer-s-wait")


# The shortest drawer a live session can produce: the reader found one side of
# the exchange only (a session prompted but not yet answered, or one whose user
# turn it could not recover). The `.board-exchange` block is then a single line
# instead of two, which is what lifts the composer — and the image menu above
# it — highest against the top of the column carousel.
_ONE_SIDED_EXCHANGE = {
    "available": True,
    "source": "native",
    "reason": None,
    "user": {"text": "fix the merge", "timestamp": "2026-07-02T11:50:00Z"},
    "assistant": None,
}
