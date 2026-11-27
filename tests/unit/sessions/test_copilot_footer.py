"""Every footer layout in ``tests/fixtures/copilot_footers.json`` reads its two
figures: the ones Copilot has painted, shifted ones with the same labels, and
unrecognised ones, which read unknown."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.session_view.copilot_credits import ai_credits_used
from src.session_view.copilot_session import context_percent

CASES = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures" / "copilot_footers.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_a_footer_layout_reads_its_figures(case):
    lines = case["lines"] + ["", ""]
    assert (context_percent(lines), ai_credits_used(lines)) == (case["percent"], case["ai_credits"])
