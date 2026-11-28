"""Per-step diffs in the session Chat (#1349).

An ``edit`` / ``create`` / ``apply_patch`` step's ``action`` carries its diff,
worked out from the tool's own input (no line numbers — never invented). A
page trims each step and the whole diff is fetched on demand through
``/transcript/diff``.

Every path, id and line of content here is synthetic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import board
from src.session_view.transcript import (
    DIFF_INLINE_LINES,
    entry_diff,
    transcript_page,
    transcript_tail,
)
from src.transcript_flavors import _shared as tf_shared
from src.transcript_flavors._shared import diff_counts
from tests.unit.transcript._support import (
    _assistant,
    _tool_result,
    _tool_use,
    _user,
    _write_jsonl,
)


def _calls(path: Path) -> List[Dict[str, Any]]:
    return [e for e in transcript_page(path)["entries"] if e["kind"] == "tool_call"]


# -------------------------------------------------------- from the tool input


def test_edit_input_diff_has_prefixed_lines_and_no_line_numbers():
    act = tf_shared._tool_action("edit", {
        "path": "src/a.py", "old_str": "keep\nold\nkeep2", "new_str": "keep\nnew\nkeep2",
    })
    diff = act["diff"]
    assert diff["numbered"] is False and diff["truncated"] is False
    assert diff["hunks"] == [{"old_start": None, "new_start": None,
                              "lines": [" keep", "-old", "+new", " keep2"]}]
    assert diff_counts(diff["hunks"]) == (act["added"], act["removed"]) == (1, 1)


def test_an_edits_list_is_one_combined_diff_not_a_generic_row():
    act = tf_shared._tool_action("edit", {"path": "b.md", "edits": [
        {"old_str": "one", "new_str": "one\ntwo"},
        {"old_str": "x\ny", "new_str": "x"},
    ]})
    assert act["verb"] == "edited" and act["path"] == "b.md"
    assert (act["added"], act["removed"]) == (1, 1)
    assert [h["lines"] for h in act["diff"]["hunks"]] == [[" one", "+two"], [" x", "-y"]]


def test_create_input_is_all_added():
    act = tf_shared._tool_action("create", {"path": "n.txt", "file_text": "a\nb"})
    assert act["diff"]["hunks"] == [{"old_start": None, "new_start": None, "lines": ["+a", "+b"]}]
    assert act["diff"]["numbered"] is False


def test_the_tools_edit_and_create_keys_are_read():
    """Copilot's ``edit`` takes ``old_str``/``new_str`` (or ``oldText``/
    ``newText``); ``create`` takes ``path`` and ``file_text``. Each set was read
    from a real call or the harness's own tool schema."""
    act = tf_shared._tool_action
    cop_edit = act("edit", {"path": "c", "old_str": "beta", "new_str": "gamma"})
    assert (cop_edit["path"], cop_edit["added"], cop_edit["removed"]) == ("c", 1, 1)
    assert act("edit", {"path": "c", "oldText": "a", "newText": "b"})["added"] == 1
    assert act("create", {"path": "n", "file_text": "hi\nthere"})["added"] == 2
    assert act("create", {"path": "n"}) is None and act("edit", {"path": "a"}) is None


def test_an_edit_that_changes_no_line_gets_no_diff():
    act = tf_shared._tool_action("edit", {"path": "a.py", "old_str": "same", "new_str": "same"})
    assert "diff" not in act and (act["added"], act["removed"]) == (0, 0)


def test_full_cap_marks_truncation():
    body = "\n".join("line %d" % i for i in range(50_000))
    act = tf_shared._tool_action("create", {"path": "big.txt", "file_text": body})
    assert act["diff"]["truncated"] is True
    kept = sum(len(line) + 1 for h in act["diff"]["hunks"] for line in h["lines"])
    assert kept <= tf_shared.DIFF_FULL_BYTES
    assert act["added"] == 50_000   # the count stays whole; only the text is capped


def test_a_failed_edit_keeps_todays_row_with_no_diff(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("go"),
        _tool_use("edit", {"path": "a.py", "old_str": "zz", "new_str": "yy"}, "t1"),
        _tool_result("t1", "String to replace not found in file.", ok=False),
        _assistant("done"),
    ])
    (call,) = _calls(path)
    assert call["error"] is True
    assert "diff" not in call["action"]
    assert call["action"]["verb"] == "edited"


def test_steps_with_no_edit_data_look_as_they_did(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("go"),
        _tool_use("rg", {"pattern": "x"}, "t1"),
        _tool_result("t1", "a.py:1:x"),
        _tool_use("powershell", {"command": "ls"}, "t2"),
        _tool_result("t2", "a.py"),
        _assistant("done"),
    ])
    grep, shell = _calls(path)
    assert "action" not in grep
    assert "diff" not in shell["action"]


