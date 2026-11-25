"""The chief's timings: tunable defaults equal the constants they replaced, the
doorbell reads the configured values, and the read-only panel's fixed values
match the constants the code runs with."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.webapp import chief_doorbell as bell
from app.webapp.routers import board_chief
from src.board import transcript
from src.chief import fallback_wake, pointer, reconcile, timings

_ROOT = Path(__file__).resolve().parents[3]


def test_defaults_equal_the_constants_they_replaced():
    assert {t.key: t.default for t in timings.TUNABLES} == {
        "chief_rering_seconds": 60,
        "chief_unread_alert_minutes": 15,
        "chief_reoffer_minutes": 15,
        "chief_hold_minutes": 10,
    }
    assert bell.BACKOFF_SECONDS == 60.0
    assert bell.UNREAD_ALERT_SECONDS == 900.0
    assert bell.CLAIM_STALE_MS == 900_000
    for spec in timings.TUNABLES:
        assert spec.minimum <= spec.default <= spec.maximum


@pytest.mark.parametrize("value,ok", [(60, True), (30, True), (600, True), (29, False),
                                      (601, False), (True, False), (60.0, False), ("60", False)])
def test_range_problem(value, ok):
    assert (timings.range_problem("chief_rering_seconds", value) == "") is ok


def test_the_doorbell_reads_each_configured_value():
    cfg = SimpleNamespace(chief_rering_seconds=120, chief_unread_alert_minutes=30,
                          chief_reoffer_minutes=20)
    assert bell.rering_seconds(cfg) == 120.0
    assert bell.unread_alert_seconds(cfg) == 1800.0
    assert bell.claim_stale_ms(cfg) == 1_200_000
    stub = SimpleNamespace(chief_rering_seconds=True)
    assert bell.rering_seconds(stub) == bell.BACKOFF_SECONDS
    assert bell.claim_stale_ms(stub) == bell.CLAIM_STALE_MS


def test_the_back_off_and_unread_alarm_follow_their_arguments():
    obs = bell.Observation(pending=1, chief_sid="c", status="idle-finished", dirty=False,
                           oldest_age=700.0)
    door = bell.Doorbell()
    door.decide(obs, 0.0, backoff=120.0)
    assert door.decide(obs, bell.DEBOUNCE_SECONDS, backoff=120.0) == "ring"
    rang = bell.DEBOUNCE_SECONDS
    assert door.decide(obs, rang + 90.0, backoff=120.0) == "backing off after ring"
    assert door.decide(obs, rang + 120.0, backoff=120.0) == "debouncing"
    assert door.unread_alarm(obs, after=900.0) is False
    assert door.unread_alarm(obs, after=600.0) is True


def test_a_claim_is_pending_again_after_the_configured_reoffer(tmp_path):
    claimed = tmp_path / ".claimed"
    claimed.mkdir()
    now_ms = 10_000_000
    (claimed / f"{now_ms - 16 * 60_000}-a.json").write_text("{}", encoding="utf-8")
    assert bell.scan_pending(tmp_path, now_ms).count == 1
    assert bell.scan_pending(tmp_path, now_ms, stale_ms=20 * 60_000).count == 0


def _constant(rel: str, name: str) -> float:
    """A module-level number in a file this test may not import (the copilot tree)."""
    tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return float(eval(compile(ast.Expression(node.value), rel, "eval"), {}))  # noqa: S307 -- a numeric literal expression from this repo
    raise AssertionError(f"{name} not found in {rel}")


def test_each_fixed_value_matches_its_source_constant():
    shown = {f.label: f.value for f in timings.FIXED}
    expected = {
        "Doorbell check": f"every {bell.TICK_SECONDS:g} s",
        "Ring settle time": f"{bell.DEBOUNCE_SECONDS:g} s",
        "Stuck 'working' label": f"{bell.STALE_WORKING_SECONDS / 60:g} min silent",
        "Fallback check": f"every {fallback_wake.CHECK_SECONDS:g} s",
        "Missed turn-end recovery":
            f"{reconcile.TICK_SECONDS:g} s check, {reconcile.GRACE_SECONDS:g} s grace",
        "Turn-end repeat":
            f"{_constant('copilot/hooks/chief_inbox.py', 'TURN_END_DEBOUNCE_SECONDS'):g} s",
        "Mail expiry": "24 h",
        "Compact freshness":
            f"{_constant('copilot/skills/_lib/chief/compact.py', 'COMPACT_FRESH_SECONDS') / 60:g} min",
        "Idle-slot reading":
            f"{_constant('copilot/skills/_lib/chief/drain.py', 'LAST_FRESH_SECONDS') / 60:g} min",
        "New lane grace":
            f"{_constant('copilot/skills/_lib/chief/chief_sweep.py', 'GRACE_S') / 60:g} min",
        "Board 'stalled'": f"{transcript._STALLED_DISPATCH_AFTER.total_seconds() / 60:g} min",
        "Respawn wait": f"{board_chief.CHIEF_STOP_WAIT_S:g} s",
        "Resume window": f"{pointer.POINTER_STALE_AFTER.days} days",
    }
    assert shown == expected
    assert _constant("copilot/skills/_lib/chief/mailbox.py", "INBOX_EVENT_TTL_SECONDS") == 24 * 3600
