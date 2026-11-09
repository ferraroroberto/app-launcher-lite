"""Manual session rename (issue #458) — Coding tab row + Board drawer.

A launcher-native rename that wins over every auto-derived title source
(see ``sessions.js::sessionTitle`` and ``test_session_title_naming.py``'s
pure-function pin). These tests drive the actual UI: click the pencil icon,
fill the shared ``#sessionRenameDialog``, submit, and confirm both the POST
body and the on-screen title update — the Coding tab via a re-fetch, the
Board drawer optimistically (its drawer stays open across a rename, so it
patches the card in place rather than re-fetching — see board.js).
"""

from __future__ import annotations

import functools
import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import test_desktop_session_mirror as desktop_session_mirror
from tests.e2e import test_edge_mirror_close as edge_mirror_close
from tests.e2e import test_inpage_terminal_not_mirror as inpage_terminal_not_mirror
from tests.e2e import test_screen_view as screen_view
from tests.e2e import test_session_title_naming as session_title_naming
from tests.e2e import test_shared_session_title as shared_session_title
from tests.e2e import test_smoke as smoke
from tests.e2e import test_stop_unify_and_terminal_kill as stop_unify_and_terminal_kill
from tests.e2e._flow import FLOW_TIMEOUT_S

pytestmark = pytest.mark.smoke

_CODING_SID = "s-rename-coding"
_BOARD_SID = "s-rename-board"


def _mock_sessions_list(page: Page, state: dict) -> None:
    def _handler(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": [{
                "session_id": _CODING_SID,
                "kind": "pty",
                "agent": "copilot",
                "project_dir": "E:/automation/renameproj",
                "name": "renameproj",
                "alive": True,
                "started_at": "2026-07-08T11:30:00Z",
                "live_title": "",
                "prompt_title": "",
                "manual_title": state["manual_title"],
            }]}),
        )

    page.route(re.compile(r".*/api/coding/sessions$"), _handler)


def _mock_rename(page: Page, sid: str, state: dict, captured: dict) -> None:
    def _handler(route):
        captured["method"] = route.request.method
        captured["body"] = route.request.post_data_json
        state["manual_title"] = captured["body"].get("title", "")
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "session_id": sid, "manual_title": state["manual_title"],
            }),
        )

    page.route(re.compile(r".*/api/coding/sessions/" + sid + r"/rename$"), _handler)


def _mock_board(page: Page) -> None:
    page.route(
        re.compile(r".*/api/board$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "generated_at": "2026-07-08T12:00:00Z",
                "columns": {
                    "backlog": [], "your_turn": [], "done": [],
                    "bot_turn": [{
                        "session_id": _BOARD_SID,
                        "kind": "pty",
                        "agent": "copilot",
                        "project_dir": "E:/automation/boardrenameproj",
                        "name": "boardrenameproj",
                        "alive": True,
                        "started_at": "2026-07-08T11:30:00Z",
                        "live_title": "",
                        "prompt_title": "",
                        "manual_title": "",
                        "project": "boardrenameproj",
                        "status": "working",
                        "age_seconds": 60,
                    }],
                },
                "gitlab": {"fetched_at": "2026-07-08T11:00:00Z", "error": None},
                "sessions_state": {
                    "available": True, "stale": False,
                    "updated_at": "2026-07-08T11:58:00Z",
                },
            }),
        ),
    )
    page.route(
        re.compile(r".*/api/board/sessions/" + _BOARD_SID + r"/exchange$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"available": False, "reason": "no_exchange"}),
        ),
    )
    # #510/#680: the real /api/coding/git-status boot fetch is
    # git-subprocess-backed and non-deterministic in timing; its completion
    # calls renderBoard() whenever the Board tab is active (apps.js), which
    # rebuilds the DOM — including an open drawer's rename button — out from
    # under an in-progress interaction. Stubbed in the shared helper so every
    # test here gets it, not just the ones that remembered to ask.
    page.route(
        re.compile(r".*/api/coding/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": []}),
        ),
    )


