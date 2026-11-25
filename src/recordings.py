"""Dictation takes kept on disk while they are recorded, so none is lost.

The record button streams each take here in chunks of a few seconds while the
normal single-shot upload to ``/api/transcribe`` carries on unchanged. A tab
that crashes or reloads mid-take, or a transcription that fails, therefore
still leaves the audio on disk; Settings > Speech to text lists the takes and
can play one or send it to the speech endpoint again.

Store: ``<audit dir>/recordings/<id>.<ext>`` (the browser's own recording,
appended as it arrives, never re-encoded, until the page swaps in its 16 kHz
mono WAV of the finished take) plus ``<id>.json`` (the take's status). The audit directory is the one ``LAUNCHER_AUDIT_DIR`` relocates, so a
test run never writes into the checkout's live ``webapp/``. Gitignored.

Recordings are private audio: nothing here logs more than sizes and statuses.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import runtime_files
from src.audit import AUDIT_DIR_ENV, PROJECT_ROOT

logger = logging.getLogger(__name__)

STATUS_RECORDING = "recording"
STATUS_STOPPED = "stopped"
STATUS_TRANSCRIBED = "transcribed"
STATUS_FAILED = "failed"
# Derived, never stored: a take still marked recording that has had no chunk
# for STALE_AFTER_S, because its page died before it could finish.
STATUS_INCOMPLETE = "incomplete"
# Outcomes the browser may report when a take ends.
FINAL_STATUSES = (STATUS_STOPPED, STATUS_TRANSCRIBED, STATUS_FAILED)

# The browser sends a chunk every few seconds; ten silent slices means the
# page is gone, not slow.
STALE_AFTER_S = 30.0
# A take past this is refused further chunks, so a stuck recorder cannot fill
# the drive. Two hours of browser Opus is well under it.
MAX_TAKE_BYTES = 300 * 1024 * 1024
_MAX_ERROR_CHARS = 300

_ID = re.compile(r"^[0-9a-f]{32}$")
_EXT_BY_MIME = (("mp4", "mp4"), ("ogg", "ogg"), ("wav", "wav"), ("webm", "webm"))
_lock = threading.Lock()


class RecordingError(ValueError):
    """A request about a take that cannot be honoured; ``status`` is its HTTP code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def recordings_dir() -> Path:
    """Where takes live; resolved per call so a test's ``LAUNCHER_AUDIT_DIR`` applies."""
    override = os.environ.get(AUDIT_DIR_ENV, "").strip()
    return (Path(override) if override else PROJECT_ROOT / "webapp") / "recordings"


def _ext_for(mime: str) -> str:
    lowered = (mime or "").lower()
    for needle, ext in _EXT_BY_MIME:
        if needle in lowered:
            return ext
    return "webm"


def _meta_path(take_id: str) -> Path:
    if not _ID.match(take_id or ""):
        raise RecordingError("unknown recording", status=404)
    return recordings_dir() / f"{take_id}.json"


def _read_meta(take_id: str) -> Dict[str, Any]:
    path = _meta_path(take_id)
    # Missing is a 404 and unreadable a 500, so this is not read_json_or.
    if not path.is_file():
        raise RecordingError("unknown recording", status=404)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecordingError(f"recording metadata unreadable ({type(exc).__name__})", status=500) from exc


def _write_meta(meta: Dict[str, Any]) -> None:
    runtime_files.save_text(_meta_path(meta["id"]), json.dumps(meta))


def audio_path(take_id: str) -> Path:
    """The take's audio file (it may not exist yet: no chunk has arrived)."""
    meta = _read_meta(take_id)
    return recordings_dir() / f"{take_id}.{meta.get('ext', 'webm')}"


def mime_of(take_id: str) -> str:
    """The content type the browser recorded the take in."""
    return str(_read_meta(take_id).get("mime") or "audio/webm")


def prune(keep_hours: int, now: Optional[float] = None) -> int:
    """Delete every take older than ``keep_hours``; all of them when it is 0."""
    folder = recordings_dir()
    if not folder.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - max(keep_hours, 0) * 3600
    removed = 0
    with _lock:
        # A take's audio and status file go together, aged by the newer of
        # the two, so a take is never left half-deleted.
        takes: Dict[str, List[Path]] = {}
        try:
            for path in folder.iterdir():
                if path.is_file():
                    takes.setdefault(path.stem, []).append(path)
        except OSError as exc:
            logger.warning("⚠️ could not list recordings for pruning: %s", exc)
            return 0
        for paths in takes.values():
            try:
                if keep_hours > 0 and max(p.stat().st_mtime for p in paths) >= cutoff:
                    continue
                for path in paths:
                    path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("⚠️ could not prune recording %s: %s", paths[0].stem, exc)
    if removed:
        logger.info("ℹ️ pruned %d dictation recording(s)", removed)
    return removed


