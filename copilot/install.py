"""Wire this checkout's Copilot side into GitHub Copilot CLI (user scope).

    python copilot\\install.py               install (idempotent; re-run after moving the checkout)
    python copilot\\install.py --uninstall   remove exactly what this installer put in ~/.copilot
    python copilot\\install.py --migrate     move Lite's state out of ~/.copilot/hooks, leave fleet-config-lite, install
    add --what-if to any of them           list every change it would make, and make none

Run it with this checkout's `.venv\\Scripts\\python.exe` (stdlib only, so any Python 3 works).
It runs no PowerShell script, so a machine whose policy only allows signed scripts runs it
as is. `install.ps1` beside it only forwards its switches here.

Step-by-step guide for each case (fresh machine, --migrate, a machine with its own skills
repo, uninstall): copilot/docs/install.md.

What install writes, and where:
1. Hook configs: renders hooks/session-state.template.json and hooks/chief-guard.template.json
   with this checkout's absolute path and a resolved Python executable, into
   ~/.copilot/hooks/app-launcher-lite-<name>.json, and makes sure the state folder
   ~/.copilot/app-launcher-lite-state exists (not on a machine whose state still sits in the
   old hooks/state: --migrate moves that). Nothing else Lite writes goes under hooks/, because
   Copilot CLI loads every .json there as a hook config.
2. Skills: junctions skills/<name> into ~/.copilot/skills/<name>, only into a real folder and
   never over a skill this checkout does not own. When Lite's skills_dir setting
   (config/webapp_config.json) names a folder, the four issue skills come from it instead:
   each is junctioned from that folder into ~/.copilot/skills, and into ~/.claude/skills when
   that is a real folder, and Lite's bundled copies of those four are not installed (earlier
   ones of its own are unlinked). Links it makes into skills_dir are listed in
   ~/.copilot/app-launcher-lite-install.json, which is how --uninstall knows them. An existing
   entry that is not Lite's is kept and reported, and counts as that skill.
3. Config: adds skills_dir (empty) to config/webapp_config.json only when the key is missing,
   after a timestamped backup. No existing value is changed; a file that does not parse is
   reported and left alone. config/config.json is a committed file and is never edited.
4. A fresh-machine checklist (Copilot CLI, glab and its sign-in for the GitLab host, whether
   git reaches GitLab with no prompt, the venv, the session-host on the port
   config/config.json sets), unless --skip-checklist.

What is Lite's own, so what --uninstall may remove:
- a hook config named app-launcher-lite-*.json whose commands run this checkout's
  copilot/hooks scripts;
- a skill junction whose target is under this checkout's copilot/skills (or a dangling one
  ending in copilot/skills/<name>, left by a moved checkout), and a skill copy that carries
  the .app-launcher-lite-managed marker file;
- a skills_dir link recorded in app-launcher-lite-install.json that still points where it
  was recorded.
A re-run reports each item it already has as unchanged and rewrites nothing. Anything else in
~/.copilot is the owner's or another tool's and is never touched. The state folder (the
session-state file, the chief's plan, journal and mailbox) is never deleted; only --migrate
moves it, out of the old hooks/state (install_state.py says how, with a chief live).

--migrate moves that state folder first, then removes the old fleet-config-lite install first (its two hook configs and its skill
junctions), because two session-state hooks would race on the same state file. It lists what
it would remove and asks; --yes answers for it. It also checks Lite's Copilot folder setting
(fleet_config_dir): an explicit path to the old fleet-config-lite folder keeps the chief there,
so it offers to point the setting at this checkout's copilot folder through Lite's own
Settings API (the one field, nothing else).

Shared machine: when ~/.copilot/skills or ~/.agents/skills is a link into another tree, this
PC also runs the full fleet. Then no skill is written, and the session-state hook is rendered
with --lite-sessions-only so it records only sessions app-launcher-lite launched. Any
user-scope target that is itself a link into another tree (the Copilot home, its hooks
folder, a hook config, the skills folder) is never written through.

This installer never edits ~/.claude/settings.json, any agent's trust settings, git config,
or the PowerShell execution policy.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

import install_state

# The webapp's scheme resolver is the same module the chief uses, so the installer follows
# Settings > Serve over exactly as it does.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", "_lib"))
import lite_webapp  # noqa: E402

COPILOT_REPO = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(COPILOT_REPO)
OUR_SKILLS = os.path.join(COPILOT_REPO, "skills")
SKILL_MARKER = ".app-launcher-lite-managed"
WEBAPP_CONFIG = os.path.join(REPO, "config", "webapp_config.json")
ISSUE_SKILLS = ("issue-add", "issue-start", "issue-yolo", "issue-finish")
# Hyphens only in the filename: Copilot CLI 1.0.70 silently ignores hook files with more than
# one dot in the name (verified live).
HOOK_CONFIGS = (
    ("session-state.template.json", "app-launcher-lite-session-state.json", "session_state.py"),
    ("chief-guard.template.json", "app-launcher-lite-chief-guard.json", "chief_guard.py"),
)
# The one command the chief skill runs: a cmd shim with this checkout's absolute paths, so the skill's
# text names no machine path and the call works from any working directory.
CHIEF_LAUNCHER = ("chief-ops.template.cmd", "app-launcher-lite-chief-ops.cmd")
CHIEF_LAUNCHER_MARKER = "REM app-launcher-lite chief launcher"
# What the old two-repo install wrote, and the folder name its skill junctions point into.
OLD_HOOK_FILES = ("fleet-config-lite-session-state.json", "fleet-config-lite-chief-guard.json")
OLD_SKILL_TARGET = re.compile(r"[\\/]fleet-config-lite[^\\/]*[\\/]skills[\\/]", re.IGNORECASE)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HELPER_TIMEOUT_S = 60
HTTP_TIMEOUT_S = 10
SESSION_HOST_TIMEOUT_S = 2


class Paths:
    """The user-scope folders this run reads and writes, from COPILOT_HOME and USERPROFILE."""

    def __init__(self) -> None:
        profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        self.home = os.environ.get("COPILOT_HOME") or os.path.join(profile, ".copilot")
        self.hooks = os.path.join(self.home, "hooks")
        self.state = os.path.join(self.home, install_state.STATE_DIRNAME)
        self.legacy_state = os.path.join(self.hooks, "state")
        self.skills = os.path.join(self.home, "skills")
        self.agents_skills = os.path.join(profile, ".agents", "skills")
        self.claude_skills = os.path.join(profile, ".claude", "skills")
        self.manifest = os.path.join(self.home, "app-launcher-lite-install.json")


# --- links ------------------------------------------------------------------------------------

def is_link(path: str) -> bool:
    """True for any reparse point (junction or symlink), dangling or not."""
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)) or os.path.islink(path)


def link_target(path: str) -> Optional[str]:
    """The link target when ``path`` is a reparse point, else None."""
    if not is_link(path):
        return None
    try:
        target = os.readlink(path)
    except OSError:
        return "(unresolved link)"
    for prefix in ("\\\\?\\", "\\??\\"):
        if target.startswith(prefix):
            target = target[len(prefix):]
    return target or "(unresolved link)"


def starts_under(path: str, root: str) -> bool:
    # The separator keeps a sibling checkout (app-launcher-lite-wt-7) from passing as this one.
    return path.lower().startswith(root.rstrip("\\/").lower() + "\\")


def foreign_link(path: str) -> Optional[str]:
    """The target when ``path`` is a link that does not point into this checkout, else None."""
    target = link_target(path)
    if not target or starts_under(target, REPO):
        return None
    return target


def same_path(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)).rstrip("\\") == os.path.normcase(os.path.abspath(b)).rstrip("\\")
    except (TypeError, ValueError):
        return False


def remove_link(path: str) -> None:
    """Delete the link itself, never what it points at."""
    try:
        os.rmdir(path)
    except NotADirectoryError:
        os.unlink(path)


def make_junction(link: str, target: str) -> None:
    import _winapi  # Windows only; a failure here falls back to a marked copy.
    _winapi.CreateJunction(target, link)


def list_dirs(folder: str) -> List[str]:
    """Every directory entry in ``folder`` (links included, dangling too), sorted by name."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    found = []
    for entry in entries:
        try:
            attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        except OSError:
            continue
        if attrs & getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10) or entry.is_dir(follow_symlinks=False):
            found.append(entry.path)
    return sorted(found, key=lambda p: os.path.basename(p).lower())


