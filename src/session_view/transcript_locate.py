"""Filesystem correlation of a live Copilot session to its event log.

Split out of ``src/board/exchange.py``: that module resolves *what to
show* for the Board drawer's conversation preview, while this one answers a
narrower, reusable question — *which file on disk belongs to this live
session*. ``src.board.exchange`` and ``app/webapp/routers/session_transcript.py``
both call into this module rather than duplicating the correlation logic;
neither owns it. It also holds the two Claude project-folder readers the
resume picker lists conversations with.

The finder fails safe: anything short of exactly one unambiguous,
safely-correlated candidate returns ``None`` rather than risk showing a
neighbouring session's text.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.board.transcript import _read_tail_bytes

# How far back to look for the naming records a scanned Claude conversation
# declares about itself, read with the same bounded-tail discipline as
# every other reader here. Claude Code re-emits `ai-title`/`custom-title`
# periodically rather than once, so the newest one sits near the end of the
# file — measured across the 8 newest conversations in three project folders
# on this box, the last naming record sat at most 22.7 KB from EOF, including
# in a 67.8 MB chief conversation. 256 KB is a ~11x margin on that worst case
# and the same window `src.board.transcript._EXCHANGE_TAIL_BYTES` already uses for
# this file. `_ACTIVITY_TAIL_BYTES` (8 KB) is deliberately *not* reused: it
# would have missed the record in 6 of those 8 files.
_TITLE_TAIL_BYTES = 256 * 1024

_COPILOT_STATE_DIR = Path.home() / ".copilot" / "session-state"
# Copilot's own session folder is created at launch, a little *after* the
# launcher spawned it: measured at 3.71 s and 3.72 s on two launches here.
# The window is wide enough to absorb a loaded box (16x the measured delay)
# and narrow enough that two launches in one folder rarely both fall in it —
# and when they do, `find_copilot_transcript` refuses rather than guesses.
_COPILOT_START_SLOP_SECONDS = 60
# Copilot stamps `created_at` from its own clock, so allow it to read a hair
# before the launcher's `started_at` without disqualifying the folder.
_COPILOT_CLOCK_SKEW_SECONDS = 5
# A detached session's `started_at` is stamped after its PowerShell launch
# returns (the pid liveness check needs that), and Copilot starts inside the
# launch, so its folder can predate `started_at` by up to the launch's own
# timeout (`remote._DETACHED_SPAWN_TIMEOUT_S`, 30 s) plus the skew.
_COPILOT_DETACHED_LAUNCH_SECONDS = 35
# `workspace.yaml` is ~500 bytes. Read bounded anyway, like every reader here.
_COPILOT_YAML_CAP_BYTES = 64 * 1024
# `\r` is matched explicitly on both sides: `re.M`'s `$` stops before a `\n`
# but leaves a CRLF file's `\r` in the way, which would strand it inside the
# captured `cwd` and block the `created_at` match outright. The real files
# here are LF, but nothing about the format promises that.
_COPILOT_CWD_RE = re.compile(r"^cwd:[ \t]*(.+?)[ \t\r]*$", re.M)
_COPILOT_CREATED_RE = re.compile(r"^created_at:[ \t]*(\S+?)[ \t\r]*$", re.M)
# `cwd` and `created_at` are fixed when Copilot creates a session folder, so a
# sidecar that parsed once is never read again: the Chat pane, its live tick
# and the Board all resolve on every request, and this box keeps hundreds of
# folders. Only a successful parse is kept, so a sidecar caught half-written
# is read again next time. Bounded by a clear, not an LRU: past this size the
# next scan simply re-reads.
_COPILOT_WORKSPACE_CACHE: Dict[Path, Tuple[str, float]] = {}
_COPILOT_WORKSPACE_CACHE_MAX = 8192
# A session's resolved event log, valid while the state folder's own mtime is
# unchanged: adding or removing a session folder moves it, and that is the
# only change that can make a match ambiguous or point it elsewhere.
_COPILOT_MATCH_CACHE: Dict[Tuple[str, str, float, float], Tuple[int, Path]] = {}
_COPILOT_MATCH_CACHE_MAX = 256
# Copilot session ids are UUIDs; a folder name is only built from one.
_COPILOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
# Per event log: the byte offset scanned so far and the resume instants seen.
_COPILOT_RESUME_CACHE: Dict[Path, Tuple[int, List[float]]] = {}
# A cheap byte filter; the parsed record's own ``type`` decides.
_COPILOT_RESUME_MARK = b'"session.resume"'
_COPILOT_RESUME_CHUNK_BYTES = 1024 * 1024
_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
# Claude Code names a project folder after its cwd with every non-alphanumeric
# character replaced by `-`. Measured across the 107 folders on this box whose
# own JSONL records a `cwd`: 103 match that transform exactly and 4 older ones
# are additionally lowercased, so the folder is matched case-insensitively
# rather than by building one name.
_CLAUDE_SLUG_RE = re.compile(r"[^A-Za-z0-9]")


def _started_epoch(session: Dict[str, Any]) -> Optional[float]:
    """A live session's ``started_at`` as epoch seconds, or None.

    The session-host reports it as a float, but a row read back from JSON
    state can carry an ISO string, so both are accepted.
    """
    raw = session.get("started_at")
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OSError):
        return None


def _copilot_workspace(path: Path) -> Tuple[str, Optional[float]]:
    """``(cwd, created_at epoch)`` from one Copilot ``workspace.yaml``.

    Two flat scalars off the top of a ~500-byte file, read with a regex
    rather than a YAML parser: the repo has no YAML dependency, and the two
    keys wanted are unquoted one-line scalars. A file that is missing,
    unreadable or malformed answers ``("", None)``, which no session matches.
    """
    cached = _COPILOT_WORKSPACE_CACHE.get(path)
    if cached is not None:
        return cached
    try:
        with path.open("rb") as fh:
            raw = fh.read(_COPILOT_YAML_CAP_BYTES)
    except OSError:
        return "", None
    text = raw.decode("utf-8", errors="replace")
    cwd_match = _COPILOT_CWD_RE.search(text)
    created_match = _COPILOT_CREATED_RE.search(text)
    if not cwd_match or not created_match:
        return "", None
    try:
        created = datetime.fromisoformat(
            created_match.group(1).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError, OSError):
        return "", None
    parsed = (_normalize_dir(cwd_match.group(1)), created)
    if len(_COPILOT_WORKSPACE_CACHE) >= _COPILOT_WORKSPACE_CACHE_MAX:
        _COPILOT_WORKSPACE_CACHE.clear()
    _COPILOT_WORKSPACE_CACHE[path] = parsed
    return parsed


def find_copilot_transcript(session: Dict[str, Any]) -> Optional[Path]:
    """The Copilot CLI event log for a live session.

    Copilot writes the launcher nothing: its hooks don't fire in the
    interactive TUI, so there is no sessions-state row, and a grep of a
    launcher-spawned session's whole log for the launcher's own session id
    finds zero hits. What it *does* write, at launch and before any prompt,
    is a per-session folder holding ``workspace.yaml`` with the two fields
    this correlates on — ``cwd`` and ``created_at``.

    So this is a launch-window match against the harness's own sidecar: a folder
    qualifies when its ``cwd`` is this session's ``project_dir`` and its
    ``created_at`` falls within :data:`_COPILOT_START_SLOP_SECONDS` after
    the session's ``started_at`` (measured: 3.71 s and 3.72 s).

    It fails safe — **anything other than
    exactly one qualifying folder returns ``None``**, so the caller answers
    "no transcript" rather than risking a neighbouring session's text. Two
    sessions launched in one folder inside the window are ambiguous by
    construction and both refuse; being *outside* each other's window is
    what makes the ordinary case unambiguous, not a tie-break.

    No folder in the window → :func:`_scan_resumed_workspaces` looks for
    the older folder a ``--resume`` launch reopened.

    A qualifying folder whose ``events.jsonl`` does not exist yet also
    answers ``None`` — Copilot creates that file at the first prompt, so a
    session nobody has typed into genuinely has no transcript. Older
    sessions on this box keep a ``session.db`` instead (28 of 71) and are
    refused by the same check.

    Cost: the Chat pane's open and live tick and the Board's poll all call
    this, and the state folder holds hundreds of sessions, so it is cached
    twice. A found log is reused while the state folder's mtime holds (one
    ``stat``), and each sidecar is parsed once in its life; only a folder
    added or removed makes the next call list the folder again.
    """
    project_dir = _normalize_dir(session.get("project_dir"))
    started = _started_epoch(session)
    if not project_dir or started is None:
        return None
    early_slop = (
        _COPILOT_DETACHED_LAUNCH_SECONDS
        if session.get("kind") == "remote"
        else _COPILOT_CLOCK_SKEW_SECONDS
    )
    try:
        listing_sig = _COPILOT_STATE_DIR.stat().st_mtime_ns
    except OSError:
        return None
    key = (str(_COPILOT_STATE_DIR), project_dir, started, early_slop)
    hit = _COPILOT_MATCH_CACHE.get(key)
    if hit is not None and hit[0] == listing_sig and hit[1].is_file():
        return hit[1]
    found = _scan_copilot_workspaces(project_dir, started, early_slop)
    if found is None:
        found = _scan_resumed_workspaces(project_dir, started, early_slop)
    if found is not None:
        if len(_COPILOT_MATCH_CACHE) >= _COPILOT_MATCH_CACHE_MAX:
            _COPILOT_MATCH_CACHE.clear()
        _COPILOT_MATCH_CACHE[key] = (listing_sig, found)
    return found


def _scan_copilot_workspaces(
    project_dir: str, started: float, early_slop: float
) -> Optional[Path]:
    """:func:`find_copilot_transcript`'s uncached walk of every sidecar."""
    try:
        workspaces = list(_COPILOT_STATE_DIR.glob("*/workspace.yaml"))
    except OSError:
        return None

    matches: List[Path] = []
    for workspace in workspaces:
        cwd, created = _copilot_workspace(workspace)
        if created is None or cwd != project_dir:
            continue
        delta = created - started
        if -early_slop <= delta <= _COPILOT_START_SLOP_SECONDS:
            matches.append(workspace.parent / "events.jsonl")
        if len(matches) > 1:
            return None
    if len(matches) != 1 or not matches[0].is_file():
        return None
    return matches[0]


