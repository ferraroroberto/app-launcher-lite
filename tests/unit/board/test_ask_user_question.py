"""Chat's AskUserQuestion card (#1149): reading the call, gating an answer,
and turning a pick into the picker's keystrokes.

The keystroke sequences pinned here are the ones probed against a live
Claude Code 2.1.281 picker, in both a PTY and a detached console (evidence on
the issue): a digit jump-selects (and, for a single-select question, moves
on), Tab leaves a multi-select question, option ``n + 1`` opens the typed
answer, and a multi-part call ends on a review tab whose ``1`` submits. A
multi-select's typed answer (2.1.316) needs the cursor on its row: Ctrl+N
per option, the text, then Tab + Enter.
Every transcript here is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.ask_user_question import (
    answer_keystrokes,
    answers_from_result,
    questions_from_input,
)
from src.board import transcript as board_transcript


def _q(question: str, labels: List[str], multi: bool = False) -> Dict[str, Any]:
    return {
        "question": question, "header": question.split()[0], "multiSelect": multi,
        "options": [{"label": lab, "description": f"about {lab}"} for lab in labels],
    }


COLOUR = _q("Pick a colour", ["Red", "Green", "Blue"])
SIZE = _q("Pick a size", ["Small", "Large"])
FRUIT = _q("Pick fruits", ["Apple", "Banana", "Cherry"], multi=True)


# ------------------------------------------------------------- keystrokes


@pytest.mark.parametrize("questions,answers,keys", [
    # One single-select question: the digit alone answers it. An Enter after
    # it would land in the composer once the picker has closed.
    ([COLOUR], [{"option": 2}], [("2", False)]),
    # "Type something" is option n + 1, then the text, then Enter.
    ([COLOUR], [{"text": "Purple haze"}], [("4", False), ("Purple haze", True)]),
    # Two questions: a digit each, then the review tab's "Submit answers".
    ([COLOUR, SIZE], [{"option": 3}, {"option": 2}], [("3", False), ("2", False), ("1", False)]),
    # Multi-select: toggles, Tab to move on, then submit — in pick order
    # regardless of the order they were tapped.
    ([FRUIT], [{"options": [3, 1]}], [("1", False), ("3", False), ("\t", False), ("1", False)]),
    # The mixed call probed end to end through the route in both kinds.
    ([FRUIT, COLOUR], [{"options": [2, 3]}, {"text": "Teal blue"}],
     [("2", False), ("3", False), ("\t", False), ("4", False), ("Teal blue", True), ("1", False)]),
    # Multi-select typed text (2.1.316): Ctrl+N once per option walks the
    # cursor to "Type something", the text fills it, Tab + Enter moves on.
    ([FRUIT], [{"text": "Teal blue"}],
     [("\x0e", False)] * 3 + [("Teal blue", False), ("\t", True), ("1", False)]),
    ([FRUIT], [{"options": [3, 1], "text": "Teal blue"}],
     [("1", False), ("3", False)] + [("\x0e", False)] * 3
     + [("Teal blue", False), ("\t", True), ("1", False)]),
    # Enter on the multi-select's "Next" row leads into the next question.
    ([FRUIT, SIZE], [{"options": [2], "text": "Teal blue"}, {"option": 2}],
     [("2", False)] + [("\x0e", False)] * 3
     + [("Teal blue", False), ("\t", True), ("2", False), ("1", False)]),
])
def test_keystrokes_match_the_probed_picker(questions, answers, keys):
    assert answer_keystrokes(questions, answers) == keys


def test_typed_answer_is_one_line():
    """A newline would be an Enter mid-answer; whitespace runs collapse."""
    assert answer_keystrokes([COLOUR], [{"text": "  two\nlines  "}]) == [("4", False), ("two lines", True)]


@pytest.mark.parametrize("questions,answers,why", [
    ([COLOUR], [], "expected 1 answer"),
    ([COLOUR], [{"option": 4}], "out of range"),        # 4 is "Type something", not an option
    ([COLOUR], [{"option": 0}], "out of range"),
    ([COLOUR], [{"option": True}], "out of range"),     # a bool is not an index
    ([COLOUR], [{"option": "2"}], "out of range"),
    ([COLOUR], [{"text": "   "}], "empty"),
    ([COLOUR], [{"text": "x" * 501}], "over 500"),
    ([FRUIT], [{"options": []}], "at least one"),
    ([FRUIT], [{"options": [1, 1]}], "out of range"),
    ([FRUIT], [{}], "at least one option or type"),
    ([FRUIT], [{"options": [1], "text": "   "}], "empty"),
    ([FRUIT], [{"text": "x" * 501}], "over 500"),
    ([COLOUR], ["2"], "not an object"),
    ([], [], "no options"),
    ([_q("Too many", [str(i) for i in range(9)])], [{"option": 1}], "too many options"),
])
def test_a_malformed_answer_builds_no_keys(questions, answers, why):
    with pytest.raises(ValueError, match=why):
        answer_keystrokes(questions, answers)


# ---------------------------------------------------------------- reading


def test_questions_are_sanitized_and_an_unknown_shape_is_refused():
    raw = {"questions": [dict(COLOUR, question="  Pick a colour  ", extra="dropped")]}
    out = questions_from_input(raw)
    assert out == [{
        "question": "Pick a colour", "header": "Pick", "multiSelect": False,
        "options": [{"label": lab, "description": f"about {lab}"} for lab in ("Red", "Green", "Blue")],
    }]
    # Anything this reader doesn't know keeps the generic row instead.
    for bad in ({}, {"questions": "x"}, {"questions": [{"options": []}]},
                {"questions": [{"options": [{"description": "no label"}]}]}):
        assert questions_from_input(bad) is None, bad


def test_answers_come_from_the_line_and_a_declined_call_has_none():
    assert answers_from_result({"answers": {"Pick a colour": "Green"}, "questions": []}) == {"Pick a colour": "Green"}
    assert answers_from_result("User rejected tool use") is None
    assert answers_from_result({"answers": {}}) is None


def _jsonl(path: Path, rows: List[Dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _ask(tid: str, questions: List[Dict[str, Any]], mid: str = "m1", **extra: Any) -> Dict[str, Any]:
    row = {"type": "assistant", "timestamp": "2026-09-24T02:00:00Z",
           "message": {"id": mid, "role": "assistant", "content": [
               {"type": "tool_use", "id": tid, "name": "AskUserQuestion",
                "input": {"questions": questions}}]}}
    row.update(extra)
    return row


def _answer(tid: str, answers: Any, error: bool = False) -> Dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": tid, "content": "answered"}
    if error:
        block["is_error"] = True
    return {"type": "user", "timestamp": "2026-09-24T02:00:05Z", "toolUseResult": answers,
            "message": {"role": "user", "content": [block]}}


# ------------------------------------------------------ pending (send gate)


def test_pending_call_is_the_newest_unanswered_question(tmp_path: Path):
    path = _jsonl(tmp_path / "t.jsonl", [
        _ask("old", [COLOUR], mid="m1"),
        _answer("old", {"answers": {"Pick a colour": "Red"}}),
        _ask("live", [SIZE], mid="m2"),
        # Newer, but a sub-agent's: never the question the user is asked.
        _ask("sub", [COLOUR], mid="m3", isSidechain=True),
    ])
    pending = board_transcript.pending_decision_call(path)
    assert pending["id"] == "live" and pending["name"] == "AskUserQuestion"
    assert pending["input"]["questions"][0]["question"] == "Pick a size"


def test_nothing_pending_once_answered_or_unreadable(tmp_path: Path):
    path = _jsonl(tmp_path / "t.jsonl", [_ask("t1", [COLOUR]), _answer("t1", {"answers": {}})])
    assert board_transcript.pending_decision_call(path) is None
    assert board_transcript.pending_decision_call(tmp_path / "missing.jsonl") is None
    assert board_transcript.pending_decision_call(None) is None


# ------------------------------------------------------------------ route


def test_answer_route_is_passkey_gated():
    from src.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/answer") == "passkey"


