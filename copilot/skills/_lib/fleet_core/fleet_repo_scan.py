"""Shared "enumerate the fleet + resolve a repo's committed default branch"
logic behind /config-map's and /system-map's `build_data.py`,
plus the filesystem-crawl half (`is_fleet_repo` / `iter_fleet_repos`) that
/audit-fleet's and /design-sweep's scanners share.

It also owns reading the fleet list itself. `hooks/projects.toml` is
machine-local and gitignored (a tracked `hooks/projects.toml.example` carries
the committed shape), and its `[global] architecture_ignore` array is the
fleet-scope switch: a repo listed there is out of every fleet-wide run. See
`docs/fleet-scope.md`.

Both skills derive their per-repo data from the same `hooks/projects.toml`
membership list, and both need each repo's committed default branch (to read
state from `origin/HEAD`, independent of whatever branch the repo happens to
be checked out on) — before this module existed, `config-map/build_data.py`
and `system-map/build_data.py` each carried a copy-pasted `fleet_repos`/
`_default_ref` pair. One shared pair now backs both; each `build_data.py`
keeps a thin same-named wrapper (its own `PROJECTS_TOML` default) so its call
sites and public API are unchanged.

stdlib + the `git` CLI only (via `skills/_lib/git_run`).
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import git_run  # noqa: E402

# skills/_lib/fleet_core/fleet_repo_scan.py -> repo root is two levels up.
_DEFAULT_PROJECTS_TOML = Path(__file__).resolve().parents[3] / "hooks" / "projects.toml"
# Redirects fleet-run reads only, never the hooks' registry (`CLAUDE_HOOKS_PROJECTS_TOML`):
# suites that build their own temp registry tree must not be pulled onto another file.
# Only the acceptance run sets it, to the tracked example; an unset variable never
# falls back, so a fleet run with no list still stops.
PROJECTS_TOML_ENV_VAR = "FLEET_PROJECTS_TOML"


def default_projects_toml() -> Path:
    """The fleet list a caller reads when it names none: the env override, else the local file."""
    override = os.environ.get(PROJECTS_TOML_ENV_VAR)
    return Path(override) if override else _DEFAULT_PROJECTS_TOML


class ProjectsTomlMissing(FileNotFoundError):
    """The machine-local fleet list is absent.

    A fleet-wide run with no list must stop, never fall back to "no repos" or
    "every repo"; the message names the tracked template to copy from.
    """

    def __init__(self, path: Path) -> None:
        example = path.with_name(path.name + ".example")
        super().__init__(
            f"{path} is missing. It is machine-local and gitignored: copy "
            f"{example.name} to {path.name} beside it (install.ps1 seeds it on a fresh clone)."
        )
        self.path = path


def load_projects_toml(projects_toml: Optional[Path] = None) -> dict:
    """Parse the fleet list, raising `ProjectsTomlMissing` when it is absent."""
    path = Path(projects_toml) if projects_toml else default_projects_toml()
    if not path.is_file():
        raise ProjectsTomlMissing(path)
    return tomllib.loads(path.read_text(encoding="utf-8"))


def ignored_repos(projects_toml: Optional[Path] = None) -> FrozenSet[str]:
    """The repos switched out of fleet-wide runs (`[global] architecture_ignore`)."""
    return _ignored(load_projects_toml(projects_toml))


def _ignored(toml: dict) -> FrozenSet[str]:
    return frozenset(toml.get("global", {}).get("architecture_ignore", []))


def fleet_repo_tables(projects_toml: Optional[Path] = None) -> Dict[str, dict]:
    """Return ``{repo_name: <its whole projects.toml table>}``.

    The membership half of `fleet_repos` without the projection down to a
    path: callers that need a *declaration* rather than a location — is there
    a `tray_cmd`, which `webapp_port`, which `api_version_path`
    (`service_probe.declared_service`) — read it here
    instead of re-parsing the file. Deliberately **not** filtered by
    ``[global] architecture_ignore``: that list says a repo is out of scope
    for the architecture map, never that it has no long-running process.
    """
    return _tables(load_projects_toml(projects_toml))


def _tables(toml: dict) -> Dict[str, dict]:
    return {
        name: tbl
        for name, tbl in toml.items()
        if name != "global" and isinstance(tbl, dict) and "cwd_prefix" in tbl
    }


def fleet_repos(
    projects_toml: Optional[Path] = None, *, include_ignored: bool = False
) -> Dict[str, Path]:
    """Return ``{repo_name: repo_dir}`` for the fleet in scope.

    Fleet = every ``[<name>]`` table carrying a ``cwd_prefix`` − the
    ``[global] architecture_ignore`` switch. ``include_ignored=True`` keeps
    the switched-off repos, for a caller resolving a repo the user named.
    """
    toml = load_projects_toml(projects_toml)
    ignore = frozenset() if include_ignored else _ignored(toml)
    return {
        name: Path(tbl["cwd_prefix"])
        for name, tbl in _tables(toml).items()
        if name not in ignore
    }


def is_linked_worktree(path: str | Path) -> bool:
    """True if `path` is a linked git worktree rather than a real checkout.

    `git worktree add` writes a `.git` **file** containing a `gitdir:` pointer,
    where a real checkout has a `.git` **directory**. That one byte-level fact
    is the only reliable discriminator, and every fleet sweep over
    `E:/automation` needs it: a sibling `<repo>-wt-<N>` tree is a full checkout
    holding a byte-identical copy of its primary's `CLAUDE.md`, `.fleet.toml`
    and skills, so counting it double-counts the repo it belongs to.

    Worktrees are created and torn down constantly by `/issue-start`,
    `/cleanup-fleet` and `/cleanup-fleet-all`, so a scan without this guard
    produces different numbers on every run with no file having changed
    (e.g. `/context-audit`'s budget block would count phantom
    tokens from two unrelated sessions' worktrees).

    Named and shared rather than re-inlined: `iter_fleet_repos` below asks the
    stricter question ("is this a real repo *of ours*") via `.git.is_dir()`,
    which excludes worktrees as a side effect; callers that only need to
    exclude worktrees — without also imposing repo-ownership — call this.
    """
    return (Path(path) / ".git").is_file()


def is_fleet_repo(remote_url: str | None) -> bool:
    """True if the remote URL belongs to the `ferraroroberto` GitHub org.

    Matches both the https (`https://github.com/ferraroroberto/x.git`) and
    ssh (`git@github.com:ferraroroberto/x.git`) remote URL forms, via the one
    `owner/repo` regex shared with the five other origin-URL parsers across
    the repo (`git_run.slug_from_url`) instead of its own
    substring check.
    """
    slug = git_run.slug_from_url(remote_url)
    return bool(slug) and slug.split("/", 1)[0] == "ferraroroberto"


def iter_fleet_repos(
    root: str | Path,
    only: str | None = None,
    *,
    include_ignored: bool = False,
    projects_toml: Optional[Path] = None,
) -> Iterator[Path]:
    """Yield every fleet repo directory directly under `root`, in name order.

    A directory qualifies when it is a real repo (`.git` is a **directory** —
    a linked worktree's `.git` is a *file*, and sweeping a sibling `-wt-<N>`
    tree would double-count the repo it belongs to; see `is_linked_worktree`)
    whose `origin` remote is a `ferraroroberto` one. `only` narrows to a
    single repo by directory name.
    A directory whose `origin` can't be read is skipped silently — it isn't a
    fleet repo as far as this crawl can tell.

    A repo switched off in `[global] architecture_ignore` is skipped unless
    `include_ignored` is set or `only` names it: an explicit request runs. An
    undeclared directory is still yielded; the `fleet_membership` acceptance
    check fails it separately.

    `fleet_audit_scan.scan` and `design_sweep_scan.scan` carried this loop
    copy-pasted verbatim, comment and all, which is why the
    worktree guard had to be got right twice.
    """
    ignore = frozenset() if (include_ignored or only) else ignored_repos(projects_toml)
    for d in sorted(Path(root).iterdir()):
        if not d.is_dir():
            continue
        if not (d / ".git").is_dir():
            continue
        if only and d.name != only:
            continue
        if d.name in ignore:
            continue
        try:
            remote = git_run.run_git_checked(["-C", str(d), "remote", "get-url", "origin"])
        except SystemExit:
            continue
        if not is_fleet_repo(remote):
            continue
        yield d


def default_ref(repo_dir: Path) -> Optional[str]:
    """The ref to read a repo's committed state from (its default branch).

    Resolves ``origin/HEAD`` (e.g. ``origin/main``); falls back through common
    default-branch names. Uses remote-tracking refs so the result is
    independent of which branch the repo is checked out on, and needs no
    network. Routed through the shared `git_run.resolve_default_branch_ref`
    resolver; `final_fallback=""` reproduces this helper's
    own pre-existing "give up and return None" contract on top of it.
    """
    ref = git_run.resolve_default_branch_ref(
        repo_dir, candidates=("origin/main", "origin/master", "main", "master"), final_fallback="",
    )
    return ref or None