def copilot_events_for_id(copilot_session_id: Optional[str]) -> Optional[Path]:
    """The event log Copilot keeps for one of its own session ids, or None.

    Copilot names each session folder after its session id, which is also
    the key the Copilot hooks write the sessions-state row under, so a row
    names its log exactly even before agentStop records a path, and even
    for a resumed launch whose folder predates the launch window. Anything
    that is not a plain id is refused rather than joined onto the path.
    """
    if not copilot_session_id or not _COPILOT_ID_RE.match(copilot_session_id):
        return None
    path = _COPILOT_STATE_DIR / copilot_session_id / "events.jsonl"
    return path if path.is_file() else None


def _scan_resumed_workspaces(
    project_dir: str, started: float, early_slop: float
) -> Optional[Path]:
    """The folder a resumed launch reopened, when the launch-window match
    found none.

    ``--resume`` reopens a folder Copilot created long before this launch,
    so its ``created_at`` never falls in the window; what marks it is the
    ``session.resume`` event Copilot appends at the launch. A folder
    qualifies when its ``cwd`` is this session's and its log records a
    resume inside the same window; only logs written since the launch are
    read. The same fail-safe holds: anything but exactly one is None.
    """
    try:
        workspaces = list(_COPILOT_STATE_DIR.glob("*/workspace.yaml"))
    except OSError:
        return None
    earliest = started - early_slop
    matches: List[Path] = []
    for workspace in workspaces:
        cwd, created = _copilot_workspace(workspace)
        if created is None or cwd != project_dir or created >= earliest:
            continue
        events = workspace.parent / "events.jsonl"
        try:
            if events.stat().st_mtime < earliest:
                continue
        except OSError:
            continue
        if any(earliest <= t <= started + _COPILOT_START_SLOP_SECONDS
               for t in _copilot_resume_times(events)):
            matches.append(events)
        if len(matches) > 1:
            return None
    return matches[0] if len(matches) == 1 else None


