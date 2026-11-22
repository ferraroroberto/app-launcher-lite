"""Save diagnostics: one local, redacted, time-ordered file of recent logs.

When Lite misbehaves on another computer, nobody can read that machine's logs
from here. This module gathers the last N minutes of every log the Lite
processes write into one plain-text file a person can read, search or send:

* every ``*.log`` (and rotated ``*.log.N``) in ``webapp/``: the webapp's
  warnings and slow requests, the session-host's output, the tray's watchdog
  and restart lines, the chief doorbell's rings and skips, the chief events
  the webapp publishes, the terminal audit's session starts and stops;
* ``chief-decisions.log`` beside the chief mailbox: each mailbox event write,
  claim and ack and each lane mark, written by the Copilot hooks and the
  chief's helpers;
* a snapshot taken now: the build sha, each process's pid and start time, the
  mailbox's pending and claimed events, the lane marks and the session rows.

Each log line is tagged with its source and the lines are merged in time
order. A line with no timestamp of its own (a traceback, a server's bare
warning) travels with the line above it. Per-session input logs under
``webapp/sessions/`` are never read: they hold what the owner typed.

Everything passes :func:`redact` before it is written: auth headers, cookies,
bearer values, ``key=value`` pairs whose key names a secret, known token
shapes, and the literal value of every secret ``config/webapp_config.json``
holds. The file stays on this PC; nothing is uploaded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from src import runtime_files
from src._json_io import read_json_or
from src.app_config import resolve_session_host_port, resolve_webapp_port
from src.build_info import build_identity
from src.diagnostics import listener_pids

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover -- psutil is in requirements.txt
    psutil = None  # type: ignore

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "webapp"
OUTPUT_DIR = LOG_DIR / "diagnostics"
DECISIONS_LOG_NAME = "chief-decisions.log"

#: The windows Settings offers; the CLI accepts any whole number of minutes.
WINDOW_CHOICES_MINUTES = (15, 60, 180)
MAX_WINDOW_MINUTES = 24 * 60

#: How much of each log's tail is read. A busy warnings log grows a few MB a
#: day, so 4 MB covers far more than the longest window without loading
#: rotated history whole.
MAX_TAIL_BYTES = 4 * 1024 * 1024

#: A file with no timestamps at all contributes only this many last lines,
#: stamped with its modification time, and only when it changed in the window.
UNTIMED_TAIL_LINES = 50

#: Log files the pre-ship gate writes for its disposable instances, not this PC's Lite.
_SKIPPED_PREFIXES = ("e2e-", "verify-progress")

FILE_NAME_RE = re.compile(r"^diagnostics-\d{8}-\d{6}\.txt$")

REDACTED = "[REDACTED]"

# ------------------------------------------------------------------ redaction

_CREDENTIAL_NAME = (
    r"[A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|pwd|api[_-]?key|apikey|"
    r"access[_-]?key|private[_-]?key|client[_-]?secret|credential|signature|"
    r"auth)[A-Za-z0-9_.-]*"
)

_REDACTIONS: Tuple[Tuple[re.Pattern, str], ...] = (
    # A PEM private key block, from its BEGIN marker to its END marker.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     REDACTED),
    # Header lines: everything after the name.
    (re.compile(r"(?i)\b((?:proxy-)?authorization|set-cookie|cookie|x-terminal-token|x-api-key)"
                r"(\s*[:=]\s*)\S[^\r\n]*"), r"\1\2" + REDACTED),
    (re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1\2" + REDACTED),
    # A URL query value whose name is a credential.
    (re.compile(r"(?i)([?&](?:tt|token|access_token|key|sig|code|password)=)[^&\s\"'#]+"),
     r"\1" + REDACTED),
    # key=value / "key": "value" whose key names a secret.
    (re.compile(r"(?i)([\"']?" + _CREDENTIAL_NAME + r"[\"']?\s*[:=]\s*)([\"']?)([^\"'\s,;&}\]]+)"),
     r"\1\2" + REDACTED),
    # Known token shapes, wherever they stand.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
)

#: A configured secret shorter than this is not replaced verbatim: a 3-char
#: value would blank unrelated words all over the file.
_MIN_LITERAL_SECRET = 6


def configured_secrets(cfg: Any) -> List[str]:
    """Every secret value the webapp config holds, longest first."""
    values: List[str] = []
    for name in ("auth_token", "auth_password", "pushover_api_token",
                 "pushover_user_key", "telegram_bot_token"):
        value = getattr(cfg, name, "")
        if isinstance(value, str):
            values.append(value)
    secrets = getattr(cfg, "secrets", None)
    if isinstance(secrets, dict):
        values.extend(v for v in secrets.values() if isinstance(v, str))
    kept = {v.strip() for v in values if len(v.strip()) >= _MIN_LITERAL_SECRET}
    return sorted(kept, key=len, reverse=True)


def redact(text: str, literals: Sequence[str] = ()) -> str:
    """``text`` with every credential this module recognises replaced by
    :data:`REDACTED`. ``literals`` are exact values to blank as well."""
    for literal in literals:
        if literal:
            text = text.replace(literal, REDACTED)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


# ----------------------------------------------------------------- timestamps

_ISO_STAMP = re.compile(
    r"^\[?(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:[,.](\d{1,6}))?"
)
_TIME_ONLY = re.compile(r"^(\d{2}):(\d{2}):(\d{2}) \[")
_DMY_STAMP = re.compile(r"^\[(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})")


def _fraction(raw: Optional[str]) -> int:
    return int((raw or "0").ljust(6, "0")[:6])


def parse_stamp(line: str, file_day: datetime) -> Optional[datetime]:
    """The local time a log line starts with, or ``None`` for a continuation.

    A time-only stamp (``12:00:01 [INFO]``) takes ``file_day``'s date, or the
    day before when it would otherwise sit after ``file_day``.
    """
    m = _ISO_STAMP.match(line)
    try:
        if m:
            y, mo, d, h, mi, s, frac = m.groups()
            return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), _fraction(frac))
        m = _DMY_STAMP.match(line)
        if m:
            d, mo, y, h, mi, s = m.groups()
            return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
        m = _TIME_ONLY.match(line)
        if m:
            h, mi, s = (int(g) for g in m.groups())
            stamp = file_day.replace(hour=h, minute=mi, second=s, microsecond=0)
            return stamp - timedelta(days=1) if stamp > file_day else stamp
    except ValueError:
        return None
    return None


# --------------------------------------------------------------------- reading


@dataclass
class Entry:
    """One log record: its time, its source tag and its lines."""

    at: datetime
    source: str
    lines: List[str]


def tail_lines(path: Path, max_bytes: int = MAX_TAIL_BYTES) -> List[str]:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - max_bytes)
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    # A read that started mid-file cut its first line.
    return lines[1:] if start and lines else lines


def source_tag(path: Path) -> str:
    """``webapp-warnings`` for ``webapp-warnings.log`` and its ``.log.1``."""
    name = path.name
    return name.split(".log", 1)[0] if ".log" in name else path.stem


def read_entries(path: Path, start: datetime, end: datetime) -> List[Entry]:
    """``path``'s records inside ``[start, end]``, continuations attached."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if mtime < start:
            return []
        lines = tail_lines(path)
    except OSError as exc:
        return [Entry(end, source_tag(path), [f"(unreadable: {exc})"])]
    tag = source_tag(path)
    entries: List[Entry] = []
    current: Optional[Entry] = None
    for line in lines:
        stamp = parse_stamp(line, mtime)
        if stamp is not None:
            current = Entry(stamp, tag, [line]) if start <= stamp <= end else None
            if current is not None:
                entries.append(current)
        elif current is not None and line.strip():
            current.lines.append(line)
    if not entries and not any(parse_stamp(ln, mtime) for ln in lines):
        tail = [ln for ln in lines if ln.strip()][-UNTIMED_TAIL_LINES:]
        if tail:
            note = f"(no timestamps in this log; its last {len(tail)} lines, file changed {mtime:%H:%M:%S})"
            entries.append(Entry(mtime, tag, [note, *tail]))
    return entries


