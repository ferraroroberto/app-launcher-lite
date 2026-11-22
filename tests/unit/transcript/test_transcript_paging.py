"""Coding-tab transcript: paging, byte caps and full-text reads over the Copilot grammar."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.session_view import transcript as st
from src.transcript_flavors import _shared as tf_shared
from tests.unit.transcript._support import (
    _assistant,
    _conversation,
    _kinds,
    _parse_whole,
    _tail_of,
    _thinking,
    _tool_result,
    _tool_use,
    _user,
    _walk,
    _write_jsonl,
)


def test_every_flavour_declares_its_tool_error_fidelity():
    """The declaration rides in the ``FLAVORS`` row rather than in a table
    beside it, so a new harness cannot be added without answering "can this
    one tell a failed call from a working one?" — and cannot silently
    default to the answer that hides failures."""
    allowed = {st.TOOL_ERRORS_REPORTED, st.TOOL_ERRORS_PARTIAL, st.TOOL_ERRORS_NONE}
    for name, row in st.FLAVORS.items():
        assert len(row) == 3, name
        assert row[2] in allowed, (name, row[2])
    # Every agent the endpoint offers Chat mode for is covered.
    from app.webapp.routers.session_transcript import _FLAVOR_BY_AGENT
    assert set(_FLAVOR_BY_AGENT.values()) == set(st.FLAVORS)


def test_a_flavor_with_no_grammar_is_refused_not_read_as_another(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
    with pytest.raises(ValueError):
        st.transcript_page(path, flavor="claude")


def test_unknown_tool_result_stands_alone(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", [_tool_result("ghost", "orphan output")])
    entries = _parse_whole(path)
    assert _kinds(entries) == ["tool_result"]
    assert entries[0]["tool_use_id"] == "ghost" and entries[0]["text"] == "orphan output"


@pytest.mark.parametrize("limit", [1, 7, 40])
def test_pages_concatenate_to_one_full_parse(tmp_path: Path, monkeypatch, limit: int):
    """No turn duplicated or dropped across pages, whatever the page size —
    with the read window shrunk so every window edge tears a line."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 300)
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(60))
    pages = _walk(path, limit)
    assert len(pages) > 1
    for page in pages:
        assert sum(1 for e in page if e["kind"] in ("user", "assistant")) <= limit
    joined = [e for page in reversed(pages) for e in page]
    assert joined == _parse_whole(path, tail=_tail_of(path, limit))


def test_first_page_is_the_newest_turns_and_cursor_advances(tmp_path: Path):
    """``limit`` counts user + assistant entries — three per exchange here."""
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(10))
    page = st.transcript_page(path, limit=6)
    users = [e["text"] for e in page["entries"] if e["kind"] == "user"]
    assert users == ["prompt 8 p", "prompt 9 pp"]
    # The exchange's thinking + tool calls ride between its turns.
    assert _kinds(page["entries"])[:4] == ["user", "thinking", "assistant", "tool_call"]
    assert page["next_cursor"] is not None
    older = st.transcript_page(path, before=page["next_cursor"], limit=6)
    assert [e["text"] for e in older["entries"] if e["kind"] == "user"] == ["prompt 6 pppppp", "prompt 7"]


def test_whole_file_in_one_page_has_no_cursor(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(3))
    page = st.transcript_page(path, limit=40)
    assert page["next_cursor"] is None
    assert page["entries"] == _parse_whole(path, tail=page["tail"])


def test_record_wider_than_the_window_is_read_whole(tmp_path: Path, monkeypatch):
    """A pasted prompt is one JSONL line of hundreds of KB. Sliding the window
    into its middle used to tear its tail off and lose the whole turn."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 200)
    big = "B" * 5_000
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("first"),
        _assistant("ok"),
        _user(big),
        _assistant("seen it"),
        _user("last"),
        _assistant("bye"),
    ])
    pages = _walk(path, 1)
    joined = [e for page in reversed(pages) for e in page]
    assert [e["text"][:5] for e in joined if e["kind"] == "user"] == ["first", "BBBBB", "last"]
    assert joined == _parse_whole(path)


def test_byte_cap_yields_a_short_page_with_a_cursor(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(st, "WINDOW_BYTES", 300)
    monkeypatch.setattr(st, "REQUEST_BYTE_CAP", 1_200)
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(40))
    page = st.transcript_page(path, limit=100)
    turns = sum(1 for e in page["entries"] if e["kind"] in ("user", "assistant"))
    assert 0 < turns < 100, "the cap must cut the page well short of the 100 asked"
    assert page["next_cursor"] is not None
    # …and the walk still completes losslessly.
    pages = _walk(path, 100)
    assert [e for page in reversed(pages) for e in page] == _parse_whole(
        path, tail=_tail_of(path, 100)
    )


def _big_result_run(prefix: str, count: int, *, size: int = 1024 * 1024) -> List[Dict[str, Any]]:
    """``count`` tool calls whose results are each one ~``size``-byte line —
    the shape of a huge tool output that filled pages with no conversation in
    them. Synthetic bytes only."""
    blob = "A" * size
    lines: List[Dict[str, Any]] = []
    for k in range(count):
        tid = f"{prefix}-{k}"
        lines.append(_tool_use("powershell", {"command": "dump"}, tid))
        lines.append(_tool_result(tid, blob))
    return lines


def test_huge_tool_results_do_not_starve_a_page_of_turns(tmp_path: Path):
    """A few ~1 MB tool-result lines used to spend the whole 2 MiB budget, so
    a page stopped with zero conversation turns in it. A line is charged at
    most :data:`LINE_CHARGE_CAP` toward the budget, so one page reaches past
    them to the older turns."""
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("old prompt"),
        _assistant("old reply"),
        *_big_result_run("s", 6),
        _user("new prompt"),
        _assistant("new reply"),
    ])
    page = st.transcript_page(path, limit=40)
    turns = [e["text"] for e in page["entries"] if st._is_turn(e)]
    assert turns == ["old prompt", "old reply", "new prompt", "new reply"]
    assert page["next_cursor"] is None


def test_real_bytes_read_stay_under_the_ceiling(tmp_path: Path, monkeypatch):
    """The charge cap must not make a page unbounded: a region of nothing
    but huge lines stops at :data:`REQUEST_READ_CEILING` real bytes with a
    cursor, and the walk from there still reaches every turn. (A page with
    no turn keeps its raw edge, so a call and its result may land on two
    pages — `_page`'s documented exception — hence turns, not entries.)"""
    monkeypatch.setattr(st, "REQUEST_READ_CEILING", 3 * 1024 * 1024)
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("first prompt"),
        _assistant("first reply"),
        *_big_result_run("s", 10),
    ])
    page = st.transcript_page(path, limit=40)
    assert page["next_cursor"] is not None
    assert not any(st._is_turn(e) for e in page["entries"])
    assert len(page["entries"]) < 10, "the ceiling must cut the run short"
    joined = [e for page in reversed(_walk(path, 40)) for e in page]
    assert [e["text"] for e in joined if st._is_turn(e)] == ["first prompt", "first reply"]
    assert sum(1 for e in joined if e["kind"] == "tool_call") == 10