# ------------------------------------------------- page caps and full fetch


def _big_edit_transcript(tmp_path: Path, lines: int = 300) -> Path:
    body = "\n".join("row %d" % i for i in range(lines))
    return _write_jsonl(tmp_path / "t.jsonl", [
        _user("go"),
        _tool_use("edit", {"path": "a.py", "old_str": "", "new_str": body}, "t1"),
        _tool_result("t1", "ok"),
        _assistant("done"),
    ])


def test_a_page_trims_each_step_and_the_full_diff_is_one_request_away(tmp_path):
    path = _big_edit_transcript(tmp_path)
    (call,) = _calls(path)
    inline = call["action"]["diff"]
    assert inline["truncated"] is True
    assert sum(len(h["lines"]) for h in inline["hunks"]) == DIFF_INLINE_LINES
    assert "offset" not in call   # a folded entry's own offset stays unexposed
    full = entry_diff(path, inline["offset"], inline["n"], "copilot")
    assert full["path"] == "a.py"
    assert full["diff"]["truncated"] is False
    assert sum(len(h["lines"]) for h in full["diff"]["hunks"]) == 300


def test_entry_diff_refuses_a_bad_ref(tmp_path):
    path = _big_edit_transcript(tmp_path)
    (call,) = _calls(path)
    ref = call["action"]["diff"]
    assert entry_diff(path, ref["offset"], 5, "copilot") is None
    assert entry_diff(path, ref["offset"] + 1, 0, "copilot") is None
    assert entry_diff(path, 10**9, 0, "copilot") is None


def test_the_live_tail_trims_too(tmp_path):
    path = _big_edit_transcript(tmp_path)
    out = transcript_tail(path, after=0)
    calls = [e for e in out["entries"] + out["pending"] if e["kind"] == "tool_call"]
    assert calls and calls[0]["action"]["diff"]["truncated"] is True
    assert calls[0]["action"]["diff"]["n"] == 0


# ------------------------------------------------------------------ route


@pytest.fixture
def _bypass_gate(monkeypatch):
    from src import middleware
    monkeypatch.setattr(
        middleware.trust, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def _serve(overrides, monkeypatch, path: Path) -> None:
    overrides["session"].list_sessions.return_value = [{
        "session_id": "s1", "kind": "pty", "agent": "copilot", "alive": True,
        "project_dir": "E:/stub/proj", "started_at": 1_789_000_000,
    }]
    monkeypatch.setattr(board, "state_row_for_session",
                        lambda live, rows, sid: {"transcript_path": str(path)})


def test_transcript_diff_path_classified_passkey():
    """Its own guard row: '.../transcript/diff' ends with neither
    '/transcript' nor '/transcript/entry', so no sibling rule covers it."""
    from src.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript/diff") == "passkey"


def test_transcript_diff_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/claude-code/sessions/s1/transcript/diff?offset=0").status_code == 403


class TestTranscriptDiffEndpoint:

    def test_returns_the_whole_diff_and_never_logs_content(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog
    ):
        client, _, overrides = webapp_client
        path = _big_edit_transcript(tmp_path)
        _serve(overrides, monkeypatch, path)
        page = client.get("/api/claude-code/sessions/s1/transcript").json()
        (call,) = [e for e in page["entries"] if e["kind"] == "tool_call"]
        ref = call["action"]["diff"]
        with caplog.at_level(logging.INFO):
            body = client.get(
                f"/api/claude-code/sessions/s1/transcript/diff?offset={ref['offset']}&n={ref['n']}"
            ).json()
        assert body["available"] is True and body["path"] == "a.py"
        assert sum(len(h["lines"]) for h in body["diff"]["hunks"]) == 300
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "row 1" not in joined and "transcript diff s1" in joined

    def test_a_bad_ref_is_diff_not_found(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        _serve(overrides, monkeypatch, _big_edit_transcript(tmp_path))
        body = client.get("/api/claude-code/sessions/s1/transcript/diff?offset=0&n=0").json()
        assert body["available"] is False and body["reason"] == "diff_not_found"

    def test_offset_is_required(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        assert client.get("/api/claude-code/sessions/s1/transcript/diff").status_code == 422

    def test_unknown_session_shares_the_page_routes_reasons(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        body = client.get("/api/claude-code/sessions/s1/transcript/diff?offset=0").json()
        assert body["available"] is False and body["reason"] == "session_not_found"