def scenario_coding_tab_rename_wins_over_launch_name(
    authed_page: Page, base_url: str
) -> None:
    state = {"manual_title": ""}
    captured: dict = {}
    _mock_sessions_list(authed_page, state)
    _mock_rename(authed_page, _CODING_SID, state, captured)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = authed_page.locator(f'#sessionsList li[data-session-id="{_CODING_SID}"]')
    expect(row.locator(".name")).to_have_text("renameproj", timeout=10_000)

    row.locator('button[aria-label="Rename session"]').click()
    dialog = authed_page.locator("#sessionRenameDialog")
    expect(dialog).to_be_visible()
    expect(authed_page.locator("#sessionRenameInput")).to_have_value("renameproj")

    authed_page.locator("#sessionRenameInput").fill("My custom title")
    authed_page.locator("#sessionRenameForm button[type='submit']").click()

    authed_page.wait_for_function(
        "() => !document.getElementById('sessionRenameDialog').open",
        timeout=3_000,
    )
    assert captured.get("method") == "POST"
    assert captured.get("body") == {"title": "My custom title"}
    expect(row.locator(".name")).to_have_text("My custom title", timeout=10_000)


def scenario_board_drawer_rename_patches_card_in_place(
    authed_page: Page, base_url: str
) -> None:
    captured: dict = {}
    _mock_board(authed_page)
    _mock_rename(authed_page, _BOARD_SID, {"manual_title": ""}, captured)
    # git-status is stubbed by _mock_board above (#510/#680).

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabBoard").click()
    expect(authed_page.locator("#paneBoard")).to_be_visible()

    card = authed_page.locator(
        '.board-list[data-col="bot_turn"] li.board-item'
    ).first.locator("button.board-card")
    expect(card.locator(".board-card-title")).to_have_text(
        "boardrenameproj", timeout=10_000
    )
    card.click()

    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    drawer.locator("button.board-rename-btn").click()

    dialog = authed_page.locator("#sessionRenameDialog")
    expect(dialog).to_be_visible()
    authed_page.locator("#sessionRenameInput").fill("Board custom title")
    authed_page.locator("#sessionRenameForm button[type='submit']").click()

    authed_page.wait_for_function(
        "() => !document.getElementById('sessionRenameDialog').open",
        timeout=3_000,
    )
    assert captured.get("body") == {"title": "Board custom title"}
    # Optimistic in-place patch — no second /api/board fetch is needed (the
    # drawer stays open, which would make fetchBoard() a no-op anyway).
    expect(card.locator(".board-card-title")).to_have_text(
        "Board custom title", timeout=5_000
    )


def scenario_rename_dialog_adopts_modal_contract(
    authed_page: Page, base_url: str
) -> None:
    # #545 supersedes #484: the rename dialogs adopt the fleet modal contract
    # (design.md components.modal), replacing the two-button Cancel+Save footer
    # that #484 hand-tuned into an equal pair. The contract is a square × close
    # in the *header* + exactly one full-width primary Save in the footer;
    # dismissal is via the × / Escape / backdrop, never a footer Cancel. This
    # is what clears the /design-sync modal-header + modal-footer FAILs.
    state = {"manual_title": ""}
    _mock_sessions_list(authed_page, state)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = authed_page.locator(f'#sessionsList li[data-session-id="{_CODING_SID}"]')
    expect(row.locator(".name")).to_have_text("renameproj", timeout=10_000)

    row.locator('button[aria-label="Rename session"]').click()
    expect(authed_page.locator("#sessionRenameDialog")).to_be_visible()

    # Header × close: the compact modal.closeSize square (~34px) carrying the
    # `dialog-close` class, sitting in the header *above* the input — not a
    # footer button.
    close = authed_page.locator("#sessionRenameCancel")
    expect(close).to_have_class(re.compile(r"\bdialog-close\b"))
    close_box = close.bounding_box()
    field = authed_page.locator("#sessionRenameInput").bounding_box()
    save = authed_page.locator(
        "#sessionRenameForm button[type='submit']"
    ).bounding_box()
    assert close_box and field and save
    assert close_box["width"] <= 40 and close_box["height"] <= 40, (
        f"× close is not a compact square: {close_box['width']}x{close_box['height']}"
    )
    assert abs(close_box["width"] - close_box["height"]) <= 2
    assert close_box["y"] < field["y"], "× close must sit in the header, above the input"

    # Footer: exactly one action, and it is the full-width primary Save — its
    # width matches the input field (both stretch to the dialog content box),
    # never a half-row paired with a Cancel.
    footer_buttons = authed_page.locator("#sessionRenameForm .dialog-actions button")
    expect(footer_buttons).to_have_count(1)
    expect(footer_buttons).to_have_text(re.compile(r"Save"))
    assert save["width"] >= field["width"] - 3, (
        f"Save width {save['width']} is not full-width vs field {field['width']}"
    )
    assert save["width"] > close_box["width"] * 3, (
        "footer Save reads as a narrow paired button, not the full-width primary"
    )


