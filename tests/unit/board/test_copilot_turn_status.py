"""A Copilot turn end reads what the session is really doing, not "needs you".

The fixture ``tests/fixtures/copilot_turn_background.jsonl`` is a live lane's
``events.jsonl`` reduced to the fields the reader looks at: the lane started a
PowerShell sleep in background mode and ended its turn while it ran. The other
cases append the events Copilot writes next (the shell's completion notice) or
replace the last round with a failed one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.board import card_status as cs
from src.board import sessions as board_sessions
from src.board.copilot_status import copilot_waiting_status
from src.transcript_flavors.copilot_turn import read_turn_end
from tests.unit.board._support import NOW

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "copilot_turn_background.jsonl"

_SHELL_DONE = {
    "type": "system.notification",
    "data": {"content": "<system_notification>shell done</system_notification>",
             "kind": {"type": "shell_completed", "shellId": "0", "exitCode": 0}},
    "timestamp": "2026-11-23T20:47:27.000Z",
}


def _recorded() -> List[Dict[str, Any]]:
    return [json.loads(line) for line in _FIXTURE.read_text(encoding="utf-8").splitlines()]


def _write(tmp_path: Path, events: List[Dict[str, Any]]) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def _failed_round(error: str = "") -> List[Dict[str, Any]]:
    """The recorded turn with its last round replaced by one that wrote no reply."""
    events = _recorded()[:-4]
    events.append({"type": "assistant.turn_start", "data": {"turnId": "1"}, "timestamp": "2026-11-23T20:44:56.260Z"})
    if error:
        events.append({"type": "session.error", "data": {"errorType": "query", "message": error},
                       "timestamp": "2026-11-23T20:44:58.000Z"})
    events.append({"type": "assistant.turn_end", "data": {"turnId": "1"}, "timestamp": "2026-11-23T20:44:58.100Z"})
    events.append({"type": "hook.start", "data": {"hookType": "agentStop", "input": {}},
                   "timestamp": "2026-11-23T20:44:58.200Z"})
    return events


def test_a_turn_end_with_its_background_shell_running_is_waiting_on_task(tmp_path: Path) -> None:
    path = _write(tmp_path, _recorded())
    turn = read_turn_end(path)
    assert turn is not None
    assert (turn.background_pending, turn.failed) == (True, False)
    assert copilot_waiting_status(path) == cs.WAITING_ON_TASK
    assert cs.WAITING_ON_TASK not in cs.NEEDS_HUMAN


def test_the_shell_completion_notice_makes_it_a_clean_stop(tmp_path: Path) -> None:
    path = _write(tmp_path, _recorded() + [_SHELL_DONE])
    assert copilot_waiting_status(path) == cs.IDLE_FINISHED


def test_stopping_the_shell_ends_the_wait(tmp_path: Path) -> None:
    stop = {"type": "tool.execution_start",
            "data": {"toolCallId": "call_stop", "toolName": "stop_powershell", "arguments": {"shellId": "0"}}}
    done = {"type": "tool.execution_complete", "data": {"toolCallId": "call_stop", "success": True}}
    path = _write(tmp_path, _recorded() + [stop, done])
    assert copilot_waiting_status(path) == cs.IDLE_FINISHED


def test_a_detached_shell_is_never_awaited(tmp_path: Path) -> None:
    events = _recorded()
    events[4]["data"]["toolTelemetry"]["properties"]["detached"] = "true"
    assert copilot_waiting_status(_write(tmp_path, events)) == cs.IDLE_FINISHED


def test_a_round_that_wrote_no_reply_is_a_failed_turn(tmp_path: Path) -> None:
    path = _write(tmp_path, _failed_round("No response was returned. Send your message again to retry"))
    turn = read_turn_end(path)
    assert turn is not None and turn.failed is True
    assert turn.error and turn.error.startswith("No response was returned")
    assert turn.failed_at == "2026-11-23T20:44:58.100Z"
    assert copilot_waiting_status(path) == cs.TURN_FAILED
    assert cs.TURN_FAILED in cs.NEEDS_HUMAN


def test_an_aborted_round_is_not_a_failure(tmp_path: Path) -> None:
    events = _failed_round()
    events.insert(-2, {"type": "abort", "data": {"reason": "user_initiated"}})
    assert read_turn_end(_write(tmp_path, events)).failed is False


def test_a_round_still_open_is_a_model_thinking_not_a_failure(tmp_path: Path) -> None:
    events = _recorded()[:-4] + [{"type": "assistant.turn_start", "data": {"turnId": "1"}}]
    assert read_turn_end(_write(tmp_path, events)).failed is None


def test_a_pending_ask_user_is_a_decision(tmp_path: Path) -> None:
    ask = {"type": "tool.execution_start", "data": {"toolCallId": "call_ask", "toolName": "ask_user"}}
    path = _write(tmp_path, _recorded()[:-2] + [ask])
    assert copilot_waiting_status(path) == cs.AWAITING_DECISION


def test_an_unreadable_transcript_is_unknown_never_needs_you(tmp_path: Path) -> None:
    assert copilot_waiting_status(tmp_path / "missing.jsonl") == cs.UNKNOWN
    assert copilot_waiting_status(None) == cs.UNKNOWN


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        (_recorded, cs.WAITING_ON_TASK),
        (lambda: _recorded() + [_SHELL_DONE], cs.AWAITING_INPUT),
        (_failed_round, cs.TURN_FAILED),
    ],
)
def test_a_copilot_card_in_a_lane_repo_reads_by_its_transcript(tmp_path: Path, events, expected) -> None:
    """Through the Board's one derivation: a lane repo with an open issue still turns a clean
    stop into ``awaiting-input``, but a running shell or a failed turn keeps its own value."""
    path = _write(tmp_path, events())
    row = {"status": cs.NEEDS_YOU_STAMP, "agent": "copilot", "project": "lane-repo",
           "transcript_path": str(path), "updated_at": "2026-07-02T11:59:00Z"}
    status, _anchor = board_sessions.derive_card_status(
        row, None, now=NOW, project="lane-repo", active_repos=frozenset({"lane-repo"}),
    )
    assert status == expected
