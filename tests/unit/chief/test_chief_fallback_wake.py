"""The launcher's fallback wake: a fake clock drives it minute by minute,
fake reads and a fake send stand in for the session-host, so nothing types
into a real session."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.webapp import chief_doorbell as bell_mod
from app.webapp.chief_doorbell import Observation, _SkipLog, fallback_tick_once
from src.chief import fallback_wake
from src.chief.fallback_wake import Facts, FallbackWake

_CONFIRMED = {"reason": "ok", "delivered": True, "submit_state": "confirmed"}
_MIN = 60.0


def _facts(**kw) -> Facts:
    base = dict(oldest_age=None, free_slots=0, queued=0, idle=True, dirty=False)
    base.update(kw)
    return Facts(**base)


async def _run(minutes, facts_at, *, steps):
    """Check once a minute for ``steps`` minutes; ``facts_at(t)`` gives the
    facts at clock ``t``. Returns (decisions, sends)."""
    cfg = SimpleNamespace(chief_fallback_minutes=minutes, session_host_port=54321)
    clock = SimpleNamespace(t=0.0)
    sends, decisions, observes = [], [], []

    async def fake_observe(_cfg):
        observes.append(clock.t)
        obs = Observation(pending=1, chief_sid="chief-sid", status="idle-finished", dirty=False)
        return obs, facts_at(clock.t)

    def fake_send(port, sid, data, submit=True):
        sends.append((clock.t, sid, data))
        return dict(_CONFIRMED)

    fallback, skips = FallbackWake(), _SkipLog()
    for _ in range(steps):
        decisions.append(await fallback_tick_once(
            cfg, fallback, skips, now=lambda: clock.t, observe_fn=fake_observe,
            send_fn=fake_send, record_fn=lambda *a, **k: True,
        ))
        clock.t += _MIN
    return decisions, sends, observes


class TestFallbackTick:
    async def test_a_missed_mailbox_event_rings_after_n_minutes_and_logs_why(self, caplog):
        """Acceptance: the doorbell never delivered (event waits from t=0);
        the fallback rings at 30 minutes, once, and the log names the reason."""
        with caplog.at_level(logging.INFO, logger=bell_mod.logger.name):
            decisions, sends, _ = await _run(30, lambda t: _facts(oldest_age=t), steps=61)
        assert [t for t, _, _ in sends] == [30 * _MIN, 60 * _MIN]
        assert decisions[30] == "ring" and decisions[29] == "nothing waiting"
        assert sends[0][2] == bell_mod.DOORBELL_LINE
        rings = [r.getMessage() for r in caplog.records if "chief fallback rang" in r.getMessage()]
        assert len(rings) == 2 and "fallback=mailbox (oldest event waited 30 min)" in rings[0]

    async def test_free_slots_beside_queued_rows_ring_once_per_window(self):
        """Acceptance: idle slots held for N minutes give one ring, and no
        second ring inside the next window."""
        decisions, sends, _ = await _run(10, lambda t: _facts(free_slots=2, queued=3), steps=15)
        assert [t for t, _, _ in sends] == [10 * _MIN]
        assert decisions[10] == "ring" and decisions[11] == "nothing waiting"

    async def test_zero_is_off_and_reads_nothing(self):
        """Acceptance: 0 turns it off; no read, no ring, however late the mail."""
        decisions, sends, observes = await _run(0, lambda t: _facts(oldest_age=9e9), steps=90)
        assert decisions == ["off"] * 90 and sends == [] and observes == []

    async def test_slots_with_no_queued_rows_never_ring(self):
        decisions, sends, _ = await _run(5, lambda t: _facts(free_slots=3, queued=0), steps=20)
        assert sends == [] and set(decisions) == {"nothing waiting"}


class TestDecideSafety:
    def test_never_rings_a_busy_chief_or_over_a_dirty_composer(self):
        fb = FallbackWake()
        assert fb.decide(5, _facts(oldest_age=600, idle=False), 0).reason == "chief not idle"
        assert fb.decide(5, _facts(oldest_age=600, dirty=True), 60).reason == "composer dirty"
        assert fb.decide(5, _facts(oldest_age=600, blocked="no live chief"), 120).reason == "no live chief"

    def test_an_unknown_composer_rings_once_after_the_window(self):
        """An unknown input line would hold the bell forever: past N minutes
        the ring goes through once, and the next waits a whole window."""
        fb = FallbackWake()
        facts = _facts(oldest_age=3600, dirty=None)
        held = [fb.decide(5, facts, t * _MIN).ring for t in range(5)]
        assert held == [False] * 5
        ring = fb.decide(5, facts, 5 * _MIN)
        assert ring.ring and "composer unknown past the window" in ring.reason
        assert not any(fb.decide(5, facts, t * _MIN).ring for t in range(6, 10))

    def test_a_dirty_composer_restarts_the_unknown_clock(self):
        fb = FallbackWake()
        fb.decide(5, _facts(oldest_age=3600, dirty=None), 0)
        fb.decide(5, _facts(oldest_age=3600, dirty=True), 4 * _MIN)
        assert not fb.decide(5, _facts(oldest_age=3600, dirty=None), 6 * _MIN).ring


class TestSlotCount:
    _LIVE = [
        {"session_id": "chief-sid", "alive": True, "label": "chief"},
        {"session_id": "lane-1", "alive": True},
        {"session_id": "mine", "alive": True},
        {"session_id": "dead", "alive": False},
        {"session_id": "tg", "alive": True, "label": "telegram:main"},
    ]

    @pytest.mark.parametrize("scope, managed, free", [
        ("chief", {"lane-1"}, 2), ("all", None, 1), ("chief", None, None),
    ])
    def test_counts_workers_by_scope(self, scope, managed, free):
        assert fallback_wake.free_slots(self._LIVE, "chief-sid", 3, scope, managed) == free

    def test_queued_rows_counts_only_queued_and_unreadable_is_unknown(self):
        plan = {"state": "ok", "queue": [{"status": "queued"}, {"status": "building"}, {"status": "queued"}]}
        assert fallback_wake.queued_rows(plan) == 2
        assert fallback_wake.queued_rows({"state": "unreadable"}) is None
