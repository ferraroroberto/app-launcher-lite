"""Opening a long session's chat: the first transcript page stays small and fast.

The synthetic session has the shape of a real tool-heavy agent run (counts
taken from one, never its content): per exchange, seven tool calls with no
text, each followed by its result (short, medium or past the display cap)
and metadata rows (three
session-info records), then a thinking block and a reply.
A first page then spans several read windows of raw lines,
which is where a re-parse on every window step used to cost.
"""

from __future__ import annotations

import gzip
import json
import random
import time
from pathlib import Path

import pytest

from src.session_view import transcript as st
from src.transcript_flavors import _shared
from tests.unit.transcript._support import (
    _assistant,
    _event,
    _parse_whole,
    _tail_of,
    _thinking,
    _tool_result,
    _tool_use,
    _user,
    _walk,
    _write_jsonl,
)

# Wire budget for the first response, gzipped as the webapp serves it.
FIRST_PAGE_GZIP_BUDGET = 100 * 1024
# Best of three on this box measured ~10 ms; the margin absorbs a loaded gate.
FIRST_PAGE_MS_BUDGET = 100

# Real first pages measured 3.8-4.3x under gzip; uniform picks from this many
# words give ~3.5x, so the synthetic page is if anything the harder case.
_WORDS = [f"w{n:x}" for n in range(512)]


def _words(rng: random.Random, n: int) -> str:
    # Varied words, so gzip compresses like real text rather than a repeat.
    return " ".join(rng.choice(_WORDS) for _ in range(n))


def _long_session(path: Path, exchanges: int) -> Path:
    rng = random.Random(2510)
    rows = []
    for i in range(exchanges):
        rows.append(_user(_words(rng, 40)))
        for t in range(7):
            tid = f"t{i}-{t}"
            rows.append(_tool_use("powershell", {"command": _words(rng, 30)}, tid))
            rows.append(_tool_result(tid, _words(rng, rng.choice((20, 80, 400)))))
            rows.append(_event("session.info", {"message": _words(rng, 3600)}))
            rows.append(_event("session.usage_info", {"detail": _words(rng, 250)}))
            rows.append(_event("hook.end", {"detail": _words(rng, 30)}))
        rows.append(_thinking(_words(rng, 200)))
        rows.append(_assistant(_words(rng, 120)))
    return _write_jsonl(path, rows)


@pytest.fixture(scope="module")
def long_session(tmp_path_factory) -> Path:
    return _long_session(tmp_path_factory.mktemp("long") / "t.jsonl", 60)


def test_first_page_fits_the_wire_budget_and_time(long_session: Path):
    page = st.transcript_page(long_session, limit=st.DEFAULT_LIMIT)
    # The served page is cut to PAGE_JSON_BUDGET, so it holds fewer than a full limit of turns here.
    assert 1 <= sum(1 for e in page["entries"] if st._is_turn(e)) <= st.DEFAULT_LIMIT
    wire = len(gzip.compress(json.dumps(page).encode()))
    assert wire <= FIRST_PAGE_GZIP_BUDGET, f"first page is {wire // 1024} KB gzipped"
    best = min(_timed(long_session) for _ in range(3))
    assert best <= FIRST_PAGE_MS_BUDGET, f"first page took {best:.0f} ms"


def _timed(path: Path) -> float:
    started = time.perf_counter()
    st.transcript_page(path, limit=st.DEFAULT_LIMIT)
    return (time.perf_counter() - started) * 1000


def test_first_page_parses_each_line_about_once(long_session: Path, monkeypatch):
    """The deterministic half of the budget: a page that re-parses a line it
    already read on a later window step fails here, however fast the box."""
    parsed = []
    real = json.loads
    monkeypatch.setattr(_shared.json, "loads", lambda raw, *a, **k: parsed.append(raw) or real(raw, *a, **k))
    page = st.transcript_page(long_session, limit=st.DEFAULT_LIMIT)
    span = long_session.stat().st_size - page["next_cursor"]
    assert span > 3 * st.WINDOW_BYTES, "the page must span several read windows"
    assert len(parsed) == len(set(parsed)), "a line was parsed more than once"


def test_walking_back_from_the_first_page_loses_and_repeats_nothing(tmp_path: Path):
    path = _long_session(tmp_path / "t.jsonl", 30)
    pages = _walk(path, st.DEFAULT_LIMIT)
    assert len(pages) > 1
    joined = [e for page in reversed(pages) for e in page]
    assert joined == _parse_whole(path, tail=_tail_of(path, st.DEFAULT_LIMIT))
