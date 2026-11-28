"""The session list and the Board read each transcript once per change, not
once per reader per poll.

Every poll asks for a live session's tail several times (activity anchor,
model, waiting-status scans) on both endpoints. Each open of a file the
session-host is appending to can stall under on-access antivirus, so a test
here counts opens of each transcript while many polls run at once on a small
thread pool, with every open slowed by an injected delay.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Dict, List

import pytest

from src import board
from src.board import tail_cache
from src.board.transcript import _tail_lines
from tests.unit.board._support import NOW, _live, _msg_line, _state_row

_SESSIONS = 5
_POLLS = 20
_WORKERS = 4
_OPEN_DELAY_S = 0.05


@pytest.fixture(autouse=True)
def _empty_cache():
    tail_cache.clear()
    tail_cache.set_budget_mb(tail_cache.DEFAULT_BUDGET_MB)
    yield
    tail_cache.clear()
    tail_cache.set_budget_mb(tail_cache.DEFAULT_BUDGET_MB)


def _count_opens(monkeypatch, delay_s: float = 0.0) -> Dict[str, int]:
    opens: Dict[str, int] = {}
    real_open = Path.open

    def spy(self, *args, **kwargs):
        if self.suffix == ".jsonl":
            opens[self.name] = opens.get(self.name, 0) + 1
            time.sleep(delay_s)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    return opens


def _transcript(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.jsonl"
    lines = [_msg_line("user", NOW - timedelta(minutes=2)), _msg_line("assistant", NOW)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_an_unchanged_transcript_is_opened_once_for_every_window(tmp_path, monkeypatch):
    path = _transcript(tmp_path, "t")
    opens = _count_opens(monkeypatch)
    for n_bytes in (8 * 1024, 256 * 1024, 8 * 1024, 256 * 1024):
        lines, truncated = _tail_lines(path, n_bytes)
        assert len(lines) == 2 and truncated is False
    assert opens == {"t.jsonl": 1}


def test_a_grown_transcript_is_read_again(tmp_path, monkeypatch):
    path = _transcript(tmp_path, "t")
    opens = _count_opens(monkeypatch)
    assert len(_tail_lines(path, 8 * 1024)[0]) == 2
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_msg_line("user", NOW) + "\n")
    assert len(_tail_lines(path, 8 * 1024)[0]) == 3
    assert opens["t.jsonl"] == 3  # two reads plus the append itself


def test_a_zero_budget_reads_the_file_every_time(tmp_path, monkeypatch):
    tail_cache.set_budget_mb(0)
    path = _transcript(tmp_path, "t")
    opens = _count_opens(monkeypatch)
    _tail_lines(path, 8 * 1024)
    _tail_lines(path, 8 * 1024)
    assert opens == {"t.jsonl": 2}


def test_concurrent_polls_open_each_transcript_once_with_bounded_latency(tmp_path, monkeypatch):
    live: List[dict] = []
    rows: Dict[str, dict] = {}
    for i in range(_SESSIONS):
        project = f"E:/stubfolder/proj{i}"
        live.append(_live(f"s{i}", project, 30 - i))
        rows[f"t{i}"] = _state_row(
            project, status="needs-you", transcript_path=str(_transcript(tmp_path, f"t{i}"))
        )
    opens = _count_opens(monkeypatch, _OPEN_DELAY_S)

    def sessions_poll() -> None:
        board.attach_last_turn(live, rows)
        board.attach_transcript_model(live)
        board.merge_sessions(live, rows, now=NOW)

    def board_poll() -> None:
        board.merge_sessions(live, rows, now=NOW)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [pool.submit(sessions_poll if i % 2 else board_poll) for i in range(_POLLS)]
        for future in futures:
            future.result(timeout=30)
    elapsed = time.monotonic() - started

    assert opens == {f"t{i}.jsonl": 1 for i in range(_SESSIONS)}
    # One delayed open per session, spread over the pool, plus slack: a read
    # per reader per poll would take many times this.
    assert elapsed < _SESSIONS * _OPEN_DELAY_S * 4
