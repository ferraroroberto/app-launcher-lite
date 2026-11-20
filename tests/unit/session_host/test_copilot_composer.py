"""Copilot's composer is tracked with the keys its probe observed.

Each sequence below was typed into a throwaway Copilot CLI 1.0.114 and its
composer read off the screen; the tracker must agree with what was seen, and
read anything the probe could not settle as unknown, never as empty.
"""

from typing import Optional

import pytest

from src.session_host_composer import composer_tracker_for


def _dirty_after(*writes: str) -> Optional[bool]:
    tracker = composer_tracker_for("copilot")
    assert tracker is not None
    for data in writes:
        tracker.feed(data)
    return tracker.state()[0]


@pytest.mark.parametrize(
    ("writes", "expected"),
    [
        (("hello world",), True),
        (("hello world", "\x7f" * 11), False),
        (("\r",), False),
        (("draft one", "\x15"), False),
        (("draft two", "\x03"), False),
        (("first\\", "\r", "second", "\x15"), True),
        (("alpha\\", "\rbeta", "\x03"), False),
        (("line a\\", "\r"), True),
        (("reply ok please", "\r"), False),
        (("say ok #1", "\r"), None),
        (("draft three", "\x1b"), None),
    ],
    ids=[
        "typed", "backspace-clears", "enter-on-empty", "ctrl-u-clears",
        "ctrl-c-clears-draft", "ctrl-u-keeps-first-line", "ctrl-c-clears-multiline",
        "backslash-enter-is-newline", "enter-submits", "issue-mention-unknown",
        "esc-unknown",
    ],
)
def test_copilot_composer_matches_probe(writes: tuple, expected: Optional[bool]) -> None:
    assert _dirty_after(*writes) is expected
