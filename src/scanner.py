"""Filesystem scanner for the unified app registry.

Two pieces of discovery share this module:

- ``scan_project_dirs(projects_dir, ignore)`` — lists the **direct child
  directories** of ``projects_dir``, dropping VCS / build noise and any
  directory whose name matches a gitignore-style ignore pattern. Each
  surviving directory becomes a ``coding`` row. There is no scan
  step and no on-disk marker file — the directory listing is the source
  of truth, recomputed live on every request.

- ``scan_app_bats(scan_root)`` — walks ``scan_root`` recursively
  looking at every ``*.bat``, classifies via ``classify_bat``, and
  returns ``(path, kind)`` pairs for kinds ``streamlit``, ``webapp``,
  ``tunnel``, ``tray``.

The two scans run independently — a coding project never collides
with an Apps row because coding rows have no ``bat_path`` (the
launcher launches the agent in the directory directly).
"""

from __future__ import annotations

import configparser
import fnmatch
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import yaml

from src.git_run import run_git

logger = logging.getLogger(__name__)

APPS_SCAN_SKIP_DIRS = frozenset(
    {".venv", "venv", "__pycache__", "node_modules", "certificates", ".git", "old"}
)

# Directories never offered as coding projects, regardless of the
# user's ignore list — VCS metadata, virtualenvs, build caches, IDE dirs.
PROJECT_SCAN_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "__pycache__", "node_modules", ".idea", ".vscode"}
)

# kind constants — used as string literals everywhere else.
KIND_CODING = "coding"
KIND_STREAMLIT = "streamlit"
KIND_WEBAPP = "webapp"
KIND_TUNNEL = "tunnel"
KIND_TRAY = "tray"

# ``coding`` rows are computed live (see registry.py's module
# docstring) and never persisted in apps.json, so it is deliberately
# excluded here — a stray/hand-edited row with that kind must be
# rejected by :func:`src.registry.load_registry`, not silently kept.
VALID_KINDS = frozenset({KIND_STREAMLIT, KIND_WEBAPP, KIND_TUNNEL, KIND_TRAY})


@dataclass(frozen=True)
class ProjectDir:
    """A project directory the Coding tab can launch a coding agent in.

    ``project_dir`` is the directory the agent will be cwd'd into; ``id``
    is a stable slug of its name; ``name`` is the **bare on-disk folder
    name**, shown verbatim on the tile (no prettification — that's the
    Coding-tab tile design from issue #45).
    """

    id: str
    name: str
    project_dir: Path


# ----------------------------------------------------------- pretty names


def pretty_folder_name(folder: Path) -> str:
    parts = [p for p in re.split(r"[_\-\s]+", folder.name) if p]
    if not parts:
        parts = [folder.name]
    return " ".join(p.capitalize() for p in parts)


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return cleaned or "app"


# ----------------------------------------------------------- coding


def dir_ignored(name: str, patterns: Sequence[str]) -> bool:
    """Return ``True`` when directory ``name`` matches any ignore pattern.

    Patterns are gitignore-style and matched case-insensitively against
    the bare directory name: a plain entry matches by name, ``*`` / ``?``
    globs are honoured (e.g. ``*-old`` or ``tmp?``). Since the scan only
    ever looks one level deep, slashes carry no extra meaning.
    """
    lowered = name.lower()
    for pattern in patterns:
        pat = str(pattern).strip().lower()
        if pat and fnmatch.fnmatch(lowered, pat):
            return True
    return False


