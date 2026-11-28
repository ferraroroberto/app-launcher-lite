"""A live Copilot session's transcript resolves through every link it has:
the hook row's recorded path, the row's own Copilot session id, and the
session folder a resumed launch reopened, and an unresolved one says which
link is missing."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from app.webapp.routers import session_transcript
from src import board
from src.session_view import transcript_locate
from src.webapp_config import WebappConfig

# 2026-11-20T10:00:00Z: the launch the live session records.
_STARTED = 1_795_168_800.0
_COPILOT_ID = "0f3c2a51-7d7e-4c55-9a0e-2b8c4ad1e001"


def _live(**extra: Any) -> Dict[str, Any]:
    row = {"session_id": "s1", "agent": "copilot", "alive": True,
           "project_dir": r"E:\stub\project", "started_at": _STARTED}
    row.update(extra)
    return row


def _folder(root: Path, folder: str, created: str, events: Optional[str] = "") -> Path:
    """A Copilot session folder; ``events`` None leaves no ``events.jsonl``."""
    session = root / folder
    session.mkdir(parents=True)
    (session / "workspace.yaml").write_text(
        f"id: {folder}\ncwd: E:\\stub\\project\ncreated_at: {created}\nupdated_at: {created}\n",
        encoding="utf-8",
    )
    path = session / "events.jsonl"
    if events is not None:
        path.write_text(events, encoding="utf-8")
    return path


def _line(kind: str, ts: str, data: Optional[Dict[str, Any]] = None) -> str:
    return json.dumps({"type": kind, "data": data or {}, "timestamp": ts}) + "\n"


def _resolve(monkeypatch, tmp_path: Path, rows: Dict[str, Dict[str, Any]]):
    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir(exist_ok=True)
    monkeypatch.setattr(session_transcript, "read_live_sessions", lambda _port: ([_live()], None))
    monkeypatch.setattr(board, "read_sessions_state", lambda _path: {"rows": rows})
    cfg = WebappConfig(sessions_state_file=str(tmp_path / "sessions-state.json"))
    return asyncio.run(session_transcript.resolve_source("s1", cfg))


def _row(transcript: Optional[str]) -> Dict[str, Any]:
    return {"status": "working", "transcript_path": transcript, "cwd": r"E:\stub\project",
            "agent": "copilot", "launcher_session_id": "s1",
            "updated_at": "2026-11-20T10:40:00Z"}


def test_a_resumed_session_with_no_recorded_path_resolves_by_its_copilot_id(
    tmp_path: Path, monkeypatch,
):
    """A resumed launch reopens a folder Copilot created long before it, so
    the cwd + launch-window match finds nothing, and only agentStop records
    a path, so a first turn 40 minutes long has none. The row's own key is
    the Copilot session id, which names the folder."""
    state = tmp_path / "state"
    state.mkdir()
    events = _folder(state, _COPILOT_ID, "2026-11-01T08:00:00.000Z",
                     _line("session.start", "2026-11-01T08:00:00.000Z"))
    reason, _flavor, path, *_ = _resolve(monkeypatch, tmp_path, {_COPILOT_ID: _row(None)})
    assert reason is None
    assert path == events


def test_a_recorded_path_that_is_gone_falls_back_to_the_other_links(
    tmp_path: Path, monkeypatch,
):
    state = tmp_path / "state"
    state.mkdir()
    events = _folder(state, _COPILOT_ID, "2026-11-01T08:00:00.000Z", "")
    gone = str(tmp_path / "moved" / "events.jsonl")
    reason, _flavor, path, *_ = _resolve(monkeypatch, tmp_path, {_COPILOT_ID: _row(gone)})
    assert reason is None
    assert path == events


def test_an_unresolved_session_names_the_missing_link(tmp_path: Path, monkeypatch):
    gone = str(tmp_path / "moved" / "events.jsonl")
    reason, _f, _p, _a, why, _s = _resolve(monkeypatch, tmp_path, {"other-id": _row(gone)})
    assert (reason, why) == ("no_transcript", "recorded_file_gone")
    reason, _f, _p, _a, why, _s = _resolve(monkeypatch, tmp_path, {"other-id": _row(None)})
    assert (reason, why) == ("no_transcript", "no_path_recorded")
    reason, _f, _p, _a, why, _s = _resolve(monkeypatch, tmp_path, {})
    assert (reason, why) == ("no_transcript", "no_hook_row")


def test_a_resumed_folder_is_found_with_no_row_from_its_resume_event(
    tmp_path: Path, monkeypatch,
):
    """With no hook row at all, the folder a resumed launch reopened is the
    one whose log records a ``session.resume`` inside the launch window."""
    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    resumed = _folder(tmp_path, "a-resumed", "2026-11-01T08:00:00.000Z",
                      _line("session.start", "2026-11-01T08:00:00.000Z")
                      + _line("session.resume", "2026-11-20T10:00:04.000Z"))
    # An older resume of another folder in the same cwd is not this launch.
    _folder(tmp_path, "b-older", "2026-10-01T08:00:00.000Z",
            _line("session.resume", "2026-11-19T09:00:00.000Z"))
    assert transcript_locate.find_copilot_transcript(_live()) == resumed


def test_two_folders_resumed_in_one_window_refuse(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    for name in ("a", "b"):
        _folder(tmp_path, name, "2026-11-01T08:00:00.000Z",
                _line("session.resume", "2026-11-20T10:00:04.000Z"))
    assert transcript_locate.find_copilot_transcript(_live()) is None


# A pid no process holds.
_DEAD_PID = 99_999_999


def _lock(events: Path, opened_s: float, pid: Optional[int] = None) -> None:
    """The ``inuse.<pid>.lock`` Copilot keeps in every session folder one of
    its processes has opened, stamped ``opened_s`` after the launch."""
    owner = os.getpid() if pid is None else pid
    lock = events.parent / f"inuse.{owner}.lock"
    lock.write_text(f"{owner}\n", encoding="utf-8")
    os.utime(lock, (_STARTED + opened_s, _STARTED + opened_s))


def _history(state: Path, name: str = "a-history") -> Path:
    """A conversation from an earlier run, not yet written to by this one."""
    events = _folder(state, name, "2026-11-01T08:00:00.000Z",
                     _line("session.start", "2026-11-01T08:00:00.000Z")
                     + _line("user.message", "2026-11-01T08:00:05.000Z", {"content": "earlier"}))
    old = _STARTED - 86_400
    os.utime(events, (old, old))
    return events


def _launch_folder(state: Path, name: str = "b-launch", events: Optional[str] = None) -> Path:
    """The empty folder every interactive launch creates for itself."""
    text = _line("session.start", "2026-11-20T10:00:04.000Z") if events is None else events
    return _folder(state, name, "2026-11-20T10:00:04.000Z", text)


def test_a_conversation_picked_after_launch_replaces_the_empty_launch_folder(
    tmp_path: Path, monkeypatch,
):
    """``--resume`` (and ``/resume``) leaves the launch's own folder holding
    no turn and appends to the picked one, writing nothing there until the
    next prompt. The process's locks name both; the later one is on screen."""
    state = tmp_path / "state"
    history, launch = _history(state), _launch_folder(state)
    _lock(launch, 4)
    _lock(history, 900)
    reason, _flavor, path, *_ = _resolve(monkeypatch, tmp_path, {})
    assert reason is None
    assert path == history


