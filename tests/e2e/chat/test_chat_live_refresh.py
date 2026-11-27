"""Chat live refresh: new turns appear with no tap, only the visible chat polls, scroll and open cards survive,
and overlapping reads never append a turn twice."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.chat._support import (
    _BACKOFF_MAX_MS,
    _OTHER_SID,
    _SID,
    _boot,
    _menu_item,
    _mock_sessions_list,
    _open_chat,
    _pull,
    _tick,
    _tool,
    _Transcript,
    _turn,
)
from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS, stable_read

pytestmark = pytest.mark.smoke


def _check_forced_read_via_load_new(page: Page, tr, open_turn) -> None:
    """Cover the forced read: Load new with nothing new, then with a new turn."""
    # -- #1292: forcing a read, for when polling seems stuck --
    # ⋮ Load new with nothing new says so, briefly, and rebuilds nothing.
    # The note lives in the bottom strip (#1387), not over the transcript.
    newer = page.locator("#terminalActivity")
    expect(page.locator("#transcriptNewer")).to_have_count(0)
    page.evaluate("document.querySelector('#transcriptList .tr-turn')._kept = 1")
    _menu_item(page, "Load new messages").click()
    expect(newer).to_have_text("No new messages")
    _fake_clock.advance(page, 2_500)       # NEWER_NOTE_MS (2 s) in session-transcript.js
    expect(newer).to_be_hidden()

    # Timed against the poll: right after a tick's read lands, the next is a
    # full default transcript poll (3 s) away, so a turn that shows within 1.5 s of the
    # tap came from the forced read, not the timer.
    _tick(page, tr)
    tr.append(_turn("user", "and the second flake?", 350))
    before = tr.tail_calls()
    _menu_item(page, "Load new messages").click()
    expect(page.locator("#transcriptList")).to_contain_text("and the second flake?", timeout=1_500)
    assert tr.tail_calls() > before
    # Appended, never rebuilt: the first turn is the same node.
    assert page.evaluate("document.querySelector('#transcriptList .tr-turn')._kept") == 1
    # The fragment that arrived as the provisional tail has settled into the
    # same turn, once: dropping the tail took its part with it.
    expect(open_turn.locator(".tr-reply")).to_have_count(2)


def _check_pull_up_forces_a_read(page: Page, tr) -> None:
    """Cover a pull up past the bottom reading new turns, and a too-short drag doing nothing."""
    # A pull up past the bottom is the same read.
    _tick(page, tr)
    tr.append(_turn("assistant", "same cause, same fix", 380))
    page.evaluate("const b = document.getElementById('transcriptBody'); b.scrollTop = b.scrollHeight")
    _pull(page, -120)
    expect(page.locator("#transcriptList")).to_contain_text("same cause, same fix", timeout=1_500)
    # A drag too short to be a pull does nothing.
    calls = tr.tail_calls()
    _pull(page, -20)
    _fake_clock.advance(page, 1)           # only to let a (wrongly) fired read reach the stub
    assert tr.tail_calls() == calls


def _check_a_moved_log_reloads_its_whole_history(page: Page, tr) -> None:
    """#713: the forward cursor belongs to one log. When a resume moves the
    session to another, its history sits below that offset, so a tick that only
    appended would never show it."""
    tr.log_id = "log-b"
    tr.entries[:] = [_turn("user", "from the earlier run", 10)]
    _tick(page, tr)
    chat = page.locator("#transcriptList")
    expect(chat).to_contain_text("from the earlier run", timeout=OVERLAY_OPEN_MS)
    expect(chat).not_to_contain_text("opening conftest now")


@pytest.mark.iphone
def test_new_turns_appear_with_no_user_action(authed_page: Page, base_url: str) -> None:
    """The feature: the conversation refreshes by itself, starting from a chat
    opened before anything was said: Copilot writes its log at launch, so the
    first read is empty, and "Nothing in the transcript yet" has to go when the
    first turns land on a tick, next to a strip already counting actions."""
    page = authed_page
    tr = _boot(page, base_url)
    first_turns = list(tr.entries)
    tr.entries.clear()
    _open_chat(page)
    empty = page.locator("#transcriptState")
    expect(empty).to_have_text("Nothing in the transcript yet", timeout=OVERLAY_OPEN_MS)

    tr.append(*first_turns)
    _tick(page, tr)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2, timeout=OVERLAY_OPEN_MS)
    expect(empty).to_be_hidden()

    tr.append(_turn("assistant", "found it — a missing await", 300))
    # No tap, no reload: the next tick brings it in.
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text(
        "found it — a missing await", timeout=OVERLAY_OPEN_MS
    )
    # #1475: the new fragment joins the open turn under its one header, the
    # way a run card takes new tool calls — not a turn of its own.
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)
    open_turn = page.locator("#transcriptList .tr-assistant")
    expect(open_turn).to_have_count(1)
    expect(open_turn.locator(".tr-reply")).to_have_text(
        ["opening conftest now", "found it — a missing await"]
    )
    expect(open_turn.locator(".tr-turn-summary")).to_have_count(1)

    _check_forced_read_via_load_new(page, tr, open_turn)

    _check_pull_up_forces_a_read(page, tr)

    _check_a_moved_log_reloads_its_whole_history(page, tr)


def test_live_polling_follows_the_visible_chat_only(
    authed_page: Page, base_url: str
) -> None:
    """The live poll runs for the open conversation in Chat mode and nowhere
    else (#1515 folded three tests that each paid for their own boot, row open
    and tick; the one boot now walks all three states in turn):

    * The other live session's transcript is never asked for.
    * A closed overlay fetches nothing.
    * A window sitting on Terminal must not also stream chat for that session
      — half of the "10 windows" constraint.
    """
    page = authed_page
    tr = _boot(page, base_url)
    other_calls: list[str] = []
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + _OTHER_SID + r"/transcript.*"),
        lambda route: (
            other_calls.append(route.request.url),
            route.fulfill(status=200, content_type="application/json", body="{}"),
        )[-1],
    )
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)
    _tick(page, tr)
    assert tr.tail_calls() >= 1
    assert other_calls == [], "a session nobody opened was polled"

    # Closed overlay: the poll stops.
    page.locator("#terminalBack").click()
    expect(page.locator("#terminalOverlay")).to_be_hidden()
    settled = tr.tail_calls()
    _fake_clock.advance(page, 2 * _BACKOFF_MAX_MS)
    assert tr.tail_calls() == settled, "a closed overlay kept polling"

    # Reopened on Chat it ticks again; switched to Terminal it stops again.
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)
    _tick(page, tr)          # at least one tick while Chat shows
    assert tr.tail_calls() > settled, "Chat mode should have ticked at least once"

    page.locator("#sessionModeTerminal").click()
    expect(page.locator("#terminalOverlay")).to_have_attribute("data-mode", "terminal")
    settled = tr.tail_calls()
    _fake_clock.advance(page, 2 * _BACKOFF_MAX_MS)          # long enough for several ticks
    assert tr.tail_calls() == settled, (
        "Terminal mode kept fetching chat: "
        f"{tr.tail_calls() - settled} request(s) after the switch"
    )
    assert other_calls == [], "a session nobody opened was polled"


# The app keeps a reader "following" within this far of the bottom
# (latest-pill.js LATEST_SLACK_PX); parking inside it is not "scrolled up".
_FOLLOW_SLACK_PX = 120


# The first turn in view and where it sits in the pane: what the reader is
# looking at, independent of how far the pane has scrolled to show it.
_ANCHOR_JS = """(el, text) => {
  const top = el.getBoundingClientRect().top;
  const turns = [...el.querySelectorAll('.tr-turn')];
  const t = text ? turns.find(n => n.textContent.includes(text))
                 : turns.find(n => n.getBoundingClientRect().bottom > top + 1);
  return t ? {text: t.textContent.trim().slice(0, 40),
              offset: Math.round(t.getBoundingClientRect().top - top)} : null;
}"""


# viewport_height: a tall pane put a fixed fraction of scrollHeight inside the
# follow slack. shift_above: the browser keeps the visible content in place when
# something above it grows after the baseline read (a late font or image), which
# raised scrollTop by the same 25px the full gate runs saw (738 -> 759).
@pytest.mark.iphone
@pytest.mark.parametrize(
    "viewport_height, shift_above",
    [(None, 0), (1590, 0), (None, 25)],
    ids=["default", "tall", "late-shift-above"],
)
def test_an_appended_turn_leaves_scroll_and_open_cards_alone(
    authed_page: Page, base_url: str, browser_name: str,
    viewport_height: int | None, shift_above: int,
) -> None:
    """The #680/#958 lesson: a poll that rebuilds the DOM breaks whatever the
    reader is interacting with. Refresh is incremental precisely so an open
    tool-call group and a scrolled-up reader both survive a tick."""
    page = authed_page
    if viewport_height is not None:
        if browser_name != "chromium":
            pytest.skip("the tall pane is a desktop-viewport case")
        page.set_viewport_size({"width": 1280, "height": viewport_height})
    tr = _boot(page, base_url)
    tr.append(
        _tool("Bash", "pytest tests/e2e -q", "3 passed", 150),
        *[_turn("user", f"filler prompt {i}", 1000 + i * 10) for i in range(20)],
    )
    _open_chat(page)
    # Open a step line (shown by default since #1476), then scroll up off
    # the tail.
    group = page.locator("#transcriptList .tr-step").first
    group.locator("summary.tr-step-line").click()
    expect(group).to_have_attribute("open", "")
    # Park the reader partway up the history — deliberately *not* at 0,
    # where a full rebuild would also land, so this position distinguishes
    # "left alone" from both failure modes: reset to the top and jump to the
    # bottom. 40% of the scrollable range, not of scrollHeight, so the spot
    # stays well above the follow slack whatever the pane's height.
    body = page.locator("#transcriptBody")
    body.evaluate(
        "el => { el.scrollTop = Math.round((el.scrollHeight - el.clientHeight) * 0.4); }"
    )
    before = stable_read(lambda: body.evaluate("el => el.scrollTop"))
    assert before, "the transcript did not scroll; the fixture is too short"
    below = stable_read(
        lambda: body.evaluate("el => el.scrollHeight - el.scrollTop - el.clientHeight")
    )
    assert below > _FOLLOW_SLACK_PX, f"parked {below}px from the bottom: inside the follow slack"
    anchor = stable_read(lambda: body.evaluate(_ANCHOR_JS, None))
    assert anchor, "no turn in view to anchor on"
    if shift_above:
        page.evaluate(
            "px => { document.querySelector('#transcriptList .tr-turn').style.paddingTop = px + 'px'; }",
            shift_above,
        )

    tr.append(_turn("assistant", "a brand new reply", 5000))
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text(
        "a brand new reply", timeout=OVERLAY_OPEN_MS
    )
    # The group the reader opened is still open, and the turn they were
    # reading is still where they left it in the pane: an append must not
    # scroll them to the bottom or the top.
    expect(group).to_have_attribute("open", "")
    # A raw geometry read on a surface that now re-renders on a poll, so it
    # goes through stable_read (#680) — the list rebuilds only its newest
    # message, but the convention holds for any polled surface.
    after = stable_read(lambda: body.evaluate(_ANCHOR_JS, anchor["text"]))
    assert after == anchor, f"the reader was moved: {anchor} -> {after}"


def test_a_reply_arriving_after_tool_calls_is_unfolded_and_a_user_fold_stays(
    authed_page: Page, base_url: str
) -> None:
    """#1531 — a new agent reply must read straight away, never arrive folded.

    A turn that starts with a tool call is keyed on that entry's offset, and
    so are its step line and the call's own item (both folded by default). The
    provisional tail is harvested by key before it is replaced, so the closed
    step line's state used to be restored onto the open turn the moment the
    reply settled it: the reply arrived behind a one-line summary. Only a fold
    the reader made themselves may persist, and only on that turn.
    """
    page = authed_page
    _mock_sessions_list(page)
    tr = _Transcript(page)
    tr.append(_turn("user", "please look at the flaky test", 100))
    _fake_clock.install(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_chat(page)
    turns = page.locator("#transcriptList .tr-assistant")
    expect(page.locator("#transcriptList .tr-user")).to_have_count(1)

    # The turn is born in the provisional tail, as a bare step line...
    tr.append(_tool("Bash", "pytest tests/e2e -q", "3 passed", 150))
    _tick(page, tr)
    expect(turns).to_have_count(1, timeout=OVERLAY_OPEN_MS)
    expect(turns.first).to_have_attribute("open", "")
    # ...and the reply that follows settles it.
    tr.append(_turn("assistant", "the reply the reader is waiting for", 200))
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text(
        "the reply the reader is waiting for", timeout=OVERLAY_OPEN_MS
    )
    expect(turns).to_have_count(1)
    expect(turns.first).to_have_attribute("open", "")

    # The reader folds this turn; a later turn must not inherit that fold.
    turns.first.locator("summary.tr-turn-summary").click()
    expect(turns.first).not_to_have_attribute("open", "")
    tr.append(
        _turn("user", "next question", 300),
        _tool("Bash", "git status", "clean", 350),
    )
    _tick(page, tr)
    tr.append(_turn("assistant", "the second reply", 400))
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text(
        "the second reply", timeout=OVERLAY_OPEN_MS
    )
    expect(turns).to_have_count(2)
    expect(turns.first).not_to_have_attribute("open", "")
    expect(turns.nth(1)).to_have_attribute("open", "")


# session-transcript.js SENT_REFRESH_MS: how long after a send the pane
# nudges the live poll.
_SENT_REFRESH_MS = 3_000

_CONFIRMED_SEND = {
    "ok": True, "submit": True, "delivered": True, "ingested": True,
    "submitted": True, "submit_confirmed": True, "submit_state": "confirmed",
}


def test_overlapping_reads_append_once_a_transient_source_recovers_and_an_ended_one_stops(
    authed_page: Page, base_url: str
) -> None:
    """A refresh nudge that lands while a read is in flight waits for it, so
    no turn is appended twice.

    Only an *ended session* is terminal. A live session reports
    `no_transcript` for as long as no log can be named for it — a condition that clears by itself,
    so refresh must back off and recover rather than latch off and make the
    reader tap Reload. A session that ends stops refreshing and says so.
    """
    page = authed_page
    tr = _boot(page, base_url)
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + _SID + r"/input$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(_CONFIRMED_SEND),
        ),
    )
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)

    tr.append(_turn("assistant", "one thing first", 250))
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text("one thing first", timeout=OVERLAY_OPEN_MS)

    # One read is held open; the send's refresh nudge fires while it is.
    # Against a real phone this is a slow tick that is still on the wire when
    # the nudge lands.
    tr.append(_turn("assistant", "only once please", 400), _turn("assistant", "tail", 450))
    tr.hold = True
    _tick(page, tr)
    composer = page.locator("#chatComposeBar")
    composer.locator(".composer-input").fill("go on")
    with page.expect_response(re.compile(r".*/input$")):
        composer.locator(".composer-send").click()
    expect(page.locator(".toast", has_text="Sent")).to_be_visible()
    _fake_clock.advance(page, _SENT_REFRESH_MS + 1)
    # The nudge has not started a second read from the same cursor.
    assert len(tr.held) == 1, tr.calls[-3:]
    tr.release()
    _fake_clock.advance(page, 1)
    # A nudge waited out the held read and then read from its cursor, if at
    # all; either way the release left nothing more held.
    tr.release()
    _fake_clock.advance(page, 1)

    expect(page.locator("#transcriptList")).to_contain_text("only once please")
    expect(page.locator("#transcriptList .tr-reply", has_text="only once please")).to_have_count(1)

    tr.unavailable = "no_transcript"
    _tick(page, tr)
    expect(page.locator("#transcriptState")).to_contain_text(
        "No transcript found", timeout=OVERLAY_OPEN_MS
    )
    # A live session's answer can name the link that failed.
    tr.missing = "recorded_file_gone"
    _tick(page, tr)
    expect(page.locator("#transcriptState")).to_contain_text(
        "the recorded log file is gone", timeout=OVERLAY_OPEN_MS
    )
    # It is still trying, unlike the ended case below.
    settled = tr.tail_calls()
    _tick(page, tr)
    assert tr.tail_calls() > settled, "a transient condition latched refresh off"

    tr.unavailable = None
    tr.append(_turn("assistant", "back again", 500))
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text(
        "back again", timeout=OVERLAY_OPEN_MS
    )
    expect(page.locator("#transcriptState")).to_be_hidden()

    tr.dead = True
    _tick(page, tr)
    expect(page.locator("#transcriptState")).to_contain_text(
        "no longer running", timeout=OVERLAY_OPEN_MS
    )
    settled = tr.tail_calls()
    _fake_clock.advance(page, 2 * _BACKOFF_MAX_MS)
    assert tr.tail_calls() == settled, "a dead session was still being polled"
    # What was already read stays readable.
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)
    expect(page.locator("#transcriptList")).to_contain_text("back again")
    expect(page.locator("#transcriptList .tr-reply", has_text="only once please")).to_have_count(1)
