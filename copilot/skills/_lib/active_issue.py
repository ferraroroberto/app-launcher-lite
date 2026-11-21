"""Maintain the Fleet Board's cross-repo active-issue markers.

Issue workflows call this helper after an issue branch is ready and after its
successful merge.  The shared state file lets App Launcher distinguish a
backlog issue that is already being worked without treating branch presence as
the source of truth.

The file is advisory and self-healing: missing/corrupt input reads as empty,
rows older than 24 hours are pruned on every mutation, and writes use an atomic
temporary-file replacement.  A small lock directory serializes the complete
read-modify-write cycle so concurrent ``/issue-batch`` writers cannot lose one
another's rows.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AbstractSet, Any, Callable, Dict, Iterator, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_run  # noqa: E402
from fleet_core import runtime_files  # noqa: E402
from fleet_core.hooks_state import atomic_write_text, state_dir  # noqa: E402
from worktree_residue import WT_SEP  # noqa: E402

STATE_FILENAME = "active-issues.json"
PRUNE_AFTER = timedelta(hours=24)
LOCK_TIMEOUT_SECONDS = 5.0
STALE_LOCK_AFTER_SECONDS = 30.0
_REPLACE_ATTEMPTS = 3
_LOCK_OWNER_PREFIX = "owner"
_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WIN_STILL_ACTIVE = 259
_WIN_ERROR_ACCESS_DENIED = 5
_WIN_ERROR_INVALID_PARAMETER = 87

# Optional owner fields on a claim row. Rows written before
# them, or by a writer with no resolvable owner, simply omit the field.
OWNER_SESSION_FIELD = "owner_session_id"
OWNER_PID_FIELD = "owner_pid"
OWNER_HOST_FIELD = "owner_host"
# The agent process that owns the session. Never this helper's own pid (or
# its shell's): both exit seconds after the write and would read as dead.
_OWNER_PID_ENV = "CLAUDE_PID"
_OWNER_SESSION_ENV = "APP_LAUNCHER_SESSION_ID"

LIVENESS_LIVE = "live"
LIVENESS_DEAD = "dead"
LIVENESS_UNKNOWN = "unknown"


def pid_state(pid: int) -> Optional[bool]:
    """Tri-state pid probe for claim-owner liveness: True alive, False
    confirmed gone, None when the probe cannot tell.

    A claim's ``dead`` needs positive evidence, so "could
    not open the process" stays ``None`` rather than folding into dead; the
    state lock reads ``pid_state(pid) is True``, where unknown counts as not
    alive because the lock's age must also exceed the stale horizon. Access
    denied means the process exists. A pid reused by an unrelated process
    reads as alive: the failure to avoid is displacing a live holder.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if sys.platform == "win32":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            error = ctypes.get_last_error()  # type: ignore[attr-defined]
            if error == _WIN_ERROR_INVALID_PARAMETER:
                return False
            if error == _WIN_ERROR_ACCESS_DENIED:
                return True
            return None
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            return exit_code.value == _WIN_STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _read_lock_owner(lock_dir: Path) -> Optional[Dict[str, Any]]:
    """Identify the marker file by name only -- never open it.

    The owner's pid and token live in the filename itself (``owner.<pid>.
    <token>``), so a staleness check is a directory listing, not a file
    open. A file read here would race the true holder's own open/close of
    that same name on Windows (``WinError 32``, "used by another process"),
    which is exactly the flake this design avoids.
    """
    try:
        entries = list(lock_dir.iterdir())
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return None
    for entry in entries:
        parts = entry.name.split(".", 2)
        if len(parts) == 3 and parts[0] == _LOCK_OWNER_PREFIX and parts[1].isdigit():
            return {"pid": int(parts[1]), "token": parts[2]}
    return None


def _write_lock_owner(lock_dir: Path, token: str) -> None:
    marker = lock_dir / f"{_LOCK_OWNER_PREFIX}.{os.getpid()}.{token}"
    runtime_files.touch_file(marker, exist_ok=False)