def _copilot_resume_times(events: Path) -> List[float]:
    """Every ``session.resume`` instant in one event log, as epoch seconds.

    The log only grows, so each file is read once from the offset the last
    call reached: the live tick asks on every poll while a session stays
    unresolved, and a long session's log runs to hundreds of MB. A shrunk
    file was replaced and is read again from the start.
    """
    try:
        size = events.stat().st_size
    except OSError:
        return []
    offset, times = _COPILOT_RESUME_CACHE.get(events, (0, []))
    if size < offset:
        offset, times = 0, []
    times = list(times)
    try:
        with events.open("rb") as fh:
            fh.seek(offset)
            carry = b""
            while True:
                chunk = fh.read(_COPILOT_RESUME_CHUNK_BYTES)
                if not chunk:
                    break
                lines = (carry + chunk).split(b"\n")
                carry = lines.pop()
                offset += len(chunk)
                for line in lines:
                    if _COPILOT_RESUME_MARK in line:
                        stamp = _resume_instant(line)
                        if stamp is not None:
                            times.append(stamp)
            # An unfinished last line is read again once it is complete.
            offset -= len(carry)
    except OSError:
        return times
    if len(_COPILOT_RESUME_CACHE) >= _COPILOT_MATCH_CACHE_MAX:
        _COPILOT_RESUME_CACHE.clear()
    _COPILOT_RESUME_CACHE[events] = (offset, times)
    return times


