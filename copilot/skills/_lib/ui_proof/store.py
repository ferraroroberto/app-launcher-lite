"""The per-repo `.proof/` store: before and after captures, one folder per merge request, an index.

Layout, rooted at the checkout the command runs in, so a lane never writes outside its own tree:

  .proof/_branch/<branch>/   captures taken before the merge request has a number
  .proof/<MR>/               before-*.png, after-*.png, proof.json, index.html
  .proof/index.json          {"<MR>": {title, issue, merge_sha, utc, branch}}

A worktree lane's report reaches the primary checkout's `.proof/` only through
`publish`, at merge, so it outlives the worktree. `.proof/` is never tracked:
it holds screenshots of a running app.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import git_run
from fleet_core import hooks_state, runtime_files

from ui_proof import proof

DIR_NAME = ".proof"
BRANCH_DIR = "_branch"
INDEX_NAME = "index.json"
DATA_NAME = "proof.json"
PAGE_NAME = "index.html"
PHASES = ("before", "after")
LEG_ORDER = ("phone-light", "phone-dark", "desktop-light", "desktop-dark")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_out(repo: Path, *args: str) -> Optional[str]:
    res = git_run.run_git(["-C", str(repo), *args], timeout=30)
    return res.stdout.strip() if res.returncode == 0 else None


def proof_root(repo: Path) -> Optional[Path]:
    """`<this checkout>/.proof`: the worktree's own tree when `repo` is a worktree."""
    top = _git_out(repo, "rev-parse", "--show-toplevel")
    return Path(top) / DIR_NAME if top else None


def _common_dir(repo: Path) -> Optional[Path]:
    common = _git_out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common) if common else None


def primary_root(repo: Path) -> Optional[Path]:
    """`<primary checkout>/.proof`, found through the shared git dir so a worktree resolves to its primary."""
    common = _common_dir(repo)
    return common.parent / DIR_NAME if common else None


def current_branch(repo: Path) -> Optional[str]:
    return _git_out(repo, "branch", "--show-current") or None


def safe_name(text: str) -> str:
    return _SAFE.sub("-", text).strip("-") or "unnamed"


def _is_ignored(repo: Path) -> Optional[bool]:
    """Whether git ignores `.proof/` in `repo`; `None` when git could not answer."""
    res = git_run.run_git(["-C", str(repo), "check-ignore", "-q", "--no-index", f"{DIR_NAME}/probe.png"], timeout=30)
    return {0: True, 1: False}.get(res.returncode)


def ensure_ignored(repo: Path) -> Optional[str]:
    """Make sure git ignores `.proof/` before anything is written there; the reason when it cannot.

    A repo that does not ignore it gets a line in `info/exclude`, which is local to
    this clone, covers its worktrees and changes no tracked file.
    """
    tracked = _git_out(repo, "ls-files", "--", DIR_NAME)
    ignored = _is_ignored(repo)
    common = _common_dir(repo)
    if tracked is None or ignored is None or common is None:
        return "git could not say whether .proof/ is ignored and untracked"
    if tracked:
        return f"{DIR_NAME}/ holds tracked files; `git rm -r --cached {DIR_NAME}` first"
    if ignored:
        return None
    exclude = common / "info" / "exclude"
    try:
        old = exclude.read_bytes() if exclude.is_file() else b""
        runtime_files.make_dirs(exclude.parent, parents=True, exist_ok=True)
        gap = "" if not old or old.endswith(b"\n") else "\n"
        # Appended, so the owner's own lines and their line endings stay as they are.
        with runtime_files.open_for_write(exclude, "a", encoding="utf-8") as handle:
            handle.write(f"{gap}{DIR_NAME}/\n")
    except OSError as exc:
        return f"could not add {DIR_NAME}/ to {exclude}: {type(exc).__name__}"
    if _is_ignored(repo) is not True:
        return f"{DIR_NAME}/ is still not ignored; a `!` rule in a .gitignore re-includes it"
    return None


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    hooks_state.atomic_write_text(path, json.dumps(data, indent=2))


def parse_images(pairs: List[str]) -> List[dict]:
    """`["<state>:<viewport>-<theme>=<png>", ...]` -> manifest-shaped entries without lint findings."""
    entries = []
    for pair in pairs:
        head, sep, path = pair.partition("=")
        state, sep2, leg = head.rpartition(":")
        viewport, sep3, theme = leg.partition("-")
        if not (sep and sep2 and sep3 and state and viewport and theme and path):
            raise ValueError(f"--image needs '<state>:<viewport>-<theme>=<png>', got {pair!r}")
        entries.append({"state": state, "viewport": viewport, "theme": theme, "screenshot": path})
    return entries


def _leg_key(entry: dict) -> str:
    return f"{entry.get('state')} {entry.get('viewport')}-{entry.get('theme')}"


