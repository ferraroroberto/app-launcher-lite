"""Move Lite's state folder out of Copilot's hooks folder: ``install.py --migrate``.

Copilot CLI loads every ``.json`` below ``<copilot-home>/hooks/`` as a hook config and logs an
error for each of Lite's state files there, in every session on the machine. The state now lives
in ``<copilot-home>/app-launcher-lite-state/``. Every writer resolves the folder per call: the old
``hooks/state`` while it is the only one, the new folder once it exists.

The move is copy-then-switch, so a live chief or lane keeps working through it:
1. copy the old folder into a staging folder beside the new one, noting each file's size and time;
2. rename the staging folder to the new name: from that instant every hook and chief command
   writes there;
3. repoint any Lite setting still pinned to the old folder (webapp_config.json, after a backup);
4. sweep the old folder: a file unchanged since its copy is removed, a file written since (a
   mailbox event, a newer plan) is moved across unless the new folder's copy changed too, in
   which case the new one stays live and the old one is kept beside it. A second sweep after a short wait catches a
   writer that resolved the old folder just before the switch; the old folder goes once empty.

Stopping the chief first would also work, but it costs the operator a restart of every lane,
and a lane that ends its turn mid-move would lose that turn-end event. Copy-then-switch has no
window in which a writer has nowhere to write.

The running webapp keeps the paths it loaded until its next start, so anything it writes in the
meantime lands in the old folder; re-running --migrate after a restart sweeps that across, and a
run with nothing left to move changes nothing.
"""

from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
import time
from typing import Callable, Dict, List, Optional, Tuple

STATE_DIRNAME = "app-launcher-lite-state"
STAGING_SUFFIX = ".migrating"
# A hook or chief command that resolved the old folder just before the switch finishes its
# write in milliseconds; this is the wait before the sweep that catches it.
SETTLE_S = 3.0
# Lite settings that name a file in the state folder, and that file's name there.
STATE_PATH_KEYS = {
    "sessions_state_file": "sessions-state.json",
    "chief_plan_file": "chief-plan.json",
    "chief_managed_file": "chief-managed.json",
    "chief_inbox_dir": "chief-inbox",
}
ASIDE_SUFFIX = ".from-hooks-state"

# Each copied file: (its size and time when copied, the copy's size and time).
Snapshot = Dict[str, Tuple[Tuple[int, int], Tuple[int, int]]]


def _stamp(path: str) -> Tuple[int, int]:
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


def _files(root: str) -> List[str]:
    """Every file below ``root`` as a relative path, deepest folders last."""
    found = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(found)


def copy_tree(src: str, dst: str) -> Snapshot:
    """Copy ``src`` into ``dst`` (kept if a run before this one died mid-copy), noting each file as copied."""
    snapshot: Snapshot = {}
    for rel in _files(src):
        source, target = os.path.join(src, rel), os.path.join(dst, rel)
        try:
            before = _stamp(source)
            # runtime-write-exempt: Lite's state folder in the Copilot home, never a code folder
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)  # runtime-write-exempt: the staging copy of Lite's state folder in the Copilot home
        except FileNotFoundError:
            continue  # removed (an event acked) between the listing and the copy
        snapshot[rel] = (before, _stamp(target))
    return snapshot


def _move(source: str, target: str) -> None:
    # runtime-write-exempt: Lite's state folder in the Copilot home, never a code folder
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.replace(source, target)  # must-use-exempt: moves an existing state file across, not a write; the installer is stdlib-only and cannot import src


