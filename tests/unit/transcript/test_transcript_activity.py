"""Coding-tab transcript: tool actions and the running-step activity label."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import board
from src.session_view import transcript as st
from src.transcript_flavors import _shared as tf_shared
from tests.unit.transcript._support import (
    _assistant,
    _live,
    _parse_whole,
    _thinking,
    _tool_result,
    _tool_use,
    _user,
    _write_jsonl,
)

# ------------------------------------------------ plain-words actions (#1266)


def test_tool_action_covers_the_probed_copilot_tools():
    """The command, edit, create and view tools probed from real Copilot
    transcripts (key names only) read in plain words; +N -M is a line diff of
    the edit's own old and new text."""
    act = tf_shared._tool_action
    # Commands keep their line breaks: Chat shows the first line folded and
    # the whole command in the opened block.
    assert act("powershell", {"command": "cd x\npytest -q"}) == {"verb": "ran", "command": "cd x\npytest -q"}
    assert act("bash", {"command": "ls"}) == {"verb": "ran", "command": "ls"}

    # Edits and writes also carry their diff (#1349): tests/unit/transcript/test_transcript_diffs.py.
    def plain(action):
        return {k: v for k, v in action.items() if k != "diff"}

    assert plain(act("edit", {"path": "a/b.py", "old_str": "x\ny", "new_str": "x\nz\nw"})) == {
        "verb": "edited", "path": "a/b.py", "added": 2, "removed": 1}
    assert plain(act("edit", {"path": "c.md", "edits": [
        {"oldText": "a", "newText": "a\nb"}, {"oldText": "q\nr", "newText": ""}]})) == {
        "verb": "edited", "path": "c.md", "added": 1, "removed": 2}
    assert plain(act("create", {"path": "n.txt", "file_text": "1\n2\n3"})) == {
        "verb": "wrote", "path": "n.txt", "added": 3, "removed": 0}
    assert act("view", {"path": "f.py"}) == {"verb": "read", "path": "f.py"}
    # Unknown tools and inputs missing their key keep today's rendering.
    assert act("rg", {"pattern": "x"}) is None
    assert act("edit", {"path": "a.py"}) is None
    assert act("powershell", {"command": "   "}) is None


# ------------------------------------------------ live activity line (#1387)

_NOW_TS = "2026-09-14T11:00:00.000Z"


def _turn(*rows: Dict[str, Any]) -> List[Dict[str, Any]]:
    """An earlier finished exchange with two tool calls, then ``rows``: the
    count must restart at the last user message."""
    return [
        _user("earlier"),
        _tool_use("powershell", {"command": "ls"}, "e1"),
        _tool_use("view", {"path": "a"}, "e2"),
        _tool_result("e1", "x"),
        _tool_result("e2", "y"),
        _assistant("done"),
        _user("now do the next thing", _NOW_TS),
        *rows,
    ]


@pytest.mark.parametrize("call, label", [
    (_tool_use("powershell", {"command": "pytest"}, "t1"), "Running powershell"),
    (_tool_use("edit", {"path": "a", "old_str": "x", "new_str": "y"}, "t1"), "Editing a file"),
    (_tool_use("create", {"path": "a", "file_text": "x"}, "t1"), "Editing a file"),
    (_tool_use("view", {"path": "a"}, "t1"), "Reading a file"),
    (_tool_use("web_fetch", {"url": "https://example.com"}, "t1"), "web_fetch"),
])
def test_activity_labels_the_running_step(tmp_path: Path, call, label):
    path = _write_jsonl(tmp_path / "c.jsonl", _turn(call))
    got = st.transcript_activity(path, now=path.stat().st_mtime)
    assert got == {"since": _NOW_TS, "actions": 1, "last": label, "working": True}


def test_activity_counts_only_the_turn_in_progress(tmp_path: Path):
    path = _write_jsonl(tmp_path / "c.jsonl", _turn(
        _tool_use("powershell", {"command": "a"}, "t1"),
        _tool_use("powershell", {"command": "b"}, "t2"),
        _tool_result("t1", "ok"),
        _thinking("hmm"),
    ))
    got = st.transcript_activity(path, now=path.stat().st_mtime)
    assert (got["actions"], got["last"], got["working"]) == (2, "Thinking", True)