def scan_project_dirs(
    projects_dir: Path, ignore: Optional[Sequence[str]] = None
) -> List[ProjectDir]:
    """List direct child directories of ``projects_dir`` as launchable rows.

    Always-skips :data:`PROJECT_SCAN_SKIP_DIRS`; additionally drops any
    directory whose name matches an entry in ``ignore`` (see
    :func:`dir_ignored`). Results are sorted by name, case-insensitively.
    """
    if not projects_dir.is_dir():
        logger.warning(f"⚠️ Projects dir does not exist: {projects_dir}")
        return []

    patterns = list(ignore or [])
    results: List[ProjectDir] = []
    for child in projects_dir.iterdir():
        try:
            if not child.is_dir():
                continue
        except OSError:  # broken junction / permission error
            continue
        if child.name in PROJECT_SCAN_SKIP_DIRS:
            continue
        if dir_ignored(child.name, patterns):
            continue
        results.append(
            ProjectDir(
                id=slugify(child.name),
                name=child.name,
                project_dir=child,
            )
        )
    results.sort(key=lambda p: p.name.lower())
    return results


# ------------------------------------------------------------- team-os skills

# A skill's slash-command / folder name must be a safe slug — it is
# interpolated into the launch command line (`copilot … /<name>`), so any
# value that isn't a bare kebab token is rejected outright rather than
# quoted. Directory names are inherently filesystem-safe; this also vets
# the SKILL.md frontmatter `name` before it can reach a shell.
_SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)

# Skill folders whose name starts with these are scaffolding, never real
# skills (`_template`, `_recap`); plus the usual VCS / cache noise.
_SKILLS_SKIP_PREFIX = "_"


@dataclass(frozen=True)
class Skill:
    """One team-os skill the Team OS tab can launch and browse.

    ``id`` is the on-disk folder name — the stable key threaded through
    the API path (``/api/team-os/skills/<id>/…``). ``command`` is the
    slash-command base used at launch (``/journal-daily``); it is the
    frontmatter ``name`` when that is a valid slug, else the folder name,
    and is always validated against :data:`_SKILL_SLUG_RE`. ``name`` is
    the display label; ``description`` the one-paragraph blurb.
    """

    id: str
    name: str
    command: str
    description: str
    skill_dir: Path