def log_files(log_dir: Path, extra: Iterable[Path] = ()) -> List[Path]:
    """The logs to read: ``log_dir``'s own (not its subfolders) plus ``extra``."""
    found: List[Path] = []
    try:
        for path in sorted(log_dir.iterdir()):
            name = path.name
            if not path.is_file() or name.startswith(_SKIPPED_PREFIXES):
                continue
            if name.endswith(".log") or re.search(r"\.log\.\d+$", name):
                found.append(path)
    except OSError as exc:
        logger.warning(f"⚠️ diagnostics: cannot list {log_dir}: {exc}")
    found.extend(p for p in extra if p.is_file())
    return found


# -------------------------------------------------------------------- snapshot


def _age(now_ms: int, stamp_ms: int) -> str:
    seconds = max(0, (now_ms - stamp_ms) // 1000)
    return f"{seconds // 60}m{seconds % 60:02d}s"


def mailbox_lines(inbox: Path, now: datetime) -> List[str]:
    """Each pending and claimed event in the chief mailbox, with its age."""
    now_ms = int(now.timestamp() * 1000)
    out: List[str] = []
    for label, folder in (("pending", inbox), ("claimed", inbox / ".claimed")):
        try:
            names = sorted(p.name for p in folder.iterdir()
                           if p.is_file() and p.name.endswith(".json") and not p.name.startswith("."))
        except FileNotFoundError:
            names = []
        except OSError as exc:
            out.append(f"{label}: unreadable ({exc})")
            continue
        out.append(f"{label}: {len(names)}")
        for name in names:
            stamp = name.split("-", 1)[0]
            age = _age(now_ms, int(stamp)) if stamp.isdigit() else "age unknown"
            out.append(f"  {name}  {label} {age}")
    return out


def json_rows_lines(path: Path) -> List[str]:
    """One line per row of a ``{key: row}`` state file, or why it has none."""
    if not path.exists():
        return [f"{path.name}: absent"]
    data = read_json_or(path, None, path.name, expect=dict)
    if data is None:
        return [f"{path.name}: unreadable, or not a JSON object"]
    out = [f"{path.name}: {len(data)} rows"]
    for key, row in data.items():
        out.append(f"  {key}: {json.dumps(row, ensure_ascii=False, sort_keys=True)}")
    return out


def _process_line(label: str, pid: Optional[int]) -> str:
    if not pid:
        return f"{label}: not found"
    if psutil is None:
        return f"{label}: pid {pid}, start time unknown (psutil missing)"
    try:
        started = datetime.fromtimestamp(psutil.Process(pid).create_time())
    except (psutil.Error, OSError, ValueError) as exc:
        return f"{label}: pid {pid}, start time unknown ({type(exc).__name__})"
    return f"{label}: pid {pid}, started {started:%Y-%m-%d %H:%M:%S}"


def process_lines(webapp_port: int, session_host_port: int, tray_pid_file: Path) -> List[str]:
    """The webapp, session-host and tray processes with their start times."""
    out: List[str] = []
    for label, port in (("webapp", webapp_port), ("session-host", session_host_port)):
        pids = listener_pids(port)
        if pids is None:
            out.append(f"{label} :{port}: unknown (socket table unreadable)")
        elif not pids:
            out.append(f"{label} :{port}: not listening")
        else:
            out.extend(_process_line(f"{label} :{port}", pid) for pid in pids)
    try:
        raw = tray_pid_file.read_text(encoding="utf-8").strip()
        out.append(_process_line("tray", int(raw) if raw.isdigit() else None))
    except OSError:
        out.append("tray: no pid file")
    return out


# ------------------------------------------------------------------- assembly


@dataclass
class Sources:
    """Where to read: the logs folder, the chief's state files, the ports."""

    log_dir: Path
    inbox_dir: Path
    managed_file: Path
    sessions_file: Path
    webapp_port: int
    session_host_port: int
    tray_pid_file: Path

    @property
    def decisions_logs(self) -> List[Path]:
        """The decisions log beside the mailbox, its rolled ``.1`` first."""
        current = self.inbox_dir.parent / DECISIONS_LOG_NAME
        return [current.with_name(DECISIONS_LOG_NAME + ".1"), current]


def sources_from_config(cfg: Any) -> Sources:
    """The live sources for this checkout, from the webapp config."""
    return Sources(
        log_dir=LOG_DIR,
        inbox_dir=Path(cfg.chief_inbox_dir),
        managed_file=Path(cfg.chief_managed_file),
        sessions_file=Path(cfg.sessions_state_file),
        webapp_port=resolve_webapp_port(),
        session_host_port=resolve_session_host_port(),
        tray_pid_file=PROJECT_ROOT / ".tray.pid",
    )


def build_text(sources: Sources, minutes: int, *, now: Optional[datetime] = None,
               served_sha: Optional[str] = None) -> Tuple[str, int]:
    """The bundle's text (not yet redacted) and its count of log records."""
    end = now or datetime.now()
    start = end - timedelta(minutes=minutes)
    head = [
        "App Launcher Lite diagnostics",
        f"generated: {end:%Y-%m-%d %H:%M:%S} (this PC's local time)",
        f"window: last {minutes} minutes, {start:%Y-%m-%d %H:%M:%S} to {end:%H:%M:%S}",
        f"checkout build: {build_identity().get('git_sha', 'unknown')}",
    ]
    if served_sha:
        head.append(f"webapp serving build: {served_sha}")
    head.append("secrets, tokens, keys, passwords, cookies and auth headers are redacted")
    sections = [
        ("Processes", process_lines(sources.webapp_port, sources.session_host_port,
                                    sources.tray_pid_file)),
        ("Chief mailbox now", mailbox_lines(sources.inbox_dir, end)),
        ("Lane marks now", json_rows_lines(sources.managed_file)),
        ("Sessions now", json_rows_lines(sources.sessions_file)),
    ]
    entries: List[Entry] = []
    for path in log_files(sources.log_dir, sources.decisions_logs):
        entries.extend(read_entries(path, start, end))
    entries.sort(key=lambda e: e.at)
    width = max((len(e.source) for e in entries), default=0)
    body = [f"[{e.source:<{width}}] {line}" for e in entries for line in e.lines]
    parts = ["\n".join(head)]
    parts += [f"== {title} ==\n" + "\n".join(lines) for title, lines in sections]
    parts.append(f"== Logs, oldest first ({len(entries)} records) ==\n" + "\n".join(body))
    return "\n\n".join(parts) + "\n", len(entries)


@dataclass
class SavedBundle:
    path: Path
    records: int
    bytes: int


def save_bundle(sources: Sources, minutes: int, *, literals: Sequence[str] = (),
                out_dir: Optional[Path] = None, now: Optional[datetime] = None,
                served_sha: Optional[str] = None) -> SavedBundle:
    """Collect, redact and write one bundle; its path, record count and size."""
    if not 1 <= int(minutes) <= MAX_WINDOW_MINUTES:
        raise ValueError(f"minutes must be between 1 and {MAX_WINDOW_MINUTES}")
    moment = now or datetime.now()
    began = time.monotonic()
    text, records = build_text(sources, int(minutes), now=moment, served_sha=served_sha)
    text = redact(text, literals)
    out_dir = out_dir or OUTPUT_DIR
    runtime_files.make_dirs(out_dir)
    target = out_dir / f"diagnostics-{moment:%Y%m%d-%H%M%S}.txt"
    runtime_files.save_text(target, text, encoding="utf-8")
    size = len(text.encode("utf-8"))
    logger.info(
        f"✅ diagnostics saved: {target.name}, {minutes} min, {records} records, "
        f"{size} bytes in {time.monotonic() - began:.1f}s"
    )
    return SavedBundle(target, records, size)


def bundle_path(name: str, out_dir: Optional[Path] = None) -> Optional[Path]:
    """The saved bundle called ``name``, or ``None`` for any other name."""
    if not FILE_NAME_RE.match(name or ""):
        return None
    path = (out_dir or OUTPUT_DIR) / name
    return path if path.is_file() else None