def test_a_resume_by_id_is_found_before_its_first_prompt(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    history = _history(state)
    _lock(history, 4)
    reason, _flavor, path, *_ = _resolve(monkeypatch, tmp_path, {})
    assert reason is None
    assert path == history


def test_a_session_with_its_own_turns_is_never_swapped_for_another(
    tmp_path: Path, monkeypatch,
):
    state = tmp_path / "state"
    history = _history(state)
    launch = _launch_folder(
        state, events=_line("user.message", "2026-11-20T10:00:09.000Z", {"content": "new"}))
    _lock(launch, 4)
    _lock(history, 900)
    _reason, _flavor, path, *_ = _resolve(monkeypatch, tmp_path, {})
    assert path == launch


def test_only_this_launch_s_own_process_is_followed(tmp_path: Path, monkeypatch):
    """Another process's lock, and a lock a dead process left behind, name
    nothing: the empty launch folder stays."""
    state = tmp_path / "state"
    history, launch = _history(state), _launch_folder(state)
    _lock(launch, 4)
    _lock(history, 900, pid=os.getppid())
    _lock(_history(state, "c-crashed"), 900, pid=_DEAD_PID)
    _reason, _flavor, path, *_ = _resolve(monkeypatch, tmp_path, {})
    assert path == launch


def test_the_conversation_opened_last_wins(tmp_path: Path, monkeypatch):
    """``/new`` after a resume: the newest conversation has no history, so
    the resumed one is no longer what the terminal shows."""
    state = tmp_path / "state"
    history, launch = _history(state), _launch_folder(state)
    fresh = _folder(state, "d-new", "2026-11-20T10:20:00.000Z",
                    _line("session.start", "2026-11-20T10:20:00.000Z"))
    _lock(launch, 4)
    _lock(history, 900)
    _lock(fresh, 1200)
    _reason, _flavor, path, *_ = _resolve(monkeypatch, tmp_path, {})
    assert path == fresh
