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


def test_a_session_that_is_gone_leaves_no_cache_entry(reads):
    async def run():
        session_credits.attach_ai_credits(1, [_session()])
        await _settle()
        session_credits.attach_ai_credits(1, [])

    asyncio.run(run())
    assert session_credits._cache == {}