def _resume_instant(line: bytes) -> Optional[float]:
    """One ``session.resume`` line's timestamp as epoch seconds, or None."""
    try:
        record = json.loads(line)
        if record.get("type") != "session.resume":
            return None
        return datetime.fromisoformat(
            str(record["timestamp"]).replace("Z", "+00:00")
        ).timestamp()
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        return None


def _claude_project_folders(project_dir: str) -> Optional[List[Path]]:
    """The ``~/.claude/projects`` folders for a cwd, or None if unreadable.

    Built from the *normalized* directory so the session's own spelling —
    separator flavour, trailing slash, drive-letter case — cannot change the
    folder it looks for; matched case-insensitively for the legacy
    lowercased folders.
    """
    slug = _CLAUDE_SLUG_RE.sub("-", _normalize_dir(project_dir))
    try:
        return [
            child
            for child in _CLAUDE_PROJECTS_DIR.iterdir()
            if child.name.lower() == slug and child.is_dir()
        ]
    except OSError:
        return None


def _claude_declared_titles(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """The newest ``custom-title`` and ``ai-title`` a conversation declares.

    Claude Code names a conversation in two independent records and both are
    in play: ``{"type": "ai-title", "aiTitle": ...}`` is the name it infers
    for itself, and ``{"type": "custom-title", "customTitle": ...}`` is an
    explicit one that overrides it in the window title. They are not
    interchangeable and they do drift apart — the standing fleet chief on
    this box carries ``customTitle: "chief"`` and
    ``aiTitle: "Investigate and fix failed jobs"`` in the *same* file, the
    ai-title frozen at what it inferred early (one distinct value across all
    38,094 lines) while the PTY paints the custom one. Reading only
    ``ai-title`` alone would have made the chief's own
    drawer disagree with itself permanently.

    Both are returned rather than resolved to one here, so the caller can
    accept a match against *either* — the fail-safe direction, since it can
    only ever refuse fewer answers.
    """
    custom: Optional[str] = None
    ai: Optional[str] = None
    for line in _read_tail(path, _TITLE_TAIL_BYTES).splitlines():
        # Cheap reject first: these records are a few per thousand lines and
        # the window is 256 KB of mostly message text.
        if '-title"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue  # a torn first line from the tail cut, or not a record
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "custom-title":
            value = str(obj.get("customTitle") or "").strip()
            if value:
                custom = value
        elif kind == "ai-title":
            value = str(obj.get("aiTitle") or "").strip()
            if value:
                ai = value
    return custom, ai


def _normalize_dir(raw: Any) -> str:
    return str(raw or "").replace("\\", "/").rstrip("/").lower()


def _read_tail(path: Path, n_bytes: int) -> str:
    return _read_tail_bytes(path, n_bytes).decode("utf-8", errors="replace")
