"""Where the four issue skills resolve, and found / missing / unknown for each."""

from __future__ import annotations

from pathlib import Path

from src.chief import skills_preflight as sp


def _skills(root: Path, *names: str) -> Path:
    for name in names:
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    return root


def test_empty_skills_dir_resolves_to_the_bundled_copilot_skills(tmp_path: Path) -> None:
    copilot = tmp_path / "copilot"
    _skills(copilot / "skills", *sp.ISSUE_SKILLS)
    body = sp.payload("", str(copilot))
    assert body["ready"] is True and body["refusal"] is None
    assert body["source"] == "bundled"
    assert [s["state"] for s in body["skills"]] == ["found"] * 4
    assert {s["source"] for s in body["skills"]} == {"bundled"}


def test_a_set_skills_dir_wins_and_names_the_one_missing_skill(tmp_path: Path) -> None:
    own = _skills(tmp_path / "own", "issue-add", "issue-yolo", "issue-finish")
    # A bundled copy of the missing name never stands in for it: one source per skill.
    _skills(tmp_path / "copilot" / "skills", *sp.ISSUE_SKILLS)
    checks = sp.check_skills(str(own), str(tmp_path / "copilot"))
    states = {c.name: (c.state, c.source) for c in checks}
    assert states["issue-start"] == ("missing", "skills_dir")
    assert states["issue-add"] == ("found", "skills_dir")
    reason = sp.refusal(checks)
    assert reason and "missing skill issue-start" in reason
    assert "issue-add" not in reason


def test_a_skills_dir_that_does_not_exist_says_so(tmp_path: Path) -> None:
    checks = sp.check_skills(str(tmp_path / "nowhere"), str(tmp_path))
    assert {c.state for c in checks} == {"missing"}
    assert "does not exist" in checks[0].reason


def test_a_relative_skills_dir_is_unknown_never_found(tmp_path: Path) -> None:
    checks = sp.check_skills("relative/skills", str(tmp_path))
    assert {c.state for c in checks} == {"unknown"}
    assert "unknown skill issue-start" in (sp.refusal(checks) or "")


def test_an_unreadable_folder_is_unknown(tmp_path: Path, monkeypatch) -> None:
    def boom(self: Path) -> bool:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "is_file", boom)
    checks = sp.check_skills(str(tmp_path), str(tmp_path))
    assert {c.state for c in checks} == {"unknown"}
    assert "denied" in checks[0].reason
