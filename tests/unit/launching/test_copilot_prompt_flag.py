"""Copilot takes its first prompt through ``-i``, never as a bare positional.

Copilot CLI parses the first positional as a subcommand and exits 1
(``unrecognized subcommand``), so every prompted launch died within seconds.
"""
from __future__ import annotations

import shlex

import pytest

from src.launching.flags import build_copilot_flags, copilot_prompt_flag
from src.webapp_config import WebappConfig


def test_prompt_is_carried_by_dash_i() -> None:
    assert copilot_prompt_flag("/issue-start 7") == '-i "/issue-start 7"'


@pytest.mark.parametrize("prompt", ["/issue-yolo 12 --brief C:/x/b.md", "/weekly-recap"])
def test_built_launch_line_never_ends_in_a_bare_positional(prompt: str) -> None:
    cfg = WebappConfig()
    line = f"{build_copilot_flags(cfg)} {copilot_prompt_flag(prompt)}".strip()
    tokens = shlex.split(line, posix=True)
    assert tokens[-2] == "-i"
    assert tokens[-1] == prompt
    # The prompt token is consumed by -i, so no stray positional remains.
    assert tokens.count(prompt) == 1