def sweep(legacy: str, current: str, snapshot: Snapshot, final: bool) -> List[str]:
    """Empty ``legacy`` into ``current``; the lines to report. Temp files wait for the final pass."""
    lines = []
    for rel in _files(legacy):
        if not final and rel.endswith(".tmp"):
            continue  # an atomic write in flight: its writer renames it into place itself
        source, target = os.path.join(legacy, rel), os.path.join(current, rel)
        try:
            copied = snapshot.get(rel)
            if copied and copied[0] == _stamp(source):
                os.remove(source)  # copied unchanged; the new copy may be gone already (an acked event)
            elif not os.path.exists(target) or (copied and copied[1] == _stamp(target)):
                _move(source, target)
                lines.append(f"[ok] written after the copy, moved across -> {target}")
            elif filecmp.cmp(source, target, shallow=False):
                os.remove(source)
            else:
                # Both sides changed: the new folder's copy is the live one, the old kept beside it.
                aside = target + ASIDE_SUFFIX
                _move(source, aside)
                lines.append(f"[warn] changed in both folders: kept the new one, the old one is beside it -> {aside}")
        except FileNotFoundError:
            continue
        except OSError as exc:
            lines.append(f"[warn] not moved yet ({exc.strerror}): re-run --migrate -> {source}")
    return lines


def remove_empty_dirs(root: str) -> bool:
    """Remove ``root`` and its empty subfolders bottom-up; True when ``root`` is gone."""
    for dirpath, _dirs, _names in sorted(os.walk(root), key=lambda w: len(w[0]), reverse=True):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass
    return not os.path.exists(root)


def repin_config(config_path: str, legacy: str, current: str) -> Optional[str]:
    """``config_path``'s text with each setting naming the old folder pointed at the new one, else None."""
    try:
        with open(config_path, encoding="utf-8-sig", newline="") as fh:
            raw = fh.read()
        body = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    updated = raw
    for key, name in STATE_PATH_KEYS.items():
        value = body.get(key)
        if not isinstance(value, str) or not _same(value, os.path.join(legacy, name)):
            continue
        pattern = re.compile(r'("' + re.escape(key) + r'"\s*:\s*)' + re.escape(json.dumps(value)))
        replacement = json.dumps(os.path.join(current, name))
        updated = pattern.sub(lambda m, new=replacement: m.group(1) + new, updated)
    if updated == raw:
        return None
    json.loads(updated)  # a splice that broke the JSON raises instead of being written
    return updated


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def migrate_state(legacy: str, current: str, config_path: str, say: Callable[[str], None],
                  change: Callable[..., None], what_if: bool, is_link: Callable[[str], bool],
                  write_text: Callable[[str, str], None]) -> None:
    """Move ``legacy`` into ``current`` as the module docstring describes; idempotent."""
    if is_link(legacy):
        say(f"[skip] the old state folder is a link into another tree, not moved -> {legacy}")
        return
    if not os.path.isdir(legacy):
        say(f"[ok] state already lives outside Copilot's hooks folder -> {current}")
        return
    snapshot: Snapshot = {}
    switched = not os.path.isdir(current)
    if switched:
        staging = current + STAGING_SUFFIX
        if what_if:
            say(f"[whatif] state copied, then switched -> {current}")
            say(f"[whatif] the old folder emptied and removed -> {legacy}")
            return
        snapshot = copy_tree(legacy, staging)
        change(f"state copied ({len(snapshot)} files), writers switched -> {current}", os.rename, staging, current)
    elif what_if:
        say(f"[whatif] files still in the old folder moved across -> {current}")
        return
    updated = repin_config(config_path, legacy, current) if os.path.isfile(config_path) else None
    if updated is not None:
        backup = config_path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        change(f"backup written -> {backup}", shutil.copy2, config_path, backup)
        change("state paths in webapp_config.json pointed at the new folder", write_text, config_path, updated)
    for line in sweep(legacy, current, snapshot, final=False):
        say(line)
    time.sleep(SETTLE_S)
    for line in sweep(legacy, current, snapshot, final=True):
        say(line)
    if remove_empty_dirs(legacy):
        say(f"[ok] old state folder removed -> {legacy}")
    else:
        say(f"[warn] the old state folder still holds files; re-run --migrate to move them -> {legacy}")
    if switched:
        say("[next] restart Lite (tray.bat --restart) so the webapp reads the new folder, then run --migrate "
            "once more: it moves anything the webapp wrote before its restart, and changes nothing otherwise")
