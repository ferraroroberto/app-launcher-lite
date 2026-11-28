"""The link rows saved in the webapp config's ``team_mirrors`` list, and the work identity.

A row is ``{project, team_url, branch, leave_out}``: ``project`` is a folder
name under ``projects_dir``, ``leave_out`` repo-relative paths (a folder takes
everything under it) that never reach the team repo. Loading is lenient, so a
hand edit drops one row and never the config; saving goes through
:func:`checked_link`, which refuses a bad row with the field it is about.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from src.git_utils import run_git

logger = logging.getLogger(__name__)

DEFAULT_BRANCH = "main"

# git's own ref-name rules, narrowed to what a team main branch is ever called.
_BRANCH_RE = re.compile(r"^(?!.*\.\.)(?!/)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![./])$")
# A remote git understands: a scheme URL, scp-style `user@host:path`, or a local path.
_SCHEME_RE = re.compile(r"^(https?|ssh|git|file)://[^\s]+$", re.IGNORECASE)
_SCP_RE = re.compile(r"^[\w.-]+@[\w.-]+:[^\s]+$")


class LinkError(ValueError):
    """A link row a save refuses; ``field`` names the input it is about."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class MirrorLink:
    project: str
    team_url: str
    branch: str = DEFAULT_BRANCH
    leave_out: Tuple[str, ...] = ()

    def as_row(self) -> Dict[str, Any]:
        return {
            "project": self.project,
            "team_url": self.team_url,
            "branch": self.branch,
            "leave_out": list(self.leave_out),
        }


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


def _checked_url(raw: Any) -> str:
    url = str(raw or "").strip()
    if not url:
        raise LinkError("team_url", "the team repo URL is empty")
    if not (_SCHEME_RE.match(url) or _SCP_RE.match(url) or Path(url).is_absolute()):
        raise LinkError("team_url", "the team repo URL is not an https, ssh or local path git can reach")
    return url


def _checked_leave_out(raw: Any) -> Tuple[str, ...]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list):
        raise LinkError("leave_out", "leave-out paths must be a list")
    paths: List[str] = []
    for item in raw:
        text = str(item or "").strip().replace("\\", "/").strip("/")
        if not text:
            continue
        parts = PurePosixPath(text).parts
        if ".." in parts or text.startswith(".git/") or text == ".git" or ":" in text:
            raise LinkError("leave_out", f"leave-out path {text!r} must stay inside the repo")
        if text not in paths:
            paths.append(text)
    return tuple(paths)


def checked_link(raw: Any) -> MirrorLink:
    """One row from the browser, validated; :class:`LinkError` names the bad field."""
    if not isinstance(raw, dict):
        raise LinkError("project", "a link must be an object")
    project = str(raw.get("project") or "").strip()
    if not project or "/" in project or "\\" in project or project in (".", ".."):
        raise LinkError("project", "the app must be one folder under the projects folder")
    branch = str(raw.get("branch") or DEFAULT_BRANCH).strip()
    if not _BRANCH_RE.match(branch):
        raise LinkError("branch", f"{branch!r} is not a branch name")
    return MirrorLink(project, _checked_url(raw.get("team_url")), branch, _checked_leave_out(raw.get("leave_out")))


def parse_links(raw: Any) -> Dict[str, MirrorLink]:
    """The config's rows by project; a row that does not validate is logged and dropped."""
    links: Dict[str, MirrorLink] = {}
    for row in raw if isinstance(raw, list) else []:
        try:
            link = checked_link(row)
        except LinkError as exc:
            logger.warning("⚠️ team mirror: a saved link was skipped (%s: %s)", exc.field, exc)
            continue
        links[link.project] = link
    return links


def with_link(raw: Any, link: MirrorLink) -> List[Dict[str, Any]]:
    """The config list with ``link`` saved over any row for the same project."""
    rows = [l.as_row() for l in parse_links(raw).values() if l.project != link.project]
    return sorted([*rows, link.as_row()], key=lambda r: r["project"].lower())


def without_link(raw: Any, project: str) -> List[Dict[str, Any]]:
    return [l.as_row() for l in parse_links(raw).values() if l.project != project]


def work_identity(any_dir: Path) -> Optional[Identity]:
    """This computer's global git identity, the author of every mirror commit; None when unset.

    Only the global config counts: a personal repo's own ``user.email`` must
    never become the team commit's author.
    """
    name = run_git(any_dir, ["config", "--global", "--get", "user.name"]) or ""
    email = run_git(any_dir, ["config", "--global", "--get", "user.email"]) or ""
    if not name.strip() or not email.strip():
        return None
    return Identity(name.strip(), email.strip())