def state_file(filename: str = STATE_FILENAME) -> Path:
    """Resolve a state path under the shared hooks-state root at call time so
    tests can override it (`CLAUDE_HOOKS_STATE_DIR`, via `hooks_state.state_dir()`).
    `filename` defaults to this module's own `active-issues.json`; a sibling
    marker file with the same read-modify-write shape (e.g. `chief_managed.py`'s
    `chief-managed.json`) passes its own name rather than re-deriving this
    resolution."""
    return state_dir() / filename


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(row: Any, field: str) -> Optional[datetime]:
    if not isinstance(row, dict):
        return None
    raw = row.get(field)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def read_rows(path: Optional[Path] = None) -> Dict[str, Any]:
    """Return the current mapping, or an empty mapping for invalid input."""
    target = path or state_file()
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def prune_rows(
    rows: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    stamp_field: str = "started_at",
) -> Dict[str, Any]:
    """Keep only well-formed records whose `stamp_field` timestamp is no
    older than :data:`PRUNE_AFTER`. `stamp_field` defaults to this module's
    own `started_at`; `chief_managed.py` reuses this directly with
    `stamp_field="dispatched_at"` rather than re-deriving its own copy."""
    cutoff = (now or _now()) - PRUNE_AFTER
    kept: Dict[str, Any] = {}
    for key, row in rows.items():
        stamp = _parse_timestamp(row, stamp_field)
        if stamp is not None and stamp >= cutoff:
            kept[str(key)] = row
    return kept


def write_rows(path: Path, rows: Dict[str, Any]) -> None:
    runtime_files.make_dirs(path.parent, parents=True, exist_ok=True)
    payload = json.dumps(rows, indent=2, sort_keys=True) + "\n"
    last_error: Optional[OSError] = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            atomic_write_text(path, payload, newline="\n")
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.05 * (attempt + 1))
    assert last_error is not None
    raise last_error


@contextmanager
def state_lock(
    path: Path,
    *,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
    stale_after_seconds: float = STALE_LOCK_AFTER_SECONDS,
) -> Iterator[None]:
    """Serialize one state-file read-modify-write transaction.

    ``mkdir`` is atomic on Windows and POSIX.  The holder's pid and a random
    token are recorded inside the lock directory on acquire.  A contender
    only reclaims the directory once it is *both* older than
    ``stale_after_seconds`` *and* its recorded pid is confirmed dead --  age
    alone can't tell a crashed holder from one that is merely slow, and a
    slow-but-alive holder must never be displaced.  Release verifies the
    lock directory still carries this call's own token before removing it,
    so a holder that was reclaimed while it slept past the stale horizon
    can't turn around and delete the reclaimer's lock out from under it.
    """
    runtime_files.make_dirs(path.parent, parents=True, exist_ok=True)
    lock_dir = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout_seconds
    token = uuid.uuid4().hex
    while True:
        try:
            runtime_files.make_dirs(lock_dir)
            break
        except (FileExistsError, PermissionError):
            # Windows can surface PermissionError instead of FileExistsError
            # when this mkdir() races another thread's rmdir() on the same
            # path (NTFS reports "access denied" for a directory mid-deletion
            # rather than "already exists") — treat it identically: retry.
            try:
                age = time.time() - lock_dir.stat().st_mtime
                owner = _read_lock_owner(lock_dir)
                holder_alive = owner is not None and pid_state(owner.get("pid", -1)) is True
                if age > stale_after_seconds and not holder_alive:
                    shutil.rmtree(lock_dir)
                    continue
            except (FileNotFoundError, PermissionError):
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for active-issue state lock: {lock_dir}")
            time.sleep(0.025)
    try:
        _write_lock_owner(lock_dir, token)
        yield
    finally:
        owner = _read_lock_owner(lock_dir)
        if owner is None or owner.get("token") == token:
            try:
                shutil.rmtree(lock_dir)
            except FileNotFoundError:
                pass
        # else: someone else reclaimed this lock directory while we held it
        # past the stale horizon (should not happen given the liveness check
        # above, but this is the last line of defense) -- removing it here
        # would delete the reclaimer's live lock, not ours.


def _remote_repo_name(repo_path: Path) -> Optional[str]:
    # The bare repo name out of `owner/repo` -- `git_run.origin_slug` owns the
    # `remote get-url origin` call plus the regex, shared with the other
    # call sites across the repo that need the same parse.
    slug = git_run.origin_slug(repo_path)
    return slug.rsplit("/", 1)[-1] if slug else None


def resolve_repo_name(repo: str | Path) -> str:
    """Resolve the canonical repo name, including from a sibling worktree."""
    repo_path = Path(repo).resolve()
    remote_name = _remote_repo_name(repo_path)
    if remote_name:
        return remote_name
    return re.sub(rf"{re.escape(WT_SEP)}\d+$", "", repo_path.name, flags=re.IGNORECASE)


def marker_key(repo_name: str, issue: int) -> str:
    return f"{repo_name}#{issue}"


def current_host() -> Optional[str]:
    """This machine's host id, or None when it cannot be resolved."""
    try:
        host = socket.gethostname().strip()
    except OSError:
        return None
    return host or None