def test_pair_split_across_a_page_boundary_keeps_the_result(tmp_path: Path):
    """A tool call whose result lands on the newer page: the result stands
    alone there (never lost), the call shows no result on the older page."""
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("go"),
        _tool_use("powershell", {"command": "ls"}, "t1"),
        _assistant("meanwhile"),
        _tool_result("t1", "a b c"),
    ])
    newest = st.transcript_page(path, limit=1)
    assert _kinds(newest["entries"]) == ["assistant", "tool_result"]
    assert newest["entries"][1]["text"] == "a b c"
    older = st.transcript_page(path, before=newest["next_cursor"], limit=1)
    assert _kinds(older["entries"]) == ["user", "tool_call"]
    assert older["entries"][1]["result"] is None
    assert older["next_cursor"] is None


def test_missing_file_raises_oserror(tmp_path: Path):
    with pytest.raises(OSError):
        st.transcript_page(tmp_path / "nope.jsonl")


def test_empty_file_is_an_empty_page(tmp_path: Path):
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")
    assert st.transcript_page(path) == {
        "entries": [], "next_cursor": None, "tool_errors": "partial",
        # An empty file still hands back a cursor a live view can resume
        # from, so the first turn of a just-started session appears.
        "tail": 0, "size": 0,
    }


def test_page_exposes_offset_on_turns_only(tmp_path: Path):
    """A turn keeps its byte offset (the copy-full-text route's lookup key) —
    the folded kinds have it stripped.

    One exception: from the live-tail offset on, every kind keeps an offset,
    because a live view re-renders exactly that region and needs a stable key
    per card.
    """
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("hi"),
        _thinking("hmm"),
        _assistant("hello"),
        _user("more"),
        _thinking("later"),
        _assistant("done"),
    ])
    page = st.transcript_page(path)
    entries = page["entries"]
    tail = page["tail"]
    # Split where the client does: the first entry carrying an offset at or
    # past the tail opens the live region, and everything after it is in it.
    cut = next(i for i, e in enumerate(entries) if e.get("offset", -1) >= tail)
    settled, live = entries[:cut], entries[cut:]
    assert [e["kind"] for e in settled] == ["user", "thinking", "assistant", "user", "thinking"]
    for e in settled:
        if e["kind"] in ("user", "assistant"):
            assert isinstance(e["offset"], int)
        else:
            assert "offset" not in e, "a settled folded entry keeps no offset"
    assert [e["kind"] for e in live] == ["assistant"]
    for e in live:
        assert isinstance(e["offset"], int), "the live tail keys every kind"


# ---------------------------------------------------------- entry_full_text


def test_entry_full_text_user_is_uncapped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tf_shared, "USER_TEXT_CAP", 5)
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("a long prompt that would be capped")])
    page = st.transcript_page(path)
    entry = page["entries"][0]
    assert entry["truncated"] is True
    full = st.entry_full_text(path, entry["offset"], "copilot")
    assert full == {"text": "a long prompt that would be capped", "truncated": False}


def test_entry_full_text_assistant_is_uncapped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tf_shared, "ASSISTANT_TEXT_CAP", 5)
    path = _write_jsonl(tmp_path / "t.jsonl", [_assistant("a whole answer")])
    page = st.transcript_page(path)
    entry = page["entries"][0]
    assert entry["truncated"] is True
    full = st.entry_full_text(path, entry["offset"], "copilot")
    assert full == {"text": "a whole answer", "truncated": False}


def test_entry_full_text_unknown_offset_is_none(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
    assert st.entry_full_text(path, 3, "copilot") is None       # mid-line, no turn starts there
    assert st.entry_full_text(path, 9_999, "copilot") is None   # past EOF
