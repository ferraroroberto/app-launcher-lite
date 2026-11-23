"""Transcript reads on a busy machine: an exact id never scans, a correlation
is scanned once per real change, concurrent lookups share one scan, and an
unchanged file is never parsed twice."""

from __future__ import annotations

import asyncio
import itertools
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.webapp.routers import session_transcript
from src import board
from src.session_view import transcript as st
from src.session_view import transcript_locate as tl
from src.webapp_config import WebappConfig
from tests.unit.transcript._support import _user, _write_jsonl

_STARTED = 1_795_168_800.0  # 2026-11-20T10:00:00Z
_COPILOT_ID = "0f3c2a51-7d7e-4c55-9a0e-2b8c4ad1e002"


def _session(**extra: Any) -> Dict[str, Any]:
    row = {"session_id": "s1", "agent": "copilot", "project_dir": r"E:\work\project",
           "started_at": _STARTED}
    row.update(extra)
    return row


def _folder(root: Path, name: str, created: str, events: bool = True) -> Path:
    (root / name).mkdir(parents=True)
    (root / name / "workspace.yaml").write_text(
        f"cwd: E:\\work\\project\ncreated_at: {created}\n", encoding="utf-8")
    path = root / name / "events.jsonl"
    if events:
        path.write_text("{}\n", encoding="utf-8")
    return path


