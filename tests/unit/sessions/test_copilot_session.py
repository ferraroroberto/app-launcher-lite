"""A Copilot session's model, context figure and readiness for ``/model``,
read the way :mod:`src.session_view.copilot_session` reads them.

The screens are Copilot CLI 1.0.114's own rows, captured live in a throwaway
session (120 and 46 columns)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.session_view import copilot_session as cs

RULE = "─" * 120
IDLE = [
    " ● Model changed from GitHub Copilot • GPT-6 Luna (medium) to GitHub Copilot • MAI Code 2 Flash (medium) for this",
    "   session",
    " [⎇ master%]                                Plan: 459/1500 (30% used) · Run on: This computer ctrl+e change environment",
    RULE,
    "❯",
    RULE,
    " ← open sidebar · Interactive · Manual Approval · / commands    · space hold to record MAI Code 2 Flash · Medium · (3%)",
    "", "",
]
WORKING = IDLE[:-3] + [
    " ○ Working ctrl+c interrupt                                                                 GPT-6 Luna · Medium · (11%)",
]
# 46 columns: the status line is cut before its figure.
NARROW = ["─" * 46, "❯", "─" * 46, " Interactive            MAI Code 2 Flash · M…"]
TYPED = IDLE[:4] + ["❯ fix the tests"] + IDLE[5:]
# The /model picker's search row is drawn like the prompt.
PICKER = [
    " ──────────────────────────",
    " ❯  Search models…",
    " ──────────────────────────",
    " ↑/↓ to navigate · ←/→ routing profile · tab context window · shift+tab group: recommended · ctrl+r rescan providers ·",
    " enter to select · esc to cancel",
]


@pytest.mark.parametrize("lines,expected", [
    (IDLE, 3),
    (WORKING, 11),
    (NARROW, None),
    ([], None),
    ([" Plan: 459/1500 (30% used)"], None),
    # The two-row footer: the figure stays on the bottom row.
    ([" Plan: 26.957/35.100 (76% used) · Session: 15.99 AIC used · YOLO", RULE, "> ", RULE,
      " GPT-6 Luna · Extra High · (9%)", ""], 9),
    # The same footer wrapped over two rows: the percent is a row of its own.
    ([" +0 Plan: 27.448/35.100 · Session: 39.74 · YOLO", " -0 (78% used) AIC used", RULE, "> ", RULE,
      " GPT-6 Luna · Extra High", " (9%)", ""], 9),
    # A figure alone on a row with no input box above it is conversation text.
    ([" ● Done.", " (9%)"], None),
    ([RULE, "> ", RULE, " Plan: 27.448/35.100", " (78% used)"], None),
])
def test_context_percent_is_the_status_lines_own_figure(lines, expected):
    assert cs.context_percent(lines) == expected


@pytest.mark.parametrize("lines,fragment", [
    (IDLE, None),
    (NARROW, None),
    (WORKING, "working"),
    (TYPED, "holds text"),
    (PICKER, "menu is open"),
    (IDLE[:3], "isn't at its prompt"),
    ([], "can't see"),
])
def test_switch_refusal(lines, fragment):
    reason = cs.switch_refusal(lines)
    assert (reason is None) if fragment is None else (fragment in reason)


def _event(kind: str, **data) -> str:
    return json.dumps({"type": kind, "data": data}) + "\n"


def test_current_model_is_the_newest_record_and_reads_only_what_was_appended(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    log.write_text(_event("session.start", selectedModel="gpt-6-luna")
                   + _event("user.message", content="hi"), encoding="utf-8")
    assert cs.current_model(log) == "gpt-6-luna"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(_event("session.model_change", newModel="mai-code-2-flash"))
        # A torn line is left for the next read.
        fh.write('{"type": "session.model_change", "data": {"newModel": "au')
    assert cs.current_model(log) == "mai-code-2-flash"
    with log.open("a", encoding="utf-8") as fh:
        fh.write('to"}}\n')
    assert cs.current_model(log) == "auto"
    assert cs.current_model(tmp_path / "missing.jsonl") is None
    assert cs.current_model(None) is None


def test_event_log_lookups_are_cached(monkeypatch, tmp_path: Path):
    calls = []
    found = {"s-hit": tmp_path / "events.jsonl", "s-miss": None}

    def fake_find(session):
        calls.append(session["session_id"])
        return found[session["session_id"]]

    monkeypatch.setattr(cs.transcript_locate, "find_copilot_transcript", fake_find)
    cs._logs.clear()
    for _ in range(3):
        assert cs.event_log({"session_id": "s-hit"}) == found["s-hit"]
        assert cs.event_log({"session_id": "s-miss"}) is None
    assert calls == ["s-hit", "s-miss"]
    # A switch looks again past a cached miss: the first prompt may just have written it.
    cs.event_log({"session_id": "s-miss"}, fresh=True)
    cs.event_log({"session_id": "s-hit"}, fresh=True)
    assert calls == ["s-hit", "s-miss", "s-miss"]
