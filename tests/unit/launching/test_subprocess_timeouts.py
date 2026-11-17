"""A wedged child must expire into a logged, non-fatal outcome, never a hang.

Pins the `subprocess-timeout` rule's fixes in the tray clipboard helper: the bounded call reports its own
expiry and lets the caller carry on.
"""

from __future__ import annotations

import logging
import subprocess

from app.tray import tray


def _hang(argv, **kwargs):
    assert kwargs.get("timeout"), "the call must be bounded"
    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])


def test_clipboard_copy_reports_false_when_clip_hangs(monkeypatch, caplog):
    monkeypatch.setattr(tray.sys, "platform", "win32")
    monkeypatch.setattr(tray.subprocess, "run", _hang)
    with caplog.at_level(logging.WARNING):
        assert tray._clipboard_copy("https://example.invalid") is False
    assert any("clip did not finish" in r.getMessage() for r in caplog.records)
