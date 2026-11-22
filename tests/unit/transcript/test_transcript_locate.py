"""Coding-tab transcript: locating a live Copilot session's event log."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

# -------------------------------------------- copilot source correlation


def _copilot_session_dir(root: Path, folder: str, cwd: str, created: str,
                       *, events: bool = True) -> Path:
    """One Copilot session folder, as the harness lays it out at launch."""
    session = root / folder
    session.mkdir(parents=True, exist_ok=True)
    (session / "workspace.yaml").write_text(
        "id: {0}\ncwd: {1}\ngit_root: {1}\nclient_name: github/cli\n"
        "created_at: {2}\nupdated_at: {2}\n".format(folder, cwd, created),
        encoding="utf-8",
    )
    events_path = session / "events.jsonl"
    if events:
        events_path.write_text("{}\n", encoding="utf-8")
    return events_path


def _copilot_session(**extra: Any) -> Dict[str, Any]:
    # 2026-09-17T21:57:30.465Z, the capture's own launch instant.
    row = {"session_id": "s1", "agent": "copilot", "alive": True,
           "project_dir": r"E:\work\project", "started_at": 1_789_682_250.465}
    row.update(extra)
    return row


def test_find_copilot_transcript_matches_cwd_and_launch_window(tmp_path: Path, monkeypatch):
    """Copilot writes the launcher nothing — no sessions-state row, and its
    own log never contains the launcher's session id — so the correlation is
    its ``workspace.yaml`` sidecar: this folder's ``cwd``, created inside the
    launch window. Measured on two real launches: 3.71 s and 3.72 s after
    ``started_at`` (#1015 capture)."""
    from src.session_view import transcript_locate

    wanted = _copilot_session_dir(
        tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
        r"E:\work\project", "2026-09-17T21:57:34.176Z",
    )
    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    assert transcript_locate.find_copilot_transcript(_copilot_session()) == wanted
    # Neither the separator nor the case of the harness's own spelling has
    # to match the session's.
    assert transcript_locate.find_copilot_transcript(
        _copilot_session(project_dir="e:/work/project/")
    ) == wanted
    # A different folder is not this session's conversation.
    assert transcript_locate.find_copilot_transcript(
        _copilot_session(project_dir=r"E:\work\other")
    ) is None
    # A session launched long before this folder existed is not its owner.
    assert transcript_locate.find_copilot_transcript(
        _copilot_session(started_at=1_789_600_000.0)
    ) is None
    # Nor one launched *after* it: the folder is created at launch, so a
    # later session's folder is a different one.
    assert transcript_locate.find_copilot_transcript(
        _copilot_session(started_at=1_789_682_400.0)
    ) is None


def test_find_copilot_transcript_allows_a_detached_sessions_launch_delay(
    tmp_path: Path, monkeypatch
):
    """A detached session's ``started_at`` is stamped after its PowerShell
    launch, and Copilot starts inside it, so its folder can predate the stamp
    by seconds; an attached session's cannot."""
    from src.session_view import transcript_locate

    wanted = _copilot_session_dir(
        tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
        r"E:\work\project", "2026-09-17T21:57:30.465Z",
    )
    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    slow = 1_789_682_250.465 + 12.0   # the folder is 12 s older than the stamp
    assert transcript_locate.find_copilot_transcript(
        _copilot_session(kind="remote", started_at=slow)
    ) == wanted
    assert transcript_locate.find_copilot_transcript(
        _copilot_session(kind="pty", started_at=slow)
    ) is None


def test_find_copilot_transcript_refuses_two_candidates_in_one_window(
    tmp_path: Path, monkeypatch
):
    """Two sessions started in one folder inside the window are ambiguous by
    construction — the sidecar says which folder, never which session — so
    both refuse rather than risk showing the other's conversation, the rule
    ``find_codex_transcript`` follows."""
    import time

    from src.session_view import transcript_locate

    _copilot_session_dir(tmp_path, "aaaaaaaa-0000-0000-0000-000000000001",
                       r"E:\work\project", "2026-09-17T21:57:34.176Z")
    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    # Inside the launch window, where a second launch can still appear.
    monkeypatch.setattr(time, "time", lambda: _copilot_session()["started_at"] + 30)
    assert transcript_locate.find_copilot_transcript(_copilot_session()) is not None
    # A second launch in the same folder, 20 s later — still inside the
    # window, so neither folder can be claimed.
    _copilot_session_dir(tmp_path, "aaaaaaaa-0000-0000-0000-000000000002",
                       r"E:\work\project", "2026-09-17T21:57:54.176Z")
    assert transcript_locate.find_copilot_transcript(_copilot_session()) is None
    # A same-folder session outside the window does not make the first
    # ambiguous — which is what keeps the ordinary case answerable.
    for stale in tmp_path.glob("aaaaaaaa-0000-0000-0000-000000000002/*"):
        stale.unlink()
    _copilot_session_dir(tmp_path, "aaaaaaaa-0000-0000-0000-000000000003",
                       r"E:\work\project", "2026-09-17T22:05:00.000Z")
    assert transcript_locate.find_copilot_transcript(_copilot_session()) is not None


def test_find_copilot_transcript_needs_an_events_file(tmp_path: Path, monkeypatch):
    """The folder and its sidecar are written at launch, ``events.jsonl``
    only at the first prompt — so a session nobody has typed into has no
    transcript, and neither do the 28 of 71 older sessions here that kept a
    ``session.db`` instead."""
    from src.session_view import transcript_locate

    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    events = _copilot_session_dir(
        tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
        r"E:\work\project", "2026-09-17T21:57:34.176Z", events=False,
    )
    assert transcript_locate.find_copilot_transcript(_copilot_session()) is None
    events.write_text("{}\n", encoding="utf-8")
    assert transcript_locate.find_copilot_transcript(_copilot_session()) == events


def test_find_copilot_transcript_survives_a_damaged_sidecar(tmp_path: Path, monkeypatch):
    """A folder whose ``workspace.yaml`` is missing, unparseable or lacks the
    two keys matches nothing — it must not raise, and must not swallow the
    real match sitting beside it."""
    from src.session_view import transcript_locate

    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "workspace.yaml").write_text(
        "cwd: E:\\work\\project\ncreated_at: not-a-timestamp\n", encoding="utf-8"
    )
    (tmp_path / "keyless").mkdir()
    (tmp_path / "keyless" / "workspace.yaml").write_text("id: x\n", encoding="utf-8")
    assert transcript_locate.find_copilot_transcript(_copilot_session()) is None
    wanted = _copilot_session_dir(
        tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
        r"E:\work\project", "2026-09-17T21:57:34.176Z",
    )
    assert transcript_locate.find_copilot_transcript(_copilot_session()) == wanted


def test_find_copilot_transcript_needs_a_dir_and_a_start(tmp_path: Path, monkeypatch):
    """A session row missing either half of the correlation answers None
    rather than scanning for a best guess."""
    from src.session_view import transcript_locate

    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", tmp_path)
    _copilot_session_dir(tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
                       r"E:\work\project", "2026-09-17T21:57:34.176Z")
    assert transcript_locate.find_copilot_transcript(_copilot_session(project_dir="")) is None
    assert transcript_locate.find_copilot_transcript(_copilot_session(started_at=None)) is None
    # An ISO `started_at` (a row read back from JSON state) works too.
    assert transcript_locate.find_copilot_transcript(
        _copilot_session(started_at="2026-09-17T21:57:30.465Z")
    ) is not None
