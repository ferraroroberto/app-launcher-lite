"""The AI credits a Copilot footer shows, and how the sessions poll carries them.

The rows are shaped like Copilot CLI 1.0.114's own captures (see
``test_copilot_session``) with the ``Session: N AIC used`` figure added; the
figures are made up. Whether a live Copilot paints the figure on exactly this
row is not proven here: a Copilot that paints none reads ``None``."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from app.webapp import session_credits
from src.session_view.copilot_credits import ai_credits_used
from tests.unit.sessions.test_copilot_session import IDLE, NARROW, RULE

WIDE_FOOTER = (
    " ← open sidebar · Interactive · Manual Approval · / commands"
    "    Session: 5.62 AIC used · MAI Code 2 Flash · Medium · (3%)"
)
SOLO = "                                                       Session: 12.3 AIC used"


def _screen(footer: str) -> List[str]:
    return IDLE[:-3] + [footer, "", ""]


@pytest.mark.parametrize("lines,expected", [
    (_screen(WIDE_FOOTER), 5.62),
    (_screen(SOLO), 12.3),
    (_screen(" Session: 1,234.5 AIC used"), 1234.5),
    (_screen(" Session: 0 AIC used"), 0.0),
    # A footer without the figure, as an older Copilot paints it.
    (IDLE, None),
    # 46 columns: the row is cut before the figure.
    (NARROW, None),
    (_screen(" Interactive    Session: 5.6…"), None),
    (_screen(" Session: 5.62 USD used"), None),
    ([], None),
    (["", ""], None),
])
def test_the_footer_figure_is_read_as_the_screen_shows_it(lines, expected):
    assert ai_credits_used(lines) == expected


# ---- the two-row footer: the figure above the input box -----------------------

STATUS = " Plan: 26.957/35.100 (76% used) · Session: 15.99 AIC used · YOLO"
MODEL = " GPT-6 Luna · Extra High · (9%)"
WIDE_RULE = "─" * 120


def _two_row(status: str = STATUS, above=(" ● Done.", ""), box=("> ",), rule: str = WIDE_RULE):
    return [*above, status, rule, *box, rule, MODEL, "", ""]


def test_the_figure_above_the_input_box_is_read_when_the_bottom_row_has_none():
    assert ai_credits_used(_two_row()) == 15.99
    # A wrapped, multi-line prompt in the box and blank rows above the status row.
    wrapped = _two_row(box=("> fix the", "  failing tests", "  please"), above=(" ● Done.", "", ""))
    assert ai_credits_used(wrapped) == 15.99
    # A narrow terminal: shorter rules, the row still carries the figure.
    narrow = _two_row(" Session: 15.99 AIC used · YOLO", rule="━" * 46)
    assert ai_credits_used(narrow) == 15.99


def test_a_two_row_footer_cut_before_the_figure_reads_none():
    cut = _two_row(" Plan: 26.957/35.100 (76% used) · Sess…", rule="─" * 46)
    assert ai_credits_used(cut) is None
    assert ai_credits_used(_two_row(" Plan: 26.957/35.100 (76% used) · YOLO")) is None


def test_a_figure_only_in_the_conversation_above_the_input_box_is_not_credits():
    quoted = [" The footer says Session: 15.99 AIC used", " ● Done.", ""]
    assert ai_credits_used(_two_row(" Plan: 26.957/35.100 (76% used) · YOLO", above=quoted)) is None
    # Past the status row's gap it is out of reach even with nothing between.
    far = [" Session: 15.99 AIC used", "", "", "", ""]
    assert ai_credits_used(_two_row(" Plan: 26.957/35.100 (76% used)", above=far)) is None


def test_a_screen_with_no_input_box_reads_none_below_the_bottom_row():
    assert ai_credits_used([STATUS, " ● Done.", MODEL]) is None
    assert ai_credits_used([STATUS, RULE, "❯", " one", " two", " three"]) is None


@pytest.mark.parametrize("figure,expected", [
    ("15.99", 15.99),
    ("0.5", 0.5),
    ("1,234.5", 1234.5),
    ("1,234", 1234.0),
    ("12,345,678.9", 12345678.9),
    ("1.234,5", 1234.5),
    ("1.234.567", 1234567.0),
    ("12.3456", 12.3456),
    ("0.123", 0.123),
    ("0", 0.0),
    # One dot and three digits is 1.234 or 1234: unreadable one way only.
    ("1.234", None),
    ("26.957", None),
    ("1,5", None),
    ("1,23,456", None),
    ("1,2345.6", None),
    ("1.23.456", None),
    ("1.234,56,7", None),
    ("1,234,5.6", None),
])
def test_the_number_is_read_one_way_or_not_at_all(figure, expected):
    assert ai_credits_used([f" Session: {figure} AIC used"]) == expected
    assert ai_credits_used(_two_row(f" Plan: 26.957/35.100 · Session: {figure} AIC used")) == expected


# ---- the footer's cells wrapped over two rows ---------------------------------

# A long session viewed on a narrow terminal: each footer cell is two rows tall,
# so ``Session: N`` sits on one row and its ``AIC`` unit on the row below.
WRAPPED_TOP = " +0 Plan: 27.448/35.100 · Session: 39.74 · YOLO"
WRAPPED_BOTTOM = " -0 (78% used) AIC used"


def _wrapped(top: str = WRAPPED_TOP, bottom: str = WRAPPED_BOTTOM, above=(" ● Done.", "")):
    return [*above, top, bottom, WIDE_RULE, "> ", WIDE_RULE, MODEL, "", ""]


def test_a_footer_wrapped_over_two_rows_reads_its_figure():
    assert ai_credits_used(_wrapped()) == 39.74
    # The plain wrap: the figure ends one row, its unit starts the next.
    assert ai_credits_used(_wrapped(" Plan: 27.448/35.100 · Session: 39.74", " AIC used · YOLO")) == 39.74
    # A narrow terminal's shorter rules and a blank row above the footer.
    narrow = [" ● Done.", "", WRAPPED_TOP, WRAPPED_BOTTOM, "─" * 46, "> ", "─" * 46, MODEL]
    assert ai_credits_used(narrow) == 39.74


def test_a_wrapped_footer_that_is_not_credits_reads_none():
    # Another unit, a figure cut before its end and a row with no AIC below it.
    assert ai_credits_used(_wrapped(" Plan: 27.448/35.100 · Session: 39.74 USD · YOLO")) is None
    assert ai_credits_used(_wrapped(" Plan: 27.448/35.100 · Session: 39.7…")) is None
    assert ai_credits_used(_wrapped(bottom=" -0 (78% used)")) is None
    # A quoted pair of rows in the conversation, out of the status block's reach.
    quoted = [" Session: 39.74", " AIC used", "", "", ""]
    assert ai_credits_used(_wrapped(" Plan: 27.448/35.100 (78% used)", " -0", above=quoted)) is None


def test_only_the_bottom_most_row_counts():
    older = ["  Session: 9.99 AIC used", RULE, "❯", RULE]
    assert ai_credits_used(older + _screen(WIDE_FOOTER)) == 5.62
    # A newer footer without the figure is None, never the older row's figure.
    assert ai_credits_used(older + IDLE) is None


def test_the_plan_allowance_row_is_not_credits():
    plan = " [⎇ master%]        Plan: 459/1500 (30% used) · Run on: This computer"
    assert ai_credits_used([plan, RULE, "❯", RULE, " Interactive"]) is None


# ---- the sessions poll's cache ------------------------------------------------

def _session(sid: str = "s1", **over: Any) -> Dict[str, Any]:
    return {"session_id": sid, "agent": "copilot", "kind": "pty", "rows": 10, "cols": 120, **over}


@pytest.fixture
def reads(monkeypatch):
    """Replace the screen read; ``reads.screen`` is what it returns."""
    calls: List[str] = []

    class Reads:
        screen: Optional[List[str]] = _screen(WIDE_FOOTER)

    async def fake(port, sid, rows, cols):
        calls.append(sid)
        return Reads.screen

    monkeypatch.setattr(session_credits, "read_vt_screen", fake)
    session_credits._cache.clear()
    session_credits._inflight.clear()
    Reads.calls = calls
    yield Reads
    session_credits._cache.clear()


async def _settle() -> None:
    while session_credits._tasks:
        await asyncio.gather(*list(session_credits._tasks))


def test_the_first_poll_waits_for_nothing_and_a_later_one_carries_the_figure(reads):
    async def run():
        first = [_session()]
        session_credits.attach_ai_credits(1, first)
        await _settle()
        second = [_session()]
        session_credits.attach_ai_credits(1, second)
        return first[0]["ai_credits"], second[0]["ai_credits"]

    assert asyncio.run(run()) == (None, 5.62)
    assert reads.calls == ["s1"]


def test_a_fresh_reading_is_reused_not_read_again(reads):
    async def run():
        for _ in range(3):
            session_credits.attach_ai_credits(1, [_session()])
            await _settle()

    asyncio.run(run())
    assert reads.calls == ["s1"]


def test_claude_detached_and_unreadable_sessions_show_none(reads):
    async def run():
        sessions = [
            _session("c", agent="claude"), _session("d", kind="remote"), _session("n"),
        ]
        reads.screen = _screen(" Interactive · Medium · (3%)")
        session_credits.attach_ai_credits(1, sessions)
        await _settle()
        session_credits.attach_ai_credits(1, sessions)
        return [s["ai_credits"] for s in sessions]

    assert asyncio.run(run()) == [None, None, None]
    assert reads.calls == ["n"]


def test_a_screen_that_cannot_be_read_keeps_the_last_figure(reads, monkeypatch):
    async def run():
        session_credits.attach_ai_credits(1, [_session()])
        await _settle()
        monkeypatch.setattr(session_credits, "_TTL_S", 0.0)
        reads.screen = None
        sessions = [_session()]
        session_credits.attach_ai_credits(1, sessions)
        await _settle()
        session_credits.attach_ai_credits(1, sessions)
        return sessions[0]["ai_credits"]

    assert asyncio.run(run()) == 5.62


def test_a_restarted_session_starts_again_from_its_screen(reads, monkeypatch):
    async def run():
        session_credits.attach_ai_credits(1, [_session()])
        await _settle()
        monkeypatch.setattr(session_credits, "_TTL_S", 0.0)
        reads.screen = _screen(" Session: 0.4 AIC used")
        sessions = [_session()]
        session_credits.attach_ai_credits(1, sessions)
        await _settle()
        session_credits.attach_ai_credits(1, sessions)
        return sessions[0]["ai_credits"]

    assert asyncio.run(run()) == 0.4


def test_a_reading_without_the_figure_keeps_the_last_one(reads, monkeypatch):
    """A repaint caught mid-draw or a footer cut short reads ``None``: unknown,
    so what was true a moment ago stays."""
    async def run():
        session_credits.attach_ai_credits(1, [_session()])
        await _settle()
        monkeypatch.setattr(session_credits, "_TTL_S", 0.0)
        reads.screen = _screen(" Interactive · Medium · (3%)")
        sessions = [_session()]
        session_credits.attach_ai_credits(1, sessions)
        await _settle()
        session_credits.attach_ai_credits(1, sessions)
        return sessions[0]["ai_credits"]

    assert asyncio.run(run()) == 5.62


def test_a_session_that_is_gone_leaves_no_cache_entry(reads):
    async def run():
        session_credits.attach_ai_credits(1, [_session()])
        await _settle()
        session_credits.attach_ai_credits(1, [])

    asyncio.run(run())
    assert session_credits._cache == {}