def _read_frontmatter(skill_md: Path) -> dict:
    """Parse the leading ``---`` YAML frontmatter block of a SKILL.md.

    Returns ``{}`` for a missing file, no frontmatter, or unparseable
    YAML — the skill still lists, just with folder-name fallbacks.
    """
    try:
        text = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    if not text.lstrip().startswith("---"):
        return {}
    # Strip a leading blank line / BOM, then split on the fence markers.
    body = text.lstrip()
    parts = body.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        logger.debug(f"SKILL.md frontmatter parse failed for {skill_md}: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _first_paragraph(path: Path) -> str:
    """First non-empty, non-heading line of a markdown file, or ``""``."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def skills_dir_for(team_os_dir: Path) -> Path:
    """The skills root inside a team-os checkout (``.claude/skills``) — Copilot reads .claude/skills too."""
    return team_os_dir / ".claude" / "skills"


def scan_skills(team_os_dir: Path) -> List[Skill]:
    """List the team-os skills under ``<team_os_dir>/.claude/skills``.

    Modelled on :func:`scan_project_dirs`: every direct child directory
    whose name does **not** start with ``_`` (scaffolding) and isn't VCS
    noise becomes a :class:`Skill`. ``SKILL.md`` frontmatter supplies the
    slash-command ``name`` and the ``description``; both fall back
    gracefully (folder name, then ``description.md`` first paragraph).
    Results are sorted alphabetically by display name. A skill whose
    folder name and frontmatter name are both invalid slugs is dropped —
    it could not be launched safely anyway.
    """
    skills_root = skills_dir_for(team_os_dir)
    if not skills_root.is_dir():
        logger.warning(f"⚠️ team-os skills dir does not exist: {skills_root}")
        return []

    results: List[Skill] = []
    for child in skills_root.iterdir():
        try:
            if not child.is_dir():
                continue
        except OSError:  # broken junction / permission error
            continue
        folder = child.name
        if folder.startswith(_SKILLS_SKIP_PREFIX):
            continue
        if folder in PROJECT_SCAN_SKIP_DIRS:
            continue

        fm = _read_frontmatter(child / "SKILL.md")
        fm_name = str(fm.get("name") or "").strip()
        # The slash-command: prefer a valid frontmatter name, else the
        # folder name. If neither is a safe slug, skip the skill.
        command = fm_name if _SKILL_SLUG_RE.match(fm_name) else ""
        if not command and _SKILL_SLUG_RE.match(folder):
            command = folder
        if not command:
            logger.warning(
                f"⚠️ skipping team-os skill with unsafe name: {folder!r}"
            )
            continue

        description = str(fm.get("description") or "").strip()
        if not description:
            description = _first_paragraph(child / "description.md")

        results.append(
            Skill(
                id=folder,
                name=fm_name or folder,
                command=command,
                description=description,
                skill_dir=child,
            )
        )
    results.sort(key=lambda s: s.name.lower())
    return results


# -------------------------------------------------------------- repo link


def _normalise_remote_url(url: str) -> Optional[str]:
    """Turn a git remote URL into a browsable https web URL, or ``None``.

    Host-agnostic (Phase 5 — the fork's Board reads GitLab, projects may sit
    on github.com, gitlab.com, or a self-hosted GitLab): handles the three
    common remote forms — SCP-style SSH (``git@host:path.git``), HTTPS
    (``https://host/path.git``), and the explicit ``ssh://git@host/path``
    form — as ``https://<host>/<path>``. A trailing ``.git`` and
    surrounding slashes are stripped.
    """
    scp = re.match(r"[^@/]+@([^:/\s]+):(.+)", url)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        proto = re.match(
            r"(?:https?|ssh|git)://(?:[^@/]+@)?([^:/\s]+)(?::\d+)?/(.+)",
            url,
            re.IGNORECASE,
        )
        if not proto:
            return None
        host, path = proto.group(1), proto.group(2)

    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    return f"https://{host}/{path}" if host and path else None


def repo_web_url(project_dir: Path) -> Optional[str]:
    """Resolve the browsable repo URL for a project from its ``origin`` remote.

    Reads ``<project_dir>/.git/config`` directly — no ``git`` subprocess
    — and normalises the ``origin`` remote URL via
    :func:`_normalise_remote_url`. Returns ``None`` when the folder has
    no ``.git/config``, no ``origin`` remote, or an unparseable remote.
    """
    config_path = project_dir / ".git" / "config"
    if not config_path.is_file():
        return None

    # strict=False: git's config format allows a key to repeat within a
    # section (multivar), and tools like VS Code do write duplicates
    # (e.g. vscode-merge-base) — configparser's default strict mode
    # rejects those. We only read remote.origin.url, where last wins.
    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(config_path, encoding="utf-8")
    except (OSError, configparser.Error) as exc:
        logger.warning(f"⚠️  Could not read {config_path} ({exc})")
        return None

    raw = parser.get('remote "origin"', "url", fallback=None)
    if not raw:
        return None
    return _normalise_remote_url(raw.strip())


def repo_issues_url(web_url: Optional[str]) -> Optional[str]:
    """The repo's issues page for a :func:`repo_web_url` result.

    Server-side host check (Phase 5): GitHub keeps the plain ``/issues``
    path; every other host is assumed GitLab-shaped and gets the
    ``/-/issues`` dash-namespace path. The client uses the result verbatim.
    """
    if not web_url:
        return None
    host = urlparse(web_url).hostname or ""
    if host.lower() == "github.com":
        return f"{web_url}/issues"
    return f"{web_url}/-/issues"


# ------------------------------------------------------------- git status


@dataclass(frozen=True)
class GitStatus:
    """The git state of one project, for the Coding tab's on-demand flags.

    ``is_git`` is ``False`` for a folder with no usable git repo (or when
    ``git`` isn't on PATH). ``branch`` is the current branch name, or
    ``None`` when detached / unborn. ``default_branch`` is the repo's
    resolved default (``origin/HEAD`` → ``main`` → ``master``), or
    ``None`` when it can't be determined. ``dirty`` is ``True`` when the
    working tree has any uncommitted or untracked changes.
    """

    is_git: bool
    branch: Optional[str]
    default_branch: Optional[str]
    dirty: bool

    @property
    def on_default_branch(self) -> bool:
        """``True`` on the default branch — and whenever the branch or the
        default can't be pinned down, so the "off main" (yellow) cue only
        fires on a *known* non-default branch, never on an ambiguous one."""
        if self.branch is None or self.default_branch is None:
            return True
        return self.branch == self.default_branch

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_git": self.is_git,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "on_default_branch": self.on_default_branch,
            "dirty": self.dirty,
        }


_NOT_GIT = GitStatus(is_git=False, branch=None, default_branch=None, dirty=False)


#: Wider than :data:`src.git_run.DEFAULT_TIMEOUT_S` — this walks whatever
#: project directory the user pointed the Coding tab at, which can be a large
#: repo on a cold filesystem cache.
_GIT_TIMEOUT_S = 10.0


def _run_git(project_dir: Path, args: Sequence[str]) -> Optional[str]:
    """Run ``git -C <project_dir> <args>`` and return stripped stdout.

    Returns ``None`` on any non-zero exit, missing ``git`` binary, or
    timeout — callers treat ``None`` as "couldn't determine". The invocation
    contract itself lives in :func:`src.git_run.run_git`; this wrapper only
    flattens "ran but said no" and "couldn't run" into the single ``None``
    its callers here already expect.
    """
    proc = run_git(project_dir, args, timeout=_GIT_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _default_branch(project_dir: Path) -> Optional[str]:
    """Resolve the repo's default branch: ``origin/HEAD`` → ``main`` → ``master``.

    Prefers the symbolic ``origin/HEAD`` ref (set on clone); falls back to
    whichever of ``main`` / ``master`` exists as a local branch. ``None``
    when none of these resolve.
    """
    head = _run_git(
        project_dir, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]
    )
    if head:
        # "origin/main" → "main"
        return head.split("/", 1)[1] if "/" in head else head
    for candidate in ("main", "master"):
        if _run_git(
            project_dir, ["rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}"]
        ):
            return candidate
    return None


def git_status(project_dir: Path) -> GitStatus:
    """Branch + clean/dirty for one project directory.

    Unlike :func:`repo_web_url` (a plain ``.git/config`` read), this
    shells out to ``git`` — ``status --porcelain=v2 --branch`` for the
    current branch and dirty flag in one call, then a default-branch
    resolve. That subprocess cost is why the Coding tab runs this only
    on demand, never on render or poll. Non-git folders and any git
    failure return :data:`_NOT_GIT`.
    """
    if not (project_dir / ".git").exists():
        return _NOT_GIT

    out = _run_git(project_dir, ["status", "--porcelain=v2", "--branch"])
    if out is None:
        return _NOT_GIT

    branch: Optional[str] = None
    dirty = False
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head ") :].strip()
            branch = None if head == "(detached)" else head
        elif line and not line.startswith("#"):
            # Any non-header line is a changed / untracked entry.
            dirty = True

    return GitStatus(
        is_git=True,
        branch=branch,
        default_branch=_default_branch(project_dir),
        dirty=dirty,
    )


# ----------------------------------------------------------- apps (bats)


def classify_bat(bat_path: Path) -> Optional[str]:
    """Return ``"tray"`` | ``"streamlit"`` | ``"webapp"`` | ``"tunnel"`` | ``None``.

    Classification is mutually exclusive — the first match wins:

    * ``tray`` — filename stem is ``tray`` AND the body references
      ``tray_lifecycle.ps1`` (issue #456), the one shared marker every
      fleet ``tray.bat`` carries (project-scaffolding's tray.bat.template).
      Checked first: a tray.bat never also matches the other signatures.
    * ``streamlit`` — body contains ``streamlit run``. Bats that *also*
      embed ``cloudflared tunnel`` inline (e.g. hybrid ``launch_server.bat``)
      stay in this bucket; they don't write a URL file we can surface.
    * ``tunnel`` — filename stem contains ``tunnel`` AND body references
      ``uvicorn`` / ``run_tunnel`` / ``cloudflared``. These are the
      only bats we surface a tunnel URL for.
    * ``webapp`` — body runs ``uvicorn`` (or imports ``app.webapp.server``).
    """
    try:
        text = bat_path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return None
    stem = bat_path.stem.lower()
    if stem == "tray" and "tray_lifecycle.ps1" in text:
        return KIND_TRAY
    if "streamlit run" in text:
        return KIND_STREAMLIT
    has_tunnel_signal = any(
        token in text for token in ("uvicorn", "run_tunnel", "cloudflared")
    )
    if "tunnel" in stem and has_tunnel_signal:
        return KIND_TUNNEL
    if (
        "uvicorn" in text
        or "app.webapp.server" in text
        or "app/webapp/server" in text
    ):
        return KIND_WEBAPP
    return None


def scan_app_bats(scan_root: Path) -> List[Tuple[Path, str]]:
    """Recursively scan ``scan_root``, returning ``(path, kind)`` pairs.

    Skips ``APPS_SCAN_SKIP_DIRS`` and unclassifiable bats. The skip is
    applied by pruning ``dirnames`` during the walk — ``.venv`` /
    ``node_modules`` / ``__pycache__`` are never descended into, which is
    the whole reason this scan is fast on a tree of sibling repos.
    """
    if not scan_root.is_dir():
        logger.warning(f"⚠️ Apps scan root does not exist: {scan_root}")
        return []

    found: List[Tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(scan_root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in APPS_SCAN_SKIP_DIRS]
        for filename in filenames:
            if not filename.lower().endswith(".bat"):
                continue
            bat = Path(dirpath) / filename
            kind = classify_bat(bat)
            if kind is not None:
                found.append((bat, kind))
    found.sort(key=lambda pair: pair[0])
    return found


def app_id_from_path(bat_path: Path, scan_root: Path) -> str:
    """Stable id derived from the bat's path relative to ``scan_root``."""
    try:
        rel = bat_path.resolve().relative_to(scan_root)
    except ValueError:
        rel = Path(bat_path.name)
    return slugify(str(rel.with_suffix("")))


def tunnel_url_for(bat_path: Path) -> Optional[str]:
    """Resolve a tunnel app's public URL.

    Prefers ``<bat.parent>/webapp/last_tunnel_url.txt`` — written at
    runtime, and includes the app's ``?token=`` when it has one. Falls
    back to the ingress hostname statically configured in
    ``<bat.parent>/webapp/cloudflared.yml``, so a sibling whose tray
    never writes the URL file (older template versions) still surfaces
    its named-tunnel URL.
    """
    webapp_dir = bat_path.parent / "webapp"
    try:
        text = (webapp_dir / "last_tunnel_url.txt").read_text(
            encoding="utf-8"
        ).strip()
        if text:
            return text
    except (OSError, UnicodeDecodeError):
        pass
    return _tunnel_url_from_cloudflared_yml(webapp_dir / "cloudflared.yml")


def _tunnel_url_from_cloudflared_yml(config_path: Path) -> Optional[str]:
    """First ``ingress[].hostname`` in a cloudflared config → ``https://<host>``."""
    if not config_path.is_file():
        return None
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        logger.debug(f"cloudflared.yml parse failed for {config_path}: {exc}")
        return None
    for entry in data.get("ingress") or []:
        if isinstance(entry, dict) and entry.get("hostname"):
            return f"https://{str(entry['hostname']).strip()}"
    return None
