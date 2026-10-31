"""``src._json_io.file_lock`` — bounded retries, then proceed unlocked."""

from __future__ import annotations

import logging

import pytest

from src import _json_io

pytestmark = pytest.mark.skipif(
    _json_io.msvcrt is None, reason="msvcrt file locks are Windows-only"
)


def test_contended_lock_gives_up_after_the_named_attempts(
    tmp_path, monkeypatch, caplog
) -> None:
    calls: list[int] = []

    def locking(_fd: int, mode: int, _nbytes: int) -> None:
        if mode == _json_io.msvcrt.LK_LOCK:
            calls.append(mode)
            raise OSError("contended")

    monkeypatch.setattr(_json_io.msvcrt, "locking", locking)
    with caplog.at_level(logging.WARNING, logger=_json_io.logger.name):
        with _json_io.file_lock(tmp_path / "x.lock", label="test"):
            pass

    assert len(calls) == _json_io._LOCK_ATTEMPTS
    assert "writing unlocked" in caplog.text
