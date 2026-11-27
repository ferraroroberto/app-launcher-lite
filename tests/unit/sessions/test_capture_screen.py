"""A session's screen is rendered once per capture state.

A render is ~85 ms of GIL-bound pyte work and the Board polls every few
seconds, so an unchanged capture is served from memory until the file changes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.session_view import capture_screen


@pytest.fixture
def renders(monkeypatch):
    seen: list = []
    real = capture_screen.screen_lines

    def counting(text, rows, cols):
        seen.append((rows, cols))
        return real(text, rows, cols)

    monkeypatch.setattr(capture_screen, "screen_lines", counting)
    capture_screen._screens.clear()
    yield seen
    capture_screen._screens.clear()


def test_an_unchanged_capture_is_rendered_once_and_a_changed_one_again(tmp_path: Path, renders):
    capture = tmp_path / "s1.transcript"
    capture.write_bytes(b"first screen\r\n")
    for _ in range(3):
        assert capture_screen.read_screen(capture, 10, 40)[0] == "first screen"
    assert len(renders) == 1
    capture.write_bytes(b"\x1b[2J\x1b[Hsecond screen\r\n")
    later = capture.stat().st_mtime + 5
    os.utime(capture, (later, later))
    assert capture_screen.read_screen(capture, 10, 40)[0] == "second screen"
    assert len(renders) == 2


def test_a_capture_that_cannot_be_read_is_unknown(tmp_path: Path, renders):
    assert capture_screen.read_screen(tmp_path / "missing.transcript", 10, 40) is None
    assert renders == []
