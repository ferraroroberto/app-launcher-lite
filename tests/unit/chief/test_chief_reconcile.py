"""A chief-managed lane's turn end or exit reaches the mailbox even when its hook missed it.

Every test uses a tmp registry, inbox and transcript: the real chief mailbox is
never touched (a stray event would wake the live chief).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from app.webapp.chief_doorbell import count_pending
from src import chief_session_end
from src.chief import reconcile
from src.chief_session_end import publish_session_ends

REPO = Path(__file__).resolve().parents[3]
LANE = "lane-1"
PLAIN = "plain-1"
T0 = time.time() - 7200  # the turn's end, as the transcript stamps it: in the past, as a stamp written now follows it


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _line(kind: str, epoch: float, **data) -> str:
    return json.dumps({"type": kind, "timestamp": _iso(epoch), "data": data})


class Lane:
    """A registry, an inbox, one hook state row per session and a transcript per session."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.registry = root / "chief-managed.json"
        self.inbox = root / "chief-inbox"
        self.rows: dict = {}
        self.registry.write_text(json.dumps({LANE: {"repo": "demo", "number": 7, "dispatch_id": "d" * 32}}),
                                 encoding="utf-8")

    def transcript(self, sid: str, *lines: str) -> Path:
        path = self.root / f"{sid}.jsonl"
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        self.rows[f"copilot-{sid}"] = {"launcher_session_id": sid, "transcript_path": str(path),
                                       "agent": "copilot", "status": "needs-you",
                                       "updated_at": "2026-11-22T10:00:00Z"}
        return path

    def run(self, now: float) -> list:
        return reconcile.reconcile(registry=self.registry, inbox=self.inbox, state_rows=self.rows, now=now)

    def events(self) -> list:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.inbox.glob("*.json"))]

    def stamp(self, kind: str, sid: str, epoch: float) -> None:
        self.inbox.mkdir(exist_ok=True)
        path = chief_session_end.stamp_path(self.inbox, kind, sid)
        path.write_text("", encoding="utf-8")
        os.utime(path, (epoch, epoch))


@pytest.fixture
def lane(tmp_path: Path) -> Lane:
    return Lane(tmp_path)


def _finished_turn(at: float = T0) -> list:
    return [_line("user.message", at - 30, content="/issue-start 7"),
            _line("assistant.turn_start", at - 29),
            _line("assistant.message", at - 1, content="Merged; main is clean."),
            _line("session.task_complete", at, summary="done", success=True),
            _line("assistant.turn_end", at)]


def test_a_terminal_lane_whose_hook_was_missed_gets_one_turn_end(lane: Lane) -> None:
    lane.transcript(LANE, *_finished_turn())
    assert lane.run(now=T0 + 60) == [], "inside the grace window the hook may still land"
    assert len(lane.run(now=T0 + 600)) == 1
    assert lane.run(now=T0 + 1200) == [], "deduplicated across ticks"
    [event] = lane.events()
    assert {k: event[k] for k in ("v", "kind", "event", "launcher_sid", "repo", "number", "dispatch_id",
                                   "excerpt", "excerpt_source", "source")} == {
        "v": 2, "kind": "turn-end", "event": "Stop", "launcher_sid": LANE, "repo": "demo", "number": 7,
        "dispatch_id": "d" * 32, "excerpt": "Merged; main is clean.", "excerpt_source": "transcript",
        "source": "reconcile"}
    # The doorbell counts it once, so it wakes the chief once.
    assert count_pending(lane.inbox, int(time.time() * 1000)) == 1


def test_a_turn_end_the_hook_already_published_is_not_repeated(lane: Lane) -> None:
    lane.transcript(LANE, *_finished_turn())
    lane.stamp("turn-end", LANE, T0 + 0.05)
    assert lane.run(now=T0 + 600) == []


def test_a_real_later_turn_end_the_debounce_swallowed_is_published(lane: Lane) -> None:
    # The hook published turn 1 and debounced turn 2 inside its 45 s window, so its stamp
    # still carries turn 1's time.
    lane.transcript(LANE, *_finished_turn(T0), *_finished_turn(T0 + 40))
    lane.stamp("turn-end", LANE, T0 + 0.05)
    assert len(lane.run(now=T0 + 600)) == 1
    assert lane.events()[0]["kind"] == "turn-end"


def test_an_active_lane_never_gets_a_false_turn_end(lane: Lane) -> None:
    lane.transcript(LANE, *_finished_turn(), _line("user.message", T0 + 5, content="next"),
                    _line("assistant.turn_start", T0 + 6))
    assert lane.run(now=T0 + 3600) == []
    assert not lane.inbox.exists()


def test_a_shutdown_lane_gets_one_session_end_shared_with_hook_and_reaper(lane: Lane) -> None:
    lane.transcript(LANE, *_finished_turn(), _line("session.shutdown", T0 + 10))
    lane.stamp("turn-end", LANE, T0 + 0.05)
    assert len(lane.run(now=T0 + 600)) == 1
    # The reaper sees the same exit afterwards and stays quiet.
    reaped = mock.Mock(session_id=LANE, agent="copilot")
    assert publish_session_ends([reaped], registry=lane.registry, inbox=lane.inbox) == []
    assert [e["kind"] for e in lane.events()] == ["session-end"]


def test_plain_sessions_leave_the_inbox_empty(lane: Lane) -> None:
    lane.transcript(PLAIN, *_finished_turn(), _line("session.shutdown", T0 + 10))
    assert lane.run(now=T0 + 600) == []
    assert not lane.inbox.exists()


def test_an_unreadable_registry_or_transcript_publishes_nothing(lane: Lane) -> None:
    path = lane.transcript(LANE, *_finished_turn())
    path.unlink()
    assert lane.run(now=T0 + 600) == []
    lane.transcript(LANE, *_finished_turn())
    lane.registry.write_text("{not json", encoding="utf-8")
    assert lane.run(now=T0 + 600) == []
    assert not lane.inbox.exists()


def test_the_hook_writes_the_stamps_the_reconciler_reads(lane: Lane, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook tree keeps its own copy of the stamp names; a drift would double every event."""
    monkeypatch.syspath_prepend(str(REPO / "copilot" / "hooks"))
    sys.modules.pop("chief_inbox", None)
    import chief_inbox

    monkeypatch.setenv("COPILOT_HOOKS_STATE_DIR", str(lane.root))
    monkeypatch.setenv("APP_LAUNCHER_SESSION_ID", LANE)
    assert chief_inbox.record({}, "sessionEnd") is not None
    assert chief_inbox.record({}, "sessionEnd") is None, "a repeated hook is deduplicated"
    lane.transcript(LANE, *_finished_turn(), _line("session.shutdown", T0 + 10))
    assert len(lane.run(now=T0 + 600)) == 1, "only the turn end; the session end was stamped"
    assert sorted(e["kind"] for e in lane.events()) == ["session-end", "turn-end"]