def capture(repo: Path, phase: str, manifest: Optional[Path], images: List[dict]) -> Tuple[bool, str]:
    """Copy one phase's screenshots into this branch's staging folder; `(ok, CAPTURED=... line)`."""
    root, branch = proof_root(repo), current_branch(repo)
    if root is None or branch is None:
        return False, "CAPTURED=none reason=not a git checkout on a named branch"
    entries: List[dict] = list(images)
    if manifest is not None:
        loaded = proof.load_manifest(manifest)
        if loaded is None:
            return False, f"CAPTURED=none reason=no readable manifest at {manifest}"
        entries += loaded
    if not entries:
        return False, "CAPTURED=none reason=give --manifest and/or --image"
    refused = ensure_ignored(repo)
    if refused:
        return False, f"CAPTURED=none reason={refused}"
    folder = root / BRANCH_DIR / safe_name(branch)
    runtime_files.make_dirs(folder, parents=True, exist_ok=True)
    data = _load_json(folder / DATA_NAME)
    legs: Dict[str, dict] = {_leg_key(e): e for e in data.get(phase, [])}
    missing = []
    for e in entries:
        name = (f"{phase}-{safe_name(str(e.get('state')))}-"
                f"{safe_name(str(e.get('viewport')))}-{safe_name(str(e.get('theme')))}.png")
        try:
            runtime_files.copy_file(str(e.get("screenshot", "")), folder / name)
        except OSError:
            missing.append(_leg_key(e))
            continue
        row = {"state": e.get("state"), "viewport": e.get("viewport"), "theme": e.get("theme"), "file": name}
        if "findings" in e:
            row["findings"] = e.get("findings") or []
        legs[_leg_key(row)] = row
    data.update({"branch": branch, phase: list(legs.values())})
    if phase == "after":
        data["after_tree"] = proof.tree_of(repo)[0]
    _write_json(folder / DATA_NAME, data)
    line = f"CAPTURED={phase} legs={len(entries) - len(missing)} folder={folder}"
    if missing:
        line += " missing_screenshots=" + ",".join(missing)
    return not missing, line


def _merge_into(src: Path, dst: Path) -> None:
    """Move staging captures into the merge request's folder; a re-capture overwrites the same leg."""
    runtime_files.make_dirs(dst, parents=True, exist_ok=True)
    incoming, existing = _load_json(src / DATA_NAME), _load_json(dst / DATA_NAME)
    for item in src.iterdir():
        if item.name != DATA_NAME:
            runtime_files.move(item, dst / item.name)
    for phase in PHASES:
        rows = {_leg_key(e): e for e in existing.get(phase, [])}
        rows.update({_leg_key(e): e for e in incoming.get(phase, [])})
        existing[phase] = list(rows.values())
    if "after_tree" in incoming:
        existing["after_tree"] = incoming["after_tree"]
    existing["branch"] = incoming.get("branch") or existing.get("branch")
    _write_json(dst / DATA_NAME, existing)
    shutil.rmtree(src)


def prepare_mr_folder(repo: Path, mr: int) -> Optional[Path]:
    """`.proof/<MR>/`, absorbing this branch's staging captures first."""
    root = proof_root(repo)
    if root is None:
        return None
    folder = root / str(mr)
    branch = current_branch(repo)
    staging = root / BRANCH_DIR / safe_name(branch) if branch else None
    if staging is not None and staging.is_dir():
        _merge_into(staging, folder)
    return folder


def load_data(folder: Path) -> dict:
    return _load_json(folder / DATA_NAME)


def save_data(folder: Path, data: dict) -> None:
    _write_json(folder / DATA_NAME, data)


def update_index(root: Path, mr: int, row: dict) -> Optional[str]:
    """Add or update `index.json`'s row for `mr`; the reason when the file cannot be read.

    An index that does not parse is left as it is: rewriting it would drop every other report's row.
    """
    path = root / INDEX_NAME
    index: dict = {}
    if path.exists():
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return f"{path} is unreadable; repair or delete it"
        if not isinstance(index, dict):
            return f"{path} is not a JSON object; repair or delete it"
    index[str(mr)] = {**(index.get(str(mr)) or {}), **{k: v for k, v in row.items() if v not in (None, "")}}
    _write_json(path, index)
    return None


def publish(repo: Path, mr: int) -> Tuple[bool, str]:
    """Copy this checkout's `.proof/<MR>/` and index row into the primary's; `(ok, PUBLISHED=... line)`.

    Keyed by merge request, so the copy replaces the primary's folder whole and a re-run changes nothing.
    """
    local, primary = proof_root(repo), primary_root(repo)
    if local is None or primary is None:
        return False, "PUBLISHED=none reason=not a git checkout"
    src = local / str(mr)
    if not (src / PAGE_NAME).is_file():
        return False, f"PUBLISHED=none reason=no .proof/{mr}/index.html in this checkout"
    if local.resolve() == primary.resolve():
        return True, f"PUBLISHED={src} same_checkout=yes"
    runtime_files.make_dirs(primary, parents=True, exist_ok=True)
    incoming = primary / f".{mr}.incoming"
    shutil.rmtree(incoming, ignore_errors=True)
    runtime_files.copy_tree(src, incoming)
    shutil.rmtree(primary / str(mr), ignore_errors=True)
    runtime_files.move(incoming, primary / str(mr))
    row = _load_json(local / INDEX_NAME).get(str(mr))
    reason = update_index(primary, mr, row if isinstance(row, dict) else {})
    if reason:
        return False, f"PUBLISHED=none reason={reason}"
    return True, f"PUBLISHED={primary / str(mr)}"