def create(mime: str, keep_hours: int, now: Optional[float] = None) -> Optional[str]:
    """Start a take and return its id; ``None`` when keeping recordings is off."""
    prune(keep_hours, now)
    if keep_hours <= 0:
        return None
    runtime_files.make_dirs(recordings_dir())
    take_id = uuid.uuid4().hex
    meta = {
        "id": take_id,
        "created": now if now is not None else time.time(),
        "mime": (mime or "audio/webm")[:80],
        "ext": _ext_for(mime),
        "ms": 0,
        "status": STATUS_RECORDING,
        "error": "",
    }
    with _lock:
        _write_meta(meta)
    logger.info("ℹ️ dictation recording started (%s)", meta["ext"])
    return take_id


def append(take_id: str, data: bytes, elapsed_ms: int) -> int:
    """Append one chunk as it came from the recorder; returns the file's size."""
    with _lock:
        meta = _read_meta(take_id)
        if meta.get("status") != STATUS_RECORDING:
            raise RecordingError("recording already finished", status=409)
        path = recordings_dir() / f"{take_id}.{meta['ext']}"
        size = path.stat().st_size if path.exists() else 0
        if size + len(data) > MAX_TAKE_BYTES:
            raise RecordingError("recording too large", status=413)
        with runtime_files.open_for_write(path, "ab") as handle:
            handle.write(data)
        if elapsed_ms > meta.get("ms", 0):
            meta["ms"] = int(elapsed_ms)
            _write_meta(meta)
    return size + len(data)


def replace_with_wav(take_id: str, wav: bytes) -> int:
    """Keep the browser's 16 kHz mono WAV of the take instead of the recorder's copy.

    A phone records MP4 that a later retry cannot always convert; the WAV the
    page already built for the upload can always be sent as it is. The
    recorder's copy goes only once the WAV is on disk, so a failed write loses
    nothing.
    """
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise RecordingError("not a WAV file")
    if len(wav) > MAX_TAKE_BYTES:
        raise RecordingError("recording too large", status=413)
    with _lock:
        meta = _read_meta(take_id)
        old = recordings_dir() / f"{take_id}.{meta.get('ext', 'webm')}"
        target = recordings_dir() / f"{take_id}.wav"
        runtime_files.save_bytes(target, wav)
        if old != target:
            old.unlink(missing_ok=True)
        meta.update(ext="wav", mime="audio/wav")
        _write_meta(meta)
    logger.info("ℹ️ dictation recording kept as WAV (%d bytes)", len(wav))
    return len(wav)


def set_status(take_id: str, status: str, error: str = "", note: Optional[str] = None) -> None:
    """Record how a take ended: stopped, transcribed or failed (with the error).

    ``note`` is why the browser could not convert the take to WAV before
    uploading it. The browser reports it with every outcome it sends, ``""``
    when it converted; a server-side retry passes none and leaves the reason
    the live take recorded.
    """
    if status not in FINAL_STATUSES:
        raise RecordingError(f"status must be one of {FINAL_STATUSES}")
    with _lock:
        meta = _read_meta(take_id)
        meta["status"] = status
        meta["error"] = (error or "")[:_MAX_ERROR_CHARS] if status == STATUS_FAILED else ""
        if note is not None:
            meta["note"] = note[:_MAX_ERROR_CHARS]
        _write_meta(meta)
    logger.info("ℹ️ dictation recording marked %s%s", status, f" (browser conversion: {note})" if note else "")


def _row(meta: Dict[str, Any], audio: Path, now: float) -> Dict[str, Any]:
    try:
        stat = audio.stat()
        size, last_write = stat.st_size, stat.st_mtime
    except OSError:
        size, last_write = 0, float(meta.get("created", 0))
    status = meta.get("status", STATUS_RECORDING)
    if status == STATUS_RECORDING and now - last_write > STALE_AFTER_S:
        status = STATUS_INCOMPLETE
    return {
        "id": meta["id"],
        "created": meta.get("created", 0),
        "ms": meta.get("ms", 0),
        "bytes": size,
        "mime": meta.get("mime", ""),
        "status": status,
        "error": meta.get("error", ""),
        "note": meta.get("note", ""),
    }


def list_takes(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Every kept take, newest first."""
    folder = recordings_dir()
    if not folder.is_dir():
        return []
    current = now if now is not None else time.time()
    rows = []
    for meta_file in folder.glob("*.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not _ID.match(str(meta.get("id", ""))):
            continue
        rows.append(_row(meta, folder / f"{meta['id']}.{meta.get('ext', 'webm')}", current))
    rows.sort(key=lambda r: r["created"], reverse=True)
    return rows