@pytest.fixture
def scans(tmp_path: Path, monkeypatch) -> List[int]:
    """Counts full launch-window scans over a fresh, empty state folder."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(tl, "_COPILOT_STATE_DIR", state)
    calls: List[int] = []
    real = tl._scan_copilot_workspaces

    def counted(*args: Any) -> Any:
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(tl, "_scan_copilot_workspaces", counted)
    return calls


# Every listing change gets its own mtime from a counter, far past any real
# one. Deriving it from the folder's current mtime repeats a stamp when two
# changes land inside one filesystem timestamp tick, and the cache then treats
# the second change as no change.
_LISTING_STAMPS = itertools.count(4_102_444_800_000_000_000, 1_000_000_000)


def _touch_listing(state: Path) -> None:
    stamp = next(_LISTING_STAMPS)
    os.utime(state, ns=(stamp, stamp))


def _bump(state: Path, name: str) -> None:
    """Add an unrelated folder and push the listing's mtime forward, as a
    new launch elsewhere does."""
    (state / name).mkdir()
    _touch_listing(state)


def test_exact_id_resolution_never_scans_the_state_folder(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    wanted = _folder(state, _COPILOT_ID, "2025-01-01T00:00:00Z")
    monkeypatch.setattr(tl, "_COPILOT_STATE_DIR", state)

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("an exact id must not list the session-state folder")

    monkeypatch.setattr(tl, "_scan_copilot_workspaces", refuse)
    monkeypatch.setattr(tl, "_scan_resumed_workspaces", refuse)
    monkeypatch.setattr(Path, "glob", refuse)
    monkeypatch.setattr(session_transcript, "read_live_sessions", lambda _p: ([_session()], None))
    row = {"status": "working", "cwd": r"E:\work\project", "agent": "copilot",
           "launcher_session_id": "s1", "updated_at": "2026-11-20T10:40:00Z"}
    monkeypatch.setattr(board, "read_sessions_state", lambda _p: {"rows": {_COPILOT_ID: row}})
    cfg = WebappConfig(sessions_state_file=str(tmp_path / "sessions-state.json"))
    reason, _f, path, *_ = asyncio.run(session_transcript.resolve_source("s1", cfg))
    assert reason is None and path == wanted


def test_an_unresolved_session_is_not_rescanned_until_the_state_changes(scans, monkeypatch):
    state = tl._COPILOT_STATE_DIR
    monkeypatch.setattr(time, "time", lambda: _STARTED + 20)
    assert tl.find_copilot_transcript(_session()) is None
    assert tl.find_copilot_transcript(_session()) is None
    assert len(scans) == 1, "an unchanged miss must cost a stat, not a scan"
    # A new folder in the window is a real change: it is found.
    wanted = _folder(state, "aaaa", "2026-11-20T10:00:04Z")
    _touch_listing(state)
    assert tl.find_copilot_transcript(_session()) == wanted
    assert len(scans) == 2
    # An expired miss TTL looks again even when the listing held.
    assert tl.find_copilot_transcript(_session(project_dir=r"E:\x"), miss_ttl_s=0) is None
    assert tl.find_copilot_transcript(_session(project_dir=r"E:\x"), miss_ttl_s=0) is None
    assert len(scans) == 4


def test_a_first_prompt_is_seen_without_a_rescan(scans, monkeypatch):
    """The window matched a folder whose log Copilot writes at the first
    prompt: the next tick stats that one file instead of listing them all."""
    state = tl._COPILOT_STATE_DIR
    monkeypatch.setattr(time, "time", lambda: _STARTED + 20)
    pending = _folder(state, "bbbb", "2026-11-20T10:00:04Z", events=False)
    assert tl.find_copilot_transcript(_session()) is None
    pending.write_text("{}\n", encoding="utf-8")
    assert tl.find_copilot_transcript(_session()) == pending
    assert len(scans) == 1


def test_a_settled_match_survives_new_launches_elsewhere(scans, monkeypatch):
    """A scan run after the launch window closed saw every folder that can
    qualify, so its log stays found while other lanes keep creating folders."""
    state = tl._COPILOT_STATE_DIR
    wanted = _folder(state, "cccc", "2026-11-20T10:00:04Z")
    monkeypatch.setattr(time, "time", lambda: _STARTED + 20)
    assert tl.find_copilot_transcript(_session()) == wanted
    _bump(state, "inside-window")
    assert tl.find_copilot_transcript(_session()) == wanted
    assert len(scans) == 2, "inside the window a new folder can make it ambiguous"
    monkeypatch.setattr(time, "time", lambda: _STARTED + 3600)
    _bump(state, "later")
    assert tl.find_copilot_transcript(_session()) == wanted
    assert len(scans) == 3, "the last scan ran inside the window, so it is checked once more"
    _bump(state, "later-still")
    assert tl.find_copilot_transcript(_session()) == wanted
    assert len(scans) == 3


def test_concurrent_lookups_share_one_scan(scans, monkeypatch):
    _folder(tl._COPILOT_STATE_DIR, "dddd", "2026-11-20T10:00:04Z")
    monkeypatch.setattr(time, "time", lambda: _STARTED + 20)
    real = tl._scan_copilot_workspaces  # the fixture's counting wrapper
    entered: List[int] = []
    second = threading.Event()

    def slow(*args: Any) -> Any:
        entered.append(1)
        if len(entered) > 1:
            second.set()
        second.wait(0.5)  # holds the first scan open while the other caller arrives
        return real(*args)

    monkeypatch.setattr(tl, "_scan_copilot_workspaces", slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        found = list(pool.map(lambda _i: tl.find_copilot_transcript(_session()), range(2)))
    assert found[0] == found[1] is not None
    assert len(scans) == 1


def test_an_unchanged_transcript_is_parsed_once(tmp_path: Path, monkeypatch):
    path = _write_jsonl(tmp_path / "events.jsonl", [_user("one"), _user("two")])
    parses: List[int] = []
    real = st._page
    monkeypatch.setattr(st, "_page", lambda *a, **k: (parses.append(1), real(*a, **k))[1])
    first = st.transcript_page(path)
    first["entries"].clear()  # a caller's edit never reaches the cache
    again = st.transcript_page(path)
    assert len(parses) == 1 and len(again["entries"]) == 2
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "user.message", "data": {"content": "three"}}\n')
    assert len(st.transcript_page(path)["entries"]) == 3 and len(parses) == 2
    st.transcript_page(path, cache_size=0)
    assert len(parses) == 3, "a zero cache size turns the cache off"
    # An unchanged live tail is one stat: the file is never opened.
    monkeypatch.setattr(st, "_read_lines_from", lambda *a: pytest.fail("tail re-read"))
    size = path.stat().st_size
    assert st.transcript_tail(path, after=size, size=size)["changed"] is False
