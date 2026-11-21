"""A very long session's transcript: bounded reads, and never an old empty
page standing in for a slow fresh one."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from src.session_view import transcript as tr
from tests.unit.transcript._support import _live

_TS = "2026-11-20T10:00:00.000Z"


def _event(kind: str, data: Dict[str, Any]) -> str:
    return json.dumps({"type": kind, "data": data, "timestamp": _TS}) + "\n"


def _turns(n: int) -> str:
    return "".join(
        _event("user.message", {"content": f"prompt {i}"})
        + _event("assistant.message", {"content": f"reply {i}"})
        for i in range(n)
    )


def test_a_200_mb_history_renders_its_last_turns_from_the_tail(tmp_path, monkeypatch):
    """The newest page of a 200 MB event log reads its last turns from the
    tail, never the whole file, and well inside the poll deadline."""
    path = tmp_path / "events.jsonl"
    with path.open("wb") as fh:
        fh.write(_event("session.start", {}).encode())
        fh.truncate(200 * 1024 * 1024)
        fh.seek(0, 2)
        fh.write(_turns(60).encode())
    read: List[int] = []
    real_open = Path.open

    def counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        fh = real_open(self, *args, **kwargs)
        real_read = fh.read

        def read_counted(n: int = -1) -> bytes:
            data = real_read(n)
            read.append(len(data))
            return data

        fh.read = read_counted
        return fh

    monkeypatch.setattr(Path, "open", counting_open)
    started = time.perf_counter()
    page = tr.transcript_page(path, flavor="copilot")
    elapsed = time.perf_counter() - started
    texts = [e["text"] for e in page["entries"] if e["kind"] in ("user", "assistant")]
    assert texts[-1] == "reply 59" and len(texts) == tr.DEFAULT_LIMIT
    assert page["next_cursor"] is not None
    # A window or two from the end, not the 200 MB in front of it.
    assert sum(read) <= 2 * tr.WINDOW_BYTES
    assert elapsed < 1.5


def test_a_slow_newest_page_is_never_answered_with_an_old_empty_one(
    webapp_client, _bypass_gate, monkeypatch, tmp_path
):
    """The chat opened while the session was new cached an empty newest page.
    Hours later the history is large and the build is slower than the poll
    deadline: the reopened chat must get the turns, not that old empty page
    reading "Nothing in the transcript yet"."""
    from app.webapp import hot_poll
    from app.webapp.routers import session_transcript as router_mod

    client, _, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_live(agent="copilot")]
    path = tmp_path / "events.jsonl"
    path.write_text(_event("session.start", {}), encoding="utf-8")
    monkeypatch.setattr(router_mod, "find_copilot_transcript", lambda session: path)
    monkeypatch.setattr(hot_poll, "DEADLINE_S", 0.05)

    first = client.get("/api/claude-code/sessions/s1/transcript").json()
    assert first["available"] is True and first["entries"] == []

    with path.open("a", encoding="utf-8") as fh:
        fh.write(_turns(3))
    real_page = router_mod.transcript_page

    def slow_page(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        time.sleep(0.3)
        return real_page(*args, **kwargs)

    monkeypatch.setattr(router_mod, "transcript_page", slow_page)
    again = client.get("/api/claude-code/sessions/s1/transcript").json()
    assert [e["text"] for e in again["entries"]][-1] == "reply 2"
    assert again.get("stale") is not True

    # A message sent now reaches the chat on the next live tick from there.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_event("user.message", {"content": "sent from the chat bar"}))
    tick = client.get(
        f"/api/claude-code/sessions/s1/transcript?after={again['tail']}&size={again['size']}"
    ).json()
    texts = [e["text"] for e in tick["entries"] + tick["pending"]]
    assert "sent from the chat bar" in texts
