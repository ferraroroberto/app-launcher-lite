"""The chief skill's rules for a lane that stops: the chief steers it on, the owner never has to."""

from __future__ import annotations

from pathlib import Path

SKILL = Path(__file__).resolve().parents[3] / "copilot" / ".github" / "skills" / "chief" / "SKILL.md"


def _turn_end_row() -> str:
    [row] = [line for line in SKILL.read_text(encoding="utf-8").splitlines() if line.startswith("| `kind=turn-end`")]
    return row


def test_a_blocked_turn_end_is_steered_on_by_the_chief() -> None:
    row = _turn_end_row()
    assert "blocked" in row and "say <sid>" in row and "--verify" in row
    assert "plan ask" in row and "real decision" in row
    assert 'never has to type "go on"' in row


def test_the_read_only_rail_lets_the_chief_steer_its_own_lanes() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "steering it on after a blocked turn end needs no new one" in text


def test_the_skill_does_not_claim_the_doorbell_never_rings() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "does not ring a Copilot chief" not in text
    assert "doorbell wakes you" in text
