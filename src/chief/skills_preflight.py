"""Where a lane's four issue skills come from, and whether each one is there.

A chief-dispatched or Board-started lane runs ``/issue-<mode>`` and ships with
``/issue-finish``. On a machine whose issue skills live in a repo of its own,
``skills_dir`` names that folder; empty means the skills bundled in the Copilot
folder (``<fleet_config_dir>/skills``). Each skill resolves to exactly one
source, so a name present in both is never counted twice.

Each skill's state is ``found``, ``missing`` (with the path expected) or
``unknown`` (the folder could not be read, so the answer is not established).
Only ``found`` lets a lane start: a start that would hand the agent an
``/issue-start`` it cannot run is refused with the reason instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

ISSUE_SKILLS = ("issue-add", "issue-start", "issue-yolo", "issue-finish")


@dataclass(frozen=True)
class SkillCheck:
    name: str
    state: str  # found | missing | unknown
    source: str  # skills_dir | bundled
    path: str
    reason: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {"name": self.name, "state": self.state, "source": self.source,
                "path": self.path, "reason": self.reason}


def skills_root(skills_dir: str, fleet_config_dir: str) -> tuple[str, Path]:
    """The source name and the folder the four skills resolve against."""
    configured = (skills_dir or "").strip()
    if configured:
        return "skills_dir", Path(configured).expanduser()
    return "bundled", Path(fleet_config_dir) / "skills"


def _check_one(name: str, source: str, root: Path) -> SkillCheck:
    skill_md = root / name / "SKILL.md"
    path = skill_md.as_posix()
    if not root.is_absolute():
        return SkillCheck(name, "unknown", source, path,
                          f"the skills folder is not an absolute path: {root.as_posix()}")
    try:
        if skill_md.is_file():
            return SkillCheck(name, "found", source, path)
        if not root.is_dir():
            return SkillCheck(name, "missing", source, path,
                              f"the skills folder does not exist: {root.as_posix()}")
        return SkillCheck(name, "missing", source, path, f"no {name}/SKILL.md in {root.as_posix()}")
    except OSError as exc:
        return SkillCheck(name, "unknown", source, path, f"could not read {path}: {exc}")


def check_skills(skills_dir: str, fleet_config_dir: str) -> List[SkillCheck]:
    """One check per issue skill, in :data:`ISSUE_SKILLS` order."""
    source, root = skills_root(skills_dir, fleet_config_dir)
    return [_check_one(name, source, root) for name in ISSUE_SKILLS]


def refusal(checks: List[SkillCheck]) -> Optional[str]:
    """Why a lane must not start, naming every skill that is not ``found``; else None."""
    problems = [c for c in checks if c.state != "found"]
    if not problems:
        return None
    parts = [f"{c.state} skill {c.name} ({c.reason or c.path})" for c in problems]
    return "issue skills not ready: " + "; ".join(parts) + \
        ". Set Settings > Chief > Skills folder, or clear it to use the bundled skills."


def payload(skills_dir: str, fleet_config_dir: str) -> Dict[str, object]:
    """The JSON the Settings row and the chief's dispatch read."""
    checks = check_skills(skills_dir, fleet_config_dir)
    source, root = skills_root(skills_dir, fleet_config_dir)
    reason = refusal(checks)
    return {
        "source": source,
        "folder": root.as_posix(),
        "ready": reason is None,
        "refusal": reason,
        "skills": [c.as_dict() for c in checks],
    }


def log_report(skills_dir: str, fleet_config_dir: str) -> Dict[str, object]:
    """Log one line per skill (the chief-start report) and return the payload.

    Reported, not enforced, at chief start: a missing skill refuses each lane start instead."""
    body = payload(skills_dir, fleet_config_dir)
    for item in body["skills"]:  # type: ignore[union-attr]
        if item["state"] == "found":
            logger.info("ℹ️ Issue skill %s found (%s) -> %s", item["name"], item["source"], item["path"])
        else:
            logger.warning("⚠️ Issue skill %s %s (%s) -> %s", item["name"], item["state"],
                           item["source"], item["reason"] or item["path"])
    return body
