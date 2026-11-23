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
    assert lane.run(now=T0 + 10) == [], "inside the grace window the hook may still land"
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


def _copilot_turn(start: float, end: float, reply: str, prompt: str) -> list:
    """An interactive Copilot turn as its transcript records it: no ``session.task_complete``,
    only the turn end and the ``agentStop`` hook Copilot logs as it runs it."""
    return [_line("user.message", start, content=prompt),
            _line("assistant.turn_start", start + 1),
            _line("assistant.message", end - 1, content=reply),
            _line("assistant.turn_end", end),
            _line("hook.start", end, hookType="agentStop"),
            _line("hook.end", end + 1, hookType="agentStop")]


async def test_a_blocked_turn_end_inside_the_debounce_reaches_an_idle_chief(
    lane: Lane, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane's turn ends, the chief steers it on, and it ends again blocked 20 s later: the hook's
    45 s debounce drops that second end, and nothing else may be left to report it."""
    from types import SimpleNamespace

    from app.webapp import chief_doorbell as bell

    monkeypatch.syspath_prepend(str(REPO / "copilot" / "hooks"))
    sys.modules.pop("chief_inbox", None)
    import chief_inbox

    monkeypatch.setenv("COPILOT_HOOKS_STATE_DIR", str(lane.root))
    monkeypatch.setenv("APP_LAUNCHER_SESSION_ID", LANE)
    first = time.time()
    second = first + 20
    blocked = "Item blocked: browser-gate retry budget exhausted. Watermark unchanged; stopping."
    lines = _copilot_turn(first - 60, first, "Gate red; retrying.", "/upstream-sync")
    lines += _copilot_turn(first + 5, second, blocked, "go on")
    transcript = lane.transcript(LANE, *lines)
    payload = {"transcript_path": str(transcript)}
    assert chief_inbox.record(payload, "agentStop") is not None, "the first turn end is published"
    for acked in lane.inbox.glob("*.json"):  # the chief read and acked it
        acked.unlink()
    monkeypatch.setattr(chief_inbox.time, "time", lambda: second)
    assert chief_inbox.record(payload, "agentStop") is None, "the hook's debounce drops the blocked end"

    bound = reconcile.GRACE_SECONDS + reconcile.TICK_SECONDS
    assert len(lane.run(now=second + bound)) == 1, f"the reconciler reports it within {bound:g}s"
    [event] = lane.events()
    assert (event["kind"], event["excerpt"]) == ("turn-end", blocked)

    async def idle_chief(cfg):
        scan = bell.scan_pending(Path(cfg.chief_inbox_dir), int(time.time() * 1000))
        return bell.Observation(pending=scan.count, chief_sid="chief", status="idle-finished", dirty=False)

    sends = []
    cfg = SimpleNamespace(chief_doorbell=True, chief_inbox_dir=str(lane.inbox), session_host_port=1)
    doorbell, skips, clock = bell.Doorbell(), bell._SkipLog(), [0.0]
    decisions = []
    for _ in range(2):
        decisions.append(await bell.tick_once(
            cfg, doorbell, skips, now=lambda: clock[0], observe_fn=idle_chief,
            send_fn=lambda *a: sends.append(a) or {"delivered": True, "submit_state": "confirmed"},
            record_fn=lambda *a, **k: True))
        clock[0] += bell.TICK_SECONDS
    assert decisions == ["debouncing", "ring"] and len(sends) == 1


def test_an_autopilot_stop_followed_by_its_continuation_is_active(lane: Lane) -> None:
    lane.transcript(LANE, *_copilot_turn(T0 - 60, T0, "Working.", "/issue-start 7"),
                    _line("user.message", T0 + 3, content="", source="autopilot"),
                    _line("assistant.turn_start", T0 + 4))
    assert lane.run(now=T0 + 3600) == []


def test_a_second_agent_stop_hook_line_does_not_repeat_a_published_turn_end(lane: Lane) -> None:
    lane.transcript(LANE, *_copilot_turn(T0 - 60, T0, "Done.", "/issue-start 7"),
                    _line("hook.start", T0 + 2, hookType="agentStop"))
    lane.stamp("turn-end", LANE, T0 + 0.5)
    assert lane.run(now=T0 + 600) == []


def test_a_finished_subagent_is_not_the_lane_s_turn_end(lane: Lane) -> None:
    # Two sub-agents run in parallel; the first ends and logs its own agentStop
    # (its hook input names the lane as parentAgentId) while the second keeps
    # the lane mid-turn with no further turn start for minutes.
    sub = {"sessionId": "call_a", "agentId": "call_a", "parentAgentId": "lane-copilot-sid"}
    lane.transcript(LANE, _line("user.message", T0 - 60, content="/upstream-sync"),
                    _line("assistant.turn_start", T0 - 59),
                    _line("tool.execution_start", T0 - 50, toolName="task", toolCallId="call_a"),
                    _line("tool.execution_start", T0 - 50, toolName="task", toolCallId="call_b"),
                    _line("user.message", T0 - 49, content="review", parentToolCallId="call_a"),
                    _line("assistant.turn_start", T0 - 48, parentToolCallId="call_a"),
                    _line("assistant.turn_end", T0, parentToolCallId="call_a"),
                    _line("hook.start", T0, hookType="agentStop", input=sub),
                    _line("tool.execution_complete", T0 + 1, toolCallId="call_a"))
    assert lane.run(now=T0 + 600) == []
    assert not lane.inbox.exists()


def _failed_turn(at: float = T0) -> list:
    """A turn whose model round closed with no reply, and no agentStop line after it."""
    return [_line("user.message", at - 30, content="/issue-start 7"),
            _line("assistant.turn_start", at - 29),
            _line("session.error", at - 1, errorType="query",
                  message="No response was returned. Send your message again to retry"),
            _line("assistant.turn_end", at)]


def test_a_failed_turn_its_hook_never_reported_reaches_the_chief_as_turn_failed(lane: Lane) -> None:
    lane.transcript(LANE, *_failed_turn())
    assert lane.run(now=T0 + 10) == [], "inside the grace window the hook may still land"
    assert len(lane.run(now=T0 + 600)) == 1
    assert lane.run(now=T0 + 1200) == [], "deduplicated across ticks through the turn-end stamp"
    [event] = lane.events()
    assert (event["kind"], event["event"], event["source"]) == ("turn-failed", "Stop", "reconcile")
    assert event["error"].startswith("No response was returned")


def test_a_failed_turn_the_hook_published_is_not_repeated(lane: Lane) -> None:
    lane.transcript(LANE, *_failed_turn())
    lane.stamp("turn-end", LANE, T0 + 0.05)
    assert lane.run(now=T0 + 600) == []


def test_a_round_still_thinking_is_not_a_failed_turn(lane: Lane) -> None:
    lane.transcript(LANE, _line("user.message", T0 - 30, content="go"), _line("assistant.turn_start", T0 - 29))
    assert lane.run(now=T0 + 3600) == []


def test_a_user_exit_mid_round_is_a_session_end_not_a_failed_turn(lane: Lane) -> None:
    lane.transcript(LANE, *_failed_turn(), _line("session.shutdown", T0 + 1))
    lane.run(now=T0 + 600)
    assert [e["kind"] for e in lane.events()] == ["session-end"]