def make_dir(path: str) -> None:
    # runtime-write-exempt: user-scope folders under ~/.copilot, never a code folder
    os.makedirs(path, exist_ok=True)


def exists(path: str) -> bool:
    return os.path.lexists(path)


# --- the run ------------------------------------------------------------------------------------

class Installer:
    """One run: every change goes through ``change``, so --what-if lists each one and makes none."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.what_if: bool = args.what_if
        self.yes: bool = args.yes
        self.p = Paths()
        self.python: Optional[str] = None

    def say(self, line: str = "") -> None:
        print(line, flush=True)

    def change(self, what: str, action: Callable[..., object], *args: str) -> None:
        if self.what_if:
            self.say(f"[whatif] {what}")
            return
        action(*args)
        self.say(f"[ok] {what}")

    def confirm(self, question: str) -> bool:
        """One question, one answer; --yes answers it. No line to read (closed stdin) is a refusal."""
        if self.what_if:
            self.say(f"[whatif] would ask: {question}")
            return True
        if self.yes:
            return True
        print(f"{question} [y/N] ", end="", flush=True)
        answer = sys.stdin.readline() if sys.stdin else ""
        if answer == "":
            self.say()
            self.say("[skip] no answer to read (no terminal); re-run with --yes to accept")
            return False
        return re.match(r"^\s*y(es)?\s*$", answer, re.IGNORECASE) is not None

    def remove_skill_entry(self, path: str) -> None:
        """A link goes as a link (a recursive delete could walk into its target); a marked copy goes whole."""
        if is_link(path):
            remove_link(path)
        else:
            shutil.rmtree(path)

    # --- ownership ---------------------------------------------------------------------------

    def our_hook(self, path: str, script: str) -> bool:
        if not os.path.isfile(path):
            return False
        return f"copilot/hooks/{script}" in read_text(path)

    def our_launcher(self, path: str) -> bool:
        return os.path.isfile(path) and CHIEF_LAUNCHER_MARKER in read_text(path)

    def our_skill(self, path: str) -> bool:
        if is_link(path):
            target = link_target(path) or ""
            if not target or target == "(unresolved link)":
                return False
            if starts_under(target, OUR_SKILLS):
                return True
            # A moved checkout leaves a dangling link whose target still ends in our layout.
            return not os.path.exists(target) and re.search(r"[\\/]copilot[\\/]skills[\\/][^\\/]+$", target) is not None
        return os.path.isfile(os.path.join(path, SKILL_MARKER))

    # --- the install record: the skills_dir links this installer made -------------------------

    def read_record(self) -> List[Dict[str, str]]:
        if not os.path.isfile(self.p.manifest):
            return []
        try:
            body = json.loads(read_text(self.p.manifest))
            links = body.get("skill_links") or []
        except (ValueError, AttributeError):
            self.say(f"[unknown] install record did not parse, so no skills_dir link is treated as Lite's -> {self.p.manifest}")
            return []
        return [{"path": str(x["path"]), "target": str(x["target"])}
                for x in links if isinstance(x, dict) and x.get("path") and x.get("target")]

    def save_record(self, links: List[Dict[str, str]], changed: bool) -> None:
        if not changed:
            return
        if not links:
            if os.path.isfile(self.p.manifest):
                self.change(f"install record removed (no skills_dir link left) -> {self.p.manifest}",
                            os.remove, self.p.manifest)
            return
        body = json.dumps({"skill_links": links}, indent=2)
        self.change(f"install record written -> {self.p.manifest}", write_text, self.p.manifest, body)

    def find_recorded(self, links: List[Dict[str, str]], path: str) -> Optional[Dict[str, str]]:
        """The record entry for a link at ``path`` that still points where it was recorded."""
        if not is_link(path):
            return None
        target = link_target(path) or ""
        for link in links:
            if same_path(link["path"], path) and same_path(link["target"], target):
                return link
        return None

    # --- Lite's config ---------------------------------------------------------------------------

    def skills_dir_setting(self) -> Dict[str, str]:
        if not os.path.isfile(WEBAPP_CONFIG):
            return {"state": "empty"}
        try:
            body = json.loads(read_text(WEBAPP_CONFIG))
            value = body.get("skills_dir") if isinstance(body, dict) else None
        except ValueError:
            return {"state": "unknown", "why": "webapp_config.json did not parse"}
        value = ("" if value is None else str(value)).strip()
        if not value:
            return {"state": "empty"}
        if not (os.path.splitdrive(value)[0] or value.startswith(("\\", "/"))):
            return {"state": "unknown", "why": f"skills_dir is not an absolute path: {value}"}
        return {"state": "set", "path": value}

    def add_missing_config_key(self, key: str, json_value: str) -> None:
        """Splice one missing key in after the opening brace, so every other byte stays; backup first."""
        if not os.path.isfile(WEBAPP_CONFIG):
            self.say(f"[ok] no webapp_config.json yet: {key} starts empty, nothing to add")
            return
        raw = read_text(WEBAPP_CONFIG)
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            self.say(f"[unknown] webapp_config.json did not parse as an object: left untouched, {key} not added -> {WEBAPP_CONFIG}")
            return
        if key in parsed:
            self.say(f"[ok] unchanged: {key} is already in webapp_config.json")
            return
        brace = raw.index("{")
        rest = raw[brace + 1:]
        separator = "" if rest.strip().startswith("}") else ","
        updated = raw[:brace + 1] + f'\n  "{key}": {json_value}{separator}' + rest
        try:
            json.loads(updated)
        except ValueError:
            self.say(f"[unknown] adding {key} would not leave valid JSON: left untouched -> {WEBAPP_CONFIG}")
            return
        backup = WEBAPP_CONFIG + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.change(f"backup written -> {backup}", shutil.copy2, WEBAPP_CONFIG, backup)
        self.change(f"key added to webapp_config.json: {key} = {json_value}", write_text, WEBAPP_CONFIG, updated)

    # --- skills_dir links ----------------------------------------------------------------------

    def issue_skill_dirs(self) -> List[str]:
        """~/.copilot/skills, plus ~/.claude/skills when it is a real folder; a link is never written through."""
        dirs = [self.p.skills]
        claude = self.p.claude_skills
        if is_link(claude):
            self.say(f"[skip] {claude} is a link into another tree, nothing written inside it ({link_target(claude)})")
        elif exists(claude):
            dirs.append(claude)
        return dirs

    def remove_recorded_links(self, folder: str, links: List[Dict[str, str]]) -> List[Dict[str, str]]:
        kept = []
        for link in links:
            if not starts_under(link["path"], folder):
                kept.append(link)
                continue
            if self.find_recorded(links, link["path"]):
                path = link["path"]
                self.change(f"skills_dir link removed -> {path}", remove_link, path)
            elif exists(link["path"]):
                self.say(f"[ok] kept (not Lite's: no longer the link it recorded) -> {link['path']}")
        return kept

    def sync_issue_skill_links(self, folder: str, source: str, links: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Link the four issue skills from skills_dir into ``folder``; returns the updated record."""
        result = list(links)
        for name in ISSUE_SKILLS:
            link = os.path.join(folder, name)
            target = os.path.join(source, name)
            recorded = self.find_recorded(result, link)
            if not recorded:
                # A record for this path whose link is gone or was replaced no longer names anything of Lite's.
                result = [x for x in result if not same_path(x["path"], link)]
            if recorded and same_path(recorded["target"], target):
                self.say(f"[ok] unchanged -> {link}")
                continue
            if recorded:
                self.change(f"earlier skills_dir link removed -> {link}", remove_link, link)
                result = [x for x in result if x is not recorded]
            elif exists(link) and self.our_skill(link):
                self.change(f"Lite's bundled {name} unlinked (skills_dir provides it) -> {link}",
                            self.remove_skill_entry, link)
            elif exists(link):
                self.say(f"[ok] kept (not Lite's), and it counts as {name} -> {link}")
                continue
            if not os.path.isfile(os.path.join(target, "SKILL.md")):
                self.say(f"[missing] {name} has no SKILL.md in the skills folder, not linked -> {target}")
                continue
            self.change(f"issue skill linked -> {link} ({target})", make_junction, link, target)
            if not self.what_if:
                result.append({"path": link, "target": target})
        return result

    def state_now(self) -> str:
        """Where Lite's writers put state today: the old hooks/state until --migrate has moved it."""
        if os.path.isdir(self.p.legacy_state) and not os.path.isdir(self.p.state):
            return self.p.legacy_state
        return self.p.state

    # --- modes -----------------------------------------------------------------------------------

    def uninstall(self, hooks_link: Optional[str], skills_link: Optional[str]) -> int:
        """Remove only what carries Lite's marker; the state folder is left alone."""
        if hooks_link:
            self.say(f"[skip] hooks folder is a link into another tree, no hook removed -> {self.p.hooks} ({hooks_link})")
        else:
            for _template, file, script in HOOK_CONFIGS:
                target = os.path.join(self.p.hooks, file)
                if not exists(target):
                    self.say(f"[ok] not installed -> {target}")
                elif self.our_hook(target, script):
                    self.change(f"hook config removed -> {target}", os.remove, target)
                else:
                    self.say(f"[skip] hook config is not ours (it does not run this checkout's hooks) -> {target}")
            launcher = os.path.join(self.p.hooks, CHIEF_LAUNCHER[1])
            if exists(launcher) and self.our_launcher(launcher):
                self.change(f"chief launcher removed -> {launcher}", os.remove, launcher)
        if not exists(self.p.skills):
            self.say(f"[ok] no skills folder -> {self.p.skills}")
        elif skills_link:
            self.say(f"[skip] skills dir is a link into another tree, not touched -> {self.p.skills} ({skills_link})")
        else:
            removed = 0
            for entry in list_dirs(self.p.skills):
                if self.our_skill(entry):
                    self.change(f"skill removed -> {entry}", self.remove_skill_entry, entry)
                    removed += 1
            if removed == 0:
                self.say(f"[ok] no skill of ours in {self.p.skills}")
        record = self.read_record()
        if record:
            left = record
            for folder in self.issue_skill_dirs():
                if exists(folder) and not foreign_link(folder):
                    left = self.remove_recorded_links(folder, left)
            # Uninstall is done with every recorded link, removed or kept, so the record goes.
            self.save_record([], True)
        self.say()
        if self.what_if:
            self.say("WhatIf: nothing was changed.")
        self.say(f"Done. The state folder was not touched: {self.state_now()}")
        self.say("Restart any running Copilot CLI session so it drops the hooks.")
        return 0

    def old_install(self, hooks_link: Optional[str], skills_link: Optional[str]) -> tuple:
        """The old fleet-config-lite install: its two hook configs and its skill junctions."""
        old_hooks = []
        if not hooks_link:
            old_hooks = [os.path.join(self.p.hooks, n) for n in OLD_HOOK_FILES if os.path.isfile(os.path.join(self.p.hooks, n))]
        old_skills = []
        if exists(self.p.skills) and not skills_link:
            for entry in list_dirs(self.p.skills):
                target = link_target(entry) or ""
                if is_link(entry) and not os.path.islink(entry) and OLD_SKILL_TARGET.search(target):
                    old_skills.append(entry)
        return old_hooks, old_skills

    def migrate_old(self, old_hooks: List[str], old_skills: List[str]) -> bool:
        """Remove the old install after one question; False when declined."""
        if not old_hooks and not old_skills:
            self.say("[ok] no fleet-config-lite hook config or skill link to remove")
        else:
            self.say("Migrating off fleet-config-lite. These would be removed (the state folder stays):")
            for path in old_hooks:
                self.say(f"  hook config -> {path}")
            for entry in old_skills:
                self.say(f"  skill link  -> {entry}")
            if not self.confirm("Remove them and install this checkout's hooks and skills?"):
                self.say("[skip] migration declined: nothing was changed")
                return False
            for path in old_hooks:
                self.change(f"old hook config removed -> {path}", os.remove, path)
            for entry in old_skills:
                self.change(f"old skill link removed -> {entry}", self.remove_skill_entry, entry)
        instructions = os.path.join(self.p.home, "copilot-instructions.md")
        if exists(instructions):
            target = link_target(instructions) or ""
            old_marker = False
            if not os.path.islink(instructions) and os.path.isfile(instructions):
                old_marker = read_text(instructions).startswith("<!-- fleet-config-lite: managed copy")
            if old_marker or re.search(r"[\\/]fleet-config-lite[^\\/]*[\\/]", target, re.IGNORECASE):
                self.say("[note] copilot-instructions.md came from fleet-config-lite and is left in place: "
                         f"this checkout does not manage global instructions -> {instructions}")
        return True

    def write_hooks(self, hooks_link: Optional[str], shared: bool) -> None:
        """Render each hook config with absolute paths (forward slashes keep the JSON free of escaping)."""
        if hooks_link:
            self.say(f"[skip] hooks folder is a link into another tree, no hook written -> {self.p.hooks} ({hooks_link})")
        else:
            for folder in (self.p.hooks, self.state_now()):
                if not exists(folder):
                    self.change(f"folder created -> {folder}", make_dir, folder)
            scope = " --lite-sessions-only" if shared else ""
            for template, file, script in HOOK_CONFIGS:
                target = os.path.join(self.p.hooks, file)
                target_link = foreign_link(target)
                if target_link:
                    self.say(f"[skip] hook config is a link into another tree -> {target} ({target_link})")
                    continue
                if os.path.isfile(target) and not self.our_hook(target, script):
                    self.say(f"[ok] kept (not Lite's: it does not run this checkout's hooks) -> {target}")
                    continue
                text = read_text(os.path.join(COPILOT_REPO, "hooks", template))
                rendered = render_hook_config(text, self.python or "", COPILOT_REPO, scope)
                if os.path.isfile(target) and read_text(target) == rendered:
                    self.say(f"[ok] unchanged -> {target}")
                    continue
                # BOM-less UTF-8: Copilot CLI 1.0.70 silently ignores BOM'd hook configs (verified live).
                self.change(f"hook config -> {target}", write_text, target, rendered)
            self.write_chief_launcher()
            if shared:
                self.say("     session-state records only sessions app-launcher-lite launched (--lite-sessions-only);")
                self.say("     chief-guard acts only for lite's chief and its lanes")
        self.say(f"     python      -> {self.python}")

    def write_chief_launcher(self) -> None:
        """Render the chief's launcher next to the hook configs, CRLF and BOM-less as cmd expects."""
        template, file = CHIEF_LAUNCHER
        target = os.path.join(self.p.hooks, file)
        target_link = foreign_link(target)
        if target_link:
            self.say(f"[skip] chief launcher is a link into another tree -> {target} ({target_link})")
            return
        if os.path.isfile(target) and not self.our_launcher(target):
            self.say(f"[ok] kept (not Lite's chief launcher) -> {target}")
            return
        text = read_text(os.path.join(COPILOT_REPO, "hooks", template))
        rendered = render_chief_launcher(text, self.python or "", COPILOT_REPO)
        if os.path.isfile(target) and read_text(target) == rendered:
            self.say(f"[ok] unchanged -> {target}")
            return
        self.change(f"chief launcher -> {target}", write_text, target, rendered)

    def install_skills(self, shared: bool, skills_link: Optional[str]) -> None:
        """Junction Lite's skills; the four issue skills come from skills_dir when it is set."""
        self.add_missing_config_key("skills_dir", '""')
        setting = self.skills_dir_setting()
        if setting["state"] == "set":
            self.say(f"[note] issue skills come from skills_dir -> {setting['path']}")
        elif setting["state"] == "unknown":
            self.say(f"[unknown] skills_dir not read ({setting['why']}): the four issue skills are left as they are")
        else:
            self.say("[note] skills_dir is empty: issue skills are Lite's bundled copies")
        record = self.read_record()
        before = json.dumps(record)
        sources: List[str] = []
        if shared:
            # Junctioned skills appear in every Copilot session, next to the full fleet's own.
            self.say(f"[skip] skills not installed on a shared machine -> {self.p.skills}")
        elif skills_link:
            self.say(f"[skip] skills dir is a link into another tree, not touched -> {self.p.skills} ({skills_link})")
        else:
            if not exists(self.p.skills):
                self.change(f"folder created -> {self.p.skills}", make_dir, self.p.skills)
            for folder in self.issue_skill_dirs():
                if setting["state"] == "set":
                    record = self.sync_issue_skill_links(folder, setting["path"], record)
                elif setting["state"] == "empty":
                    record = self.remove_recorded_links(folder, record)
            sources = list_dirs(OUR_SKILLS)
            if setting["state"] != "empty":
                sources = [d for d in sources if os.path.basename(d) not in ISSUE_SKILLS]
        for source in sources:
            self.link_skill(source)
        self.save_record(record, json.dumps(record) != before)

    def link_skill(self, source: str) -> None:
        name = os.path.basename(source)
        link = os.path.join(self.p.skills, name)
        if exists(link):
            if not self.our_skill(link):
                counts = f", and it counts as {name}" if name in ISSUE_SKILLS else ""
                self.say(f"[ok] kept (not Lite's){counts} -> {link}")
                return
            if is_link(link) and same_path(link_target(link) or "", source):
                self.say(f"[ok] unchanged -> {link}")
                return
            self.change(f"stale skill entry of Lite's removed -> {link}", self.remove_skill_entry, link)
        if self.what_if:
            self.say(f"[whatif] skill junction -> {link}")
            return
        try:
            make_junction(link, source)
            self.say(f"[ok] skill junction -> {link}")
        except (OSError, ImportError):
            # runtime-write-exempt: the skill copy lands in ~/.copilot/skills, never a code folder
            shutil.copytree(source, link)
            write_text(os.path.join(link, SKILL_MARKER), "app-launcher-lite copilot\\install.py manages this copy; --uninstall removes it")
            self.say(f"[ok] skill copied   -> {link} (junction unavailable)")

    # --- helpers run as children -------------------------------------------------------------

    def helper(self, *argv: str, timeout: float = HELPER_TIMEOUT_S) -> List[str]:
        """Stdout lines of one of this checkout's helper scripts, or [] when it did not answer."""
        try:
            proc = subprocess.run(  # noqa: S603 -- this checkout's own helper, argv built here
                [sys.executable, *argv], capture_output=True, text=True, timeout=timeout,
                creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return proc.stdout.splitlines()

    def update_copilot_folder_setting(self) -> None:
        """Repoint an explicit fleet_config_dir at this checkout through Lite's Settings API, never the file."""
        if not os.path.isfile(WEBAPP_CONFIG):
            self.say("[ok] Copilot folder setting: no webapp_config.json, so Lite uses this checkout's copilot folder")
            return
        try:
            body = json.loads(read_text(WEBAPP_CONFIG))
            current = str((body.get("fleet_config_dir") if isinstance(body, dict) else None) or "")
        except ValueError:
            self.say("[unknown] Copilot folder setting not read (webapp_config.json did not parse): check it in Lite's Settings -> Folders")
            return
        if not current.strip():
            self.say("[ok] Copilot folder setting is empty: Lite uses this checkout's copilot folder")
            return
        if same_path(current, COPILOT_REPO):
            self.say(f"[ok] Copilot folder setting already points here -> {current}")
            return
        self.say(f"[note] Lite's Copilot folder setting holds another folder -> {current}")
        self.say("       A chief started there is not recognised by this checkout. Pointing it at the copilot folder here:")
        self.say(f"       {COPILOT_REPO}")
        if not self.confirm("Change that one Lite setting now, through its Settings API?"):
            self.say("[skip] Copilot folder setting left as it was; set it in Lite's Settings -> Folders before starting the chief")
            return
        lines = self.helper(os.path.join(COPILOT_REPO, "skills", "_lib", "lite_ports.py"), "webapp-url")
        url = lines[0].strip() if lines else ""
        if not re.fullmatch(r"http://127\.0\.0\.1:\d+", url):
            self.say("[unknown] Lite's webapp address not read (lite_ports.py did not answer): set the Copilot folder in Settings -> Folders")
            return
        if self.what_if:
            self.say(f"[whatif] Copilot folder setting -> {COPILOT_REPO} (through {url}/api/config)")
            return
        # An explicit path, not an empty value: the running webapp keeps whatever it is sent until
        # its next start, and an empty folder there would read as the current directory.
        try:
            lite_webapp.request(url, "/api/config", "POST", {"fleet_config_dir": COPILOT_REPO}, timeout=HTTP_TIMEOUT_S)
            after = str(lite_webapp.request(url, "/api/config", timeout=HTTP_TIMEOUT_S).get("fleet_config_dir") or "")
        except lite_webapp.WrongSchemeError as exc:
            self.say(f"[unknown] Lite's Settings call hit the wrong scheme ({exc.reason}): it was not resent; "
                     "re-run copilot\\install.py --migrate, or set the Copilot folder in Settings -> Folders")
            return
        except urllib.error.HTTPError as exc:
            self.say(f"[unknown] Lite refused the Settings change (HTTP {exc.code}, {url}): "
                     "set the Copilot folder in Settings -> Folders")
            return
        except (OSError, ValueError, AttributeError):
            self.say(f"[unknown] Lite did not take the change ({url}): start it (tray.bat), then re-run "
                     "copilot\\install.py --migrate, or set the Copilot folder in Settings -> Folders")
            return
        if same_path(after, COPILOT_REPO):
            self.say(f"[ok] Copilot folder setting -> {after} (read back from Lite)")
        else:
            self.say(f"[unknown] Lite answered but its Copilot folder reads back as '{after}': check Settings -> Folders")

    def checklist(self) -> None:
        """Every line is ok, missing or unknown, never assumed."""
        self.say()
        self.say("Fresh-machine checklist:")
        copilot = shutil.which("copilot")
        if copilot:
            version = run_first_line([copilot, "--version"])
            self.say(f"[ok] Copilot CLI -> {version}")
        else:
            self.say("[missing] Copilot CLI: install it (winget install GitHub.Copilot), then run copilot login")
        self.gitlab_lines()
        # The helpers run through the repo-root venv that setup.bat creates; the chief skips them without it.
        if os.path.isfile(os.path.join(REPO, ".venv", "Scripts", "python.exe")):
            self.say("[ok] venv")
        else:
            self.say("[missing] venv: run setup.bat")
        # The session-host port is config/config.json's; lite_ports.py is the one reader every caller shares.
        lines = self.helper(os.path.join(COPILOT_REPO, "skills", "_lib", "lite_ports.py"), "session-host-url")
        host_url = lines[0].strip() if lines else ""
        match = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/sessions", host_url)
        if not match:
            self.say("[unknown] session-host port not read (lite_ports.py did not answer)")
        else:
            try:
                with urllib.request.urlopen(host_url, timeout=SESSION_HOST_TIMEOUT_S):  # noqa: S310 -- loopback URL checked above
                    pass
                self.say(f"[ok] session-host answers on :{match.group(1)}")
            except (OSError, ValueError):
                self.say(f"[unknown] session-host not reachable on :{match.group(1)} (start the tray)")
        self.say("[next] restart any running Copilot CLI session, then run: python copilot\\scripts\\copilot_probe.py run --live")

    def gitlab_lines(self) -> None:
        """glab's sign-in for the GitLab host git uses, then whether git itself reaches it with no prompt."""
        access: Dict[str, str] = {}
        fixes: List[str] = []
        script = os.path.join(COPILOT_REPO, "skills", "_lib", "gitlab_git_access.py")
        for line in self.helper(script, "check", "--repo", REPO):
            key, _, value = line.partition("=")
            if key == "FIX":
                fixes.append(value)
            else:
                access[key] = value
        host = access.get("HOST", "")
        # glab keeps one sign-in per host, so a bare `glab auth status` misreports a self-hosted one.
        glab = shutil.which("glab")
        if not glab:
            self.say("[missing] glab: install it (winget install GLab.GLab), then run glab auth login")
        elif not host:
            self.say("[unknown] glab sign-in not confirmed: no GitLab host is configured "
                     "(no https GitLab remote found in this checkout or its sibling checkouts)")
        elif run_ok([glab, "auth", "status", "--hostname", host]):
            self.say(f"[ok] glab signed in -> {host}")
        else:
            self.say(f"[missing] glab is not signed in to {host}: run glab auth login --hostname {host}")
        # glab signed in does not mean git can reach GitLab: git asks its own credential helper.
        # The fix is printed for the owner to run; this installer never edits git config.
        state = access.get("GIT_ACCESS")
        if state == "ok":
            self.say(f"[ok] git reaches GitLab without a prompt -> {host}")
        elif state == "missing":
            self.say(f"[missing] git cannot reach GitLab without a prompt ({host}): {access.get('REASON', '')}")
            self.say("          glab being signed in is not enough: git uses its own credential helper. "
                     "Run this yourself (the installer never edits your git config):")
            for fix in fixes:
                self.say(f"            {fix}")
            self.say("          then re-run copilot\\install.py; this line should read [ok]")
        else:
            self.say(f"[unknown] git access to GitLab not confirmed: {access.get('REASON') or 'the check did not run'}")

    def run(self, uninstall: bool, migrate: bool, skip_checklist: bool) -> int:
        home_link = foreign_link(self.p.home)
        if home_link:
            self.say(f"[skip] Copilot home is a link into another tree, nothing written -> {self.p.home} ({home_link})")
            return 0
        hooks_link = foreign_link(self.p.hooks)
        skills_link = foreign_link(self.p.skills)
        if uninstall:
            return self.uninstall(hooks_link, skills_link)
        shared_why = None
        for candidate in (self.p.skills, self.p.agents_skills):
            link = foreign_link(candidate)
            if link:
                shared_why = f"{candidate} -> {link}"
                break
        if shared_why:
            self.say(f"[note] shared machine (the full fleet runs here too): {shared_why}")
        old_hooks, old_skills = self.old_install(hooks_link, skills_link)
        if migrate:
            if not self.migrate_old(old_hooks, old_skills):
                return 1
            install_state.migrate_state(self.p.legacy_state, self.p.state, WEBAPP_CONFIG, self.say, self.change,
                                        self.what_if, is_link, write_text)
        elif os.path.isdir(self.p.legacy_state):
            self.say("[warn] Lite's state is still in hooks\\state, where Copilot CLI reports each .json as an invalid")
            self.say(f"       hook config in every session. Re-run with --migrate to move it -> {self.p.legacy_state}")
        elif old_hooks:
            self.say("[warn] the old fleet-config-lite hook configs are still installed: two session-state hooks race on the same state file.")
            self.say("       Stop the chief at a turn end, then re-run with --migrate:")
            for path in old_hooks:
                self.say(f"         {path}")
        self.python = resolve_python()
        if not self.python:
            print("No usable python.exe found (only the WindowsApps alias). Install Python and re-run.", file=sys.stderr)
            return 1
        self.write_hooks(hooks_link, bool(shared_why))
        self.install_skills(bool(shared_why), skills_link)
        if migrate:
            self.update_copilot_folder_setting()
            self.say("[next] restart Lite (tray.bat --restart), then start the chief from the Board")
        if not skip_checklist:
            self.checklist()
        self.say()
        if self.what_if:
            self.say("WhatIf: nothing was changed. Run again without --what-if to make the changes listed above.")
        self.say("Done. Restart any running Copilot CLI session to pick up the hooks.")
        self.say(f"State file will appear at: {self.state_now()}\\sessions-state.json")
        return 0


# --- rendering: what install writes, and what `chief_ops.py doctor` compares against -------------

def render_hook_config(template: str, python: str, copilot_repo: str, scope: str) -> str:
    """A hook config with absolute paths filled in (forward slashes keep the JSON free of escaping)."""
    return (template.replace("{{PYTHON}}", python.replace("\\", "/"))
            .replace("{{REPO}}", copilot_repo.replace("\\", "/")).replace("{{SCOPE}}", scope))


def render_chief_launcher(template: str, python: str, copilot_repo: str) -> str:
    """The chief's cmd launcher with this checkout's paths, CRLF as cmd expects."""
    return (template.replace("{{PYTHON}}", python).replace("{{COPILOT}}", copilot_repo)
            .replace("\r\n", "\n").replace("\n", "\r\n"))


# --- small I/O helpers ----------------------------------------------------------------------------

def read_text(path: str) -> str:
    """The file's text with any BOM dropped and line endings kept as written."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return fh.read()


def write_text(path: str, text: str) -> None:
    """BOM-less UTF-8, line endings as given."""
    # runtime-write-exempt: hook configs, the install record and webapp_config.json, never a code folder
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def resolve_python() -> Optional[str]:
    """A real python.exe for the hooks: never the WindowsApps alias, which can hang when a hook spawns it."""
    candidates: List[str] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local) / "Programs" / "Python"
        candidates += [str(p) for p in [base / "python.exe"] if p.is_file()]
        candidates += [str(p) for p in sorted(base.glob("*/python.exe"), key=lambda p: str(p).lower())]
    on_path = shutil.which("python")
    if on_path:
        candidates.append(on_path)
    for candidate in candidates:
        if "\\windowsapps\\" not in candidate.lower():
            return candidate
    return None


def run_first_line(argv: List[str]) -> str:
    try:
        proc = subprocess.run(  # noqa: S603 -- a CLI found on PATH, argv built here
            argv, capture_output=True, text=True, timeout=HELPER_TIMEOUT_S,
            creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = proc.stdout.splitlines()
    return lines[0] if lines else ""


def run_ok(argv: List[str]) -> bool:
    try:
        proc = subprocess.run(  # noqa: S603 -- a CLI found on PATH, argv built here
            argv, capture_output=True, text=True, timeout=HELPER_TIMEOUT_S,
            creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install Lite's Copilot hooks and skills into ~/.copilot.")
    parser.add_argument("--uninstall", action="store_true", help="remove exactly what this installer put there")
    parser.add_argument("--migrate", action="store_true", help="leave fleet-config-lite, then install")
    parser.add_argument("--yes", action="store_true", help="answer yes to --migrate's questions")
    parser.add_argument("--skip-checklist", action="store_true", help="skip the fresh-machine checklist")
    parser.add_argument("--what-if", action="store_true", help="list every change and make none")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = parse_args(argv)
    if args.uninstall and args.migrate:
        print("--uninstall and --migrate cannot be combined: migrate installs, uninstall removes.", file=sys.stderr)
        return 2
    return Installer(args).run(args.uninstall, args.migrate, args.skip_checklist)


if __name__ == "__main__":
    sys.exit(main())