def test_activity_is_thinking_with_no_actions_right_after_a_prompt(tmp_path: Path):
    path = _write_jsonl(tmp_path / "c.jsonl", _turn())
    got = st.transcript_activity(path, now=path.stat().st_mtime)
    assert (got["actions"], got["last"], got["working"]) == (0, "Thinking", True)


def test_activity_without_tool_data_carries_the_time_alone(tmp_path: Path, monkeypatch):
    """A harness that records no tool calls reports `actions: None` — "0
    actions" would be a guess."""
    path = _write_jsonl(tmp_path / "c.jsonl", _turn(_tool_use("powershell", {"command": "a"}, "t1")))
    entries = _parse_whole(path)
    assert st.turn_activity(entries, tool_calls=False)["actions"] is None
    monkeypatch.setattr(st, "_NO_TOOL_CALL_FLAVORS", frozenset({"copilot"}))
    got = st.transcript_activity(path, now=path.stat().st_mtime)
    assert got["actions"] is None and got["since"] == _NOW_TS


def test_activity_working_needs_an_open_fresh_turn(tmp_path: Path):
    open_turn = _write_jsonl(tmp_path / "open.jsonl", _turn(_tool_use("powershell", {"command": "a"}, "t1")))
    stale = open_turn.stat().st_mtime + st.ACTIVITY_FRESH_S + 1
    assert st.transcript_activity(open_turn, now=stale)["working"] is False   # interrupted: never written again

    finished = _write_jsonl(tmp_path / "done.jsonl", _turn(_assistant("All done.")))
    assert st.transcript_activity(finished, now=finished.stat().st_mtime)["working"] is False


def test_activity_is_none_when_no_user_message_is_in_reach(tmp_path: Path):
    path = _write_jsonl(tmp_path / "c.jsonl", [_assistant("hello")])
    assert st.transcript_activity(path) is None
    assert st.transcript_activity(tmp_path / "missing.jsonl") is None


def test_activity_reads_the_file_only_when_it_changed(tmp_path: Path, monkeypatch):
    """An idle chat view ticks every 3 s: an unchanged file must cost a stat,
    not a page read (#1050's cost model)."""
    path = _write_jsonl(tmp_path / "c.jsonl", _turn(_tool_use("powershell", {"command": "a"}, "t1")))
    reads = []
    real = st._page
    monkeypatch.setattr(st, "_page", lambda *a, **k: (reads.append(1), real(*a, **k))[1])
    st.transcript_activity(path)
    st.transcript_activity(path)
    assert len(reads) == 1
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_tool_result("t1", "ok")) + "\n")
    assert st.transcript_activity(path)["last"] == "Thinking"
    assert len(reads) == 2


class TestTranscriptActivityEndpoint:
    """#1387: the chat strip's activity line rides the existing page and tail
    responses — no route or poll of its own."""

    def _serve(self, webapp_client, monkeypatch, path: Path, **live: Any):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(**live)]
        monkeypatch.setattr(
            board, "state_row_for_session",
            lambda live_rows, rows, sid: {"transcript_path": str(path)},
        )
        return client

    def test_the_newest_page_and_every_tail_tick_carry_it(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        path = _write_jsonl(tmp_path / "t.jsonl", _turn(_tool_use("powershell", {"command": "pytest"}, "t1")))
        client = self._serve(webapp_client, monkeypatch, path)
        url = "/api/claude-code/sessions/s1/transcript"

        page = client.get(url).json()
        assert page["activity"]["last"] == "Running powershell" and page["activity"]["actions"] == 1
        assert page["activity"]["since"] == _NOW_TS
        # An older page is not the chat's first read and carries none.
        older = client.get(f"{url}?before={path.stat().st_size}").json()
        assert older["available"] is True and "activity" not in older

        unchanged = client.get(f"{url}?after={page['tail']}&size={page['size']}").json()
        assert unchanged["changed"] is False
        assert unchanged["activity"]["last"] == "Running powershell"
