"""Chat step labels (#1532): a Copilot ``tool_call`` carries ``label``.

A call that records its own one-line description (Copilot's ``powershell``
``description``) labels the step with it; one that records none falls back to
the call's one-line ``summary``. The fallback is the shared builder's, so no
call can ship without a label. The strip's live line reads the description
too, while the call is in flight.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from src.session_view import transcript as st
from tests.unit.transcript._support import _tool_use, _user, _write_jsonl


def _lines(rows: List[Dict[str, Any]]) -> List[Any]:
    return [(i * 100, json.dumps(row)) for i, row in enumerate(rows)]


def _calls(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in entries if e["kind"] == "tool_call"]


def test_copilot_steps_read_by_their_descriptions():
    entries = st.copilot_entries(_lines([
        _tool_use("powershell", {"command": "git status", "description": "Check the tree"}, "c1"),
        _tool_use("powershell", {"command": "Get-ChildItem", "description": "  List   the\nfolder "}, "c2"),
        _tool_use("view", {"path": "E:/x/a.py"}, "c3"),
        _tool_use("powershell", {"command": "ls", "description": ""}, "c4"),
    ]))
    calls = _calls(entries)
    assert [c["label"] for c in calls] == ["Check the tree", "List the folder", "E:/x/a.py", "ls"]
    # The summary is untouched: it still names the command.
    assert calls[0]["summary"] == "git status"


def test_a_call_with_no_description_falls_back_to_its_summary():
    entries = st.copilot_entries(_lines([_tool_use("bash", {"command": "find docs"}, "p1")]))
    (call,) = _calls(entries)
    assert call["label"] == call["summary"] == "find docs"


def test_activity_line_names_the_running_step_by_its_description(tmp_path: Path):
    path = _write_jsonl(tmp_path / "c.jsonl", [
        _user("run the tests", "2026-10-12T10:00:00.000Z"),
        _tool_use("powershell", {"command": "pytest", "description": "Run the unit tests"}, "t1"),
    ])
    got = st.transcript_activity(path, now=path.stat().st_mtime)
    assert got["last"] == "Run the unit tests"
