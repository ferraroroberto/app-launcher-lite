"""A Copilot lane whose sub-agent ends is still working, not "needs you".

Copilot CLI runs a sub-agent (a ``task`` call, autopilot's completion review)
through the same hook config as its parent, so the sub-agent's own
``userPromptSubmitted`` and ``agentStop`` reach ``session_state.py`` with
``sessionId`` set to the sub-agent's tool-call id and ``parentAgentId`` set to
the lane's session. The lane's ``APP_LAUNCHER_SESSION_ID`` is inherited, so a
row written for the sub-agent claims the lane's card the moment it is the
newest. These tests drive the real hook script with the payload shapes a
Copilot event log records, then build the Board columns from what it wrote.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.board import build_board
from src.board.sessions import merge_sessions
from src.board.state import iso_z, read_sessions_state
from src.subprocess_flags import NO_WINDOW

_HOOK = Path(__file__).resolve().parents[3] / "copilot" / "hooks" / "session_state.py"
_HOOK_TIMEOUT_S = 30

_LANE_SID = "3723f142-0000-4000-8000-000000000001"
_SUBAGENT_SID = "call_subagent0001"
_LAUNCHER_SID = "launcher-lane-1"


def _run_hook(event: str, payload: Dict[str, Any], state_dir: Path) -> None:
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "COPILOT_HOOKS_STATE_DIR": str(state_dir),
        "APP_LAUNCHER_SESSION_ID": _LAUNCHER_SID,
        "APP_LAUNCHER_AGENT": "copilot",
    }
    proc = subprocess.run(
        [sys.executable, "-B", str(_HOOK), event],
        input=json.dumps(payload).encode("utf-8"),
        env=env,
        capture_output=True,
        timeout=_HOOK_TIMEOUT_S,
        creationflags=NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def _lane(event_extra: Dict[str, Any], cwd: Path) -> Dict[str, Any]:
    return {"sessionId": _LANE_SID, "timestamp": 1795077476815, "cwd": str(cwd), **event_extra}


def _subagent(event_extra: Dict[str, Any], cwd: Path) -> Dict[str, Any]:
    return {
        "sessionId": _SUBAGENT_SID,
        "timestamp": 1795077494032,
        "agentId": _SUBAGENT_SID,
        "agentType": "general-purpose",
        "parentAgentId": _LANE_SID,
        "cwd": str(cwd),
        **event_extra,
    }


def _backdate_rows(state_dir: Path, minutes: int) -> None:
    """Move every row's stamp back, as if the lane's prompt landed long before."""
    path = state_dir / "sessions-state.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    stamp = iso_z(datetime.now(timezone.utc) - timedelta(minutes=minutes))
    for row in rows.values():
        row["updated_at"] = stamp
    path.write_text(json.dumps(rows), encoding="utf-8")


def _live(cwd: Path) -> List[Dict[str, Any]]:
    return [{
        "session_id": _LAUNCHER_SID,
        "agent": "copilot",
        "kind": "pty",
        "alive": True,
        "project_dir": str(cwd),
        "started_at": iso_z(datetime.now(timezone.utc) - timedelta(hours=1)),
        "live_title": "GitHub Copilot",
        "last_output_at": None,
    }]


def _your_turn(state_dir: Path, cwd: Path) -> List[Dict[str, Any]]:
    """The lane's Your turn column, built the way ``GET /api/board`` builds it."""
    rows = read_sessions_state(state_dir / "sessions-state.json")["rows"]
    cards = merge_sessions(_live(cwd), rows)
    return build_board(cards, {}, [])["your_turn"]


@pytest.fixture()
def lane(tmp_path: Path) -> Dict[str, Path]:
    cwd = tmp_path / "some-repo"
    cwd.mkdir()
    transcript = tmp_path / "events.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant.turn_start", "data": {}, "timestamp": "2026-11-19T08:37:59Z"}) + "\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return {"cwd": cwd, "transcript": transcript, "state": state_dir}


def test_a_finished_subagent_leaves_its_working_lane_off_your_turn(lane: Dict[str, Path]) -> None:
    cwd, state = lane["cwd"], lane["state"]
    _run_hook("userPromptSubmitted", _lane({"prompt": "go"}, cwd), state)
    _backdate_rows(state, minutes=30)
    _run_hook("userPromptSubmitted", _subagent({"prompt": "review the change"}, cwd), state)
    _run_hook("agentStop", _subagent(
        {"transcriptPath": str(lane["transcript"]), "stopReason": "end_turn"}, cwd), state)

    rows = json.loads((state / "sessions-state.json").read_text(encoding="utf-8"))
    assert set(rows) == {_LANE_SID}
    assert rows[_LANE_SID]["status"] == "working"
    assert _your_turn(state, cwd) == []


def test_the_lane_s_own_turn_end_is_the_card_s_turn_end(lane: Dict[str, Path]) -> None:
    """The lane's own ``agentStop`` claims the card; its transcript (an open round, no
    background work) is no failure and nothing to wait on, so the card reads its turn ended."""
    cwd, state = lane["cwd"], lane["state"]
    _run_hook("userPromptSubmitted", _lane({"prompt": "go"}, cwd), state)
    _run_hook("userPromptSubmitted", _subagent({"prompt": "review the change"}, cwd), state)
    _run_hook("agentStop", _subagent(
        {"transcriptPath": str(lane["transcript"]), "stopReason": "end_turn"}, cwd), state)
    _run_hook("agentStop", _lane(
        {"transcriptPath": str(lane["transcript"]), "stopReason": "end_turn"}, cwd), state)

    rows = read_sessions_state(state / "sessions-state.json")["rows"]
    assert rows[_LANE_SID]["status"] == "needs-you"
    cards = [c for c in merge_sessions(_live(cwd), rows) if c["session_id"] == _LAUNCHER_SID]
    assert [(c["state_sid"], c["status"]) for c in cards] == [(_LANE_SID, "idle-finished")]


pytestmark = pytest.mark.timeout(4 * _HOOK_TIMEOUT_S + 30)