def scenario_board_drawer_rename_btn_clickable_at_narrow_desktop_widths(
    authed_page: Page, base_url: str, width: int
) -> None:
    # Regression for #473: below ~1150px the desktop board grid
    # (styles/board.css `@media (min-width: 700px) and (pointer: fine)`) let the
    # drawer-actions row overflow past its own column — the empty "Your
    # turn" column (stretched tall by CSS Grid's default row `align-items:
    # stretch` once the drawer expands its sibling) then won by paint order
    # and swallowed the click. `flex-wrap` on `.board-drawer-actions` keeps
    # the row inside its own column at every width in this range.
    captured: dict = {}
    _mock_board(authed_page)
    _mock_rename(authed_page, _BOARD_SID, {"manual_title": ""}, captured)
    authed_page.set_viewport_size({"width": width, "height": 800})
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabBoard").click()
    expect(authed_page.locator("#paneBoard")).to_be_visible()

    card = authed_page.locator(
        '.board-list[data-col="bot_turn"] li.board-item'
    ).first.locator("button.board-card")
    expect(card.locator(".board-card-title")).to_have_text(
        "boardrenameproj", timeout=10_000
    )
    card.click()

    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    drawer.locator("button.board-rename-btn").click()
    expect(authed_page.locator("#sessionRenameDialog")).to_be_visible()


@pytest.mark.timeout(FLOW_TIMEOUT_S)
def test_sessions_flow(run_flow) -> None:
    """A live session from the list: its stop control, rename and title naming, the desktop mirror, closing, and the terminal view's back and kill controls.

    Each scenario below ran as its own test before; ``run_flow`` gives each a fresh
    browser context and reports every scenario that fails, not just the first.
    """
    run_flow(
        smoke.scenario_pty_session_renders_with_both_stop_buttons,
        scenario_coding_tab_rename_wins_over_launch_name,
        scenario_board_drawer_rename_patches_card_in_place,
        scenario_rename_dialog_adopts_modal_contract,
        functools.partial(scenario_board_drawer_rename_btn_clickable_at_narrow_desktop_widths, width=700),
        functools.partial(scenario_board_drawer_rename_btn_clickable_at_narrow_desktop_widths, width=900),
        functools.partial(scenario_board_drawer_rename_btn_clickable_at_narrow_desktop_widths, width=1100),
        session_title_naming.scenario_session_title_precedence,
        session_title_naming.scenario_mirror_doc_title_keeps_marker_and_human_title,
        shared_session_title.scenario_board_and_coding_tab_show_identical_shared_title,
        stop_unify_and_terminal_kill.scenario_terminal_view_has_back_arrow_and_kill_button,
        stop_unify_and_terminal_kill.scenario_kill_from_terminal_view_stops_and_returns_to_list,
        desktop_session_mirror.scenario_desktop_row_tap_mirrors_and_keeps_inpage_terminal_closed,
        edge_mirror_close.scenario_mirror_page_keeps_close_marker_in_document_title,
        edge_mirror_close.scenario_mirror_marker_applies_on_tailnet_origin,
        inpage_terminal_not_mirror.scenario_inpage_loopback_open_is_not_treated_as_mirror,
        screen_view.scenario_settings_opens_a_live_view_of_the_chosen_display,
    )