def owner_fields(environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Resolve the optional owner fields for a new claim row.

    Each field is included only when positively known: the App Launcher
    session id, the agent process pid, and the host both are scoped to. A
    field that cannot be resolved is omitted, never guessed.
    """
    env = os.environ if environ is None else environ
    fields: Dict[str, Any] = {}
    session_id = (env.get(_OWNER_SESSION_ENV) or "").strip()
    if session_id:
        fields[OWNER_SESSION_FIELD] = session_id
    raw_pid = (env.get(_OWNER_PID_ENV) or "").strip()
    if raw_pid.isdigit() and int(raw_pid) > 0:
        fields[OWNER_PID_FIELD] = int(raw_pid)
    host = current_host()
    if host and fields:
        fields[OWNER_HOST_FIELD] = host
    return fields


def classify_owner(
    row: Any,
    *,
    live_session_ids: Optional[AbstractSet[str]],
    pid_probe: Callable[[int], Optional[bool]],
    host: Optional[str],
) -> str:
    """Classify a claim row's owner as ``live`` / ``dead`` / ``unknown``.

    Pure apart from the injected ``pid_probe`` (pass :func:`pid_state` for the
    real one). ``live_session_ids`` is the set of live App Launcher session
    ids, or None when that list could not be read; ``host`` is
    :func:`current_host`. Evidence of life wins over evidence of death, and
    ``dead`` needs positive evidence: the owner session absent from a
    readable list, or the owner pid confirmed gone on the owner's own host.
    Rows without owner fields, from another host, or with any unreadable
    input are ``unknown``. Classification never mutates or prunes a row.
    """
    if not isinstance(row, dict):
        return LIVENESS_UNKNOWN
    session_id = row.get(OWNER_SESSION_FIELD)
    pid = row.get(OWNER_PID_FIELD)
    owner_host = row.get(OWNER_HOST_FIELD)
    has_session = isinstance(session_id, str) and bool(session_id)
    has_pid = isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
    if not isinstance(owner_host, str) or not owner_host or not host:
        return LIVENESS_UNKNOWN
    if owner_host.casefold() != host.casefold():
        return LIVENESS_UNKNOWN

    verdicts = []
    if has_session and live_session_ids is not None:
        verdicts.append(session_id in live_session_ids)
    if has_pid:
        try:
            alive = pid_probe(pid)
        except Exception:
            alive = None
        if alive is not None:
            verdicts.append(bool(alive))
    if any(verdicts):
        return LIVENESS_LIVE
    if verdicts:
        return LIVENESS_DEAD
    return LIVENESS_UNKNOWN


def add_marker(
    repo: str | Path,
    issue: int,
    branch: str,
    *,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Add/refresh one marker and return the row written."""
    if issue <= 0:
        raise ValueError("issue number must be positive")
    if not branch.strip():
        raise ValueError("branch must not be empty")
    target = path or state_file()
    moment = now or _now()
    repo_name = resolve_repo_name(repo)
    row: Dict[str, Any] = {
        "repo": repo_name,
        "number": issue,
        "branch": branch,
        "started_at": _iso_z(moment),
        **owner_fields(environ),
    }
    with state_lock(target):
        rows = prune_rows(read_rows(target), now=moment)
        rows[marker_key(repo_name, issue)] = row
        write_rows(target, rows)
    return row


def remove_marker(
    repo: str | Path,
    issue: int,
    *,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> bool:
    """Remove one marker while preserving and pruning all unrelated rows."""
    if issue <= 0:
        raise ValueError("issue number must be positive")
    target = path or state_file()
    moment = now or _now()
    repo_name = resolve_repo_name(repo)
    key = marker_key(repo_name, issue)
    with state_lock(target):
        rows = prune_rows(read_rows(target), now=moment)
        removed = rows.pop(key, None) is not None
        write_rows(target, rows)
    return removed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="active_issue", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    add_parser = sub.add_parser("add", help="record an issue branch as active")
    add_parser.add_argument("repo", help="repo or worktree path")
    add_parser.add_argument("issue", type=int)
    add_parser.add_argument("branch")

    remove_parser = sub.add_parser("remove", help="clear an active issue marker")
    remove_parser.add_argument("repo", help="repo or worktree path")
    remove_parser.add_argument("issue", type=int)

    args = parser.parse_args(argv)
    if args.command == "add":
        row = add_marker(args.repo, args.issue, args.branch)
        print(f"ACTIVE_ISSUE=added key={marker_key(row['repo'], row['number'])}")
    else:
        repo_name = resolve_repo_name(args.repo)
        removed = remove_marker(args.repo, args.issue)
        status = "removed" if removed else "absent"
        print(f"ACTIVE_ISSUE={status} key={marker_key(repo_name, args.issue)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
