"""A very long session's transcript: bounded reads, a small first response
that is quick to answer, and never an old empty page standing in for a slow
fresh one."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from src.session_view import transcript as tr
from src.session_view import transcript_locate
from tests.unit.transcript._support import _live, _parse_whole, _tail_of, _walk

_TS = "2026-11-20T10:00:00.000Z"


def _event(kind: str, data: Dict[str, Any]) -> str:
    return json.dumps({"type": kind, "data": data, "timestamp": _TS}) + "\n"


def _tool_heavy_turns(n: int, tools: int) -> str:
    """``n`` exchanges, each with ``tools`` file reads whose results fill the
    page's per-result cap, the shape that made a long session's opening page
    weigh hundreds of KB."""
    out = []
    for i in range(n):
        out.append(_event("user.message", {"content": f"prompt {i} " + "word " * 30}))
        for t in range(tools):
            call = f"t{i}-{t}"
            out.append(_event("tool.execution_start", {
                "toolCallId": call, "toolName": "view", "arguments": {"path": f"src/f{t}.py"},
            }))
            out.append(_event("tool.execution_complete", {
                "toolCallId": call, "success": True, "result": {"content": "code line\n" * 200},
            }))
        out.append(_event("assistant.message", {"content": f"reply {i} " + "text " * 150}))
    return "".join(out)


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
    monkeypatch.setattr(router_mod, "find_copilot_transcript", lambda session, **_k: path)
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


# The first transcript response's budgets: its size for any session length,
# and how long a reopen may take to answer. The time is a generous ceiling
# for a loaded CI box; a warm open here answers in ~20 ms.
FIRST_RESPONSE_MAX_BYTES = 100 * 1024
WARM_OPEN_MAX_S = 0.5


def test_a_long_tool_heavy_session_opens_small_and_fast(
    webapp_client, _bypass_gate, monkeypatch, tmp_path
):
    """Opening the chat of a 25 MB tool-heavy Copilot session, among
    hundreds of other session folders, answers at most 100 KB however many
    tool results sit between its turns, and a reopen reads no session
    sidecar again: path resolution was most of every open's time."""
    client, _, overrides = webapp_client
    state = tmp_path / "session-state"
    for n in range(400):
        other = state / f"other-{n:04d}"
        other.mkdir(parents=True)
        (other / "workspace.yaml").write_text(
            f"cwd: E:\\stub\\other{n}\ncreated_at: 2026-11-01T10:00:00.000Z\n", encoding="utf-8"
        )
    mine = state / "mine"
    mine.mkdir()
    (mine / "workspace.yaml").write_text(
        "cwd: E:\\stub\\project\ncreated_at: 2026-11-20T10:00:03.000Z\n", encoding="utf-8"
    )
    log = mine / "events.jsonl"
    log.write_text(_event("session.start", {}) + _tool_heavy_turns(1500, 8), encoding="utf-8")
    assert log.stat().st_size > 20 * 1024 * 1024
    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", state)
    overrides["session"].list_sessions.return_value = [_live(
        agent="copilot", project_dir=r"E:\stub\project", started_at=1_795_168_800.0,
    )]
    reads: List[Path] = []
    real_open = Path.open

    def counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == "workspace.yaml":
            reads.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    first = client.get("/api/claude-code/sessions/s1/transcript")
    assert first.json()["available"] is True
    assert len(first.content) <= FIRST_RESPONSE_MAX_BYTES
    assert reads, "the first open resolves the log from the sidecars"

    reads.clear()
    started = time.perf_counter()
    again = client.get("/api/claude-code/sessions/s1/transcript?limit=39")
    elapsed = time.perf_counter() - started
    body = again.json()
    assert reads == []
    assert elapsed < WARM_OPEN_MAX_S
    assert len(again.content) <= FIRST_RESPONSE_MAX_BYTES
    assert body["entries"][-1]["text"].startswith("reply 1499")
    assert body["next_cursor"] is not None


def test_pages_held_to_the_json_budget_still_join_to_the_whole_history(tmp_path):
    """Pages cut short by the size budget still walk back to the file start
    with every entry exactly once, so scrolling up loses and repeats
    nothing; each page fits the budget, or holds a single turn that does
    not."""
    path = tmp_path / "events.jsonl"
    path.write_text(_event("session.start", {}) + _tool_heavy_turns(60, 12), encoding="utf-8")
    pages = _walk(path, 40, flavor="copilot")
    assert len(pages) > 2
    for page in pages:
        turns = sum(1 for e in page if e["kind"] in ("user", "assistant"))
        assert turns == 1 or len(json.dumps(page, ensure_ascii=False)) <= tr.PAGE_JSON_BUDGET
    joined = [e for page in reversed(pages) for e in page]
    assert joined == _parse_whole(path, build=tr.copilot_entries, tail=_tail_of(path, 40, "copilot"))
