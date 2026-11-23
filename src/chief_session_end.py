"""Publish a chief ``session-end`` event for a session that died without a hook.

A worker the fleet chief dispatched reports its end through its own ``Stop`` /
``SessionEnd`` hook, which writes an event into the chief's mailbox. A worker
killed too hard to run any hook leaves nothing. The session-host's reaper
already notices every dead session, so it calls :func:`publish_session_ends`
with the sessions it just dropped; each one the chief registry names gets one
``session-end`` event.

The mailbox contract is fleet-config's ``docs/chief-mailbox.md``: one file per
event, written to a temp file in the same directory (``.event.<random>.tmp``)
and ``os.replace``d to ``<published_ms>-<id>.json``, so a reader never sees a
partial ``.json``. Nothing is imported from fleet-config; the file format is the
whole interface.

"Could not tell" is not "not managed": an unreadable or malformed registry
logs an *undetermined* line and writes nothing. A missing registry file is a
determination (nothing was ever dispatched), and stays quiet.

Each published event leaves a ``.<kind>-<sid>.stamp`` marker in the inbox
whose mtime is when it was published. The worker's hook
(``copilot/hooks/chief_inbox.py``), this reaper and the reconciler
(``src/chief_reconcile.py``) all read and write the same names, so a session
whose own ``SessionEnd`` hook already fired gets no second event here.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src import runtime_files

logger = logging.getLogger(__name__)

EVENT_VERSION = 2
KIND_SESSION_END = "session-end"
KIND_TURN_END = "turn-end"
#: A turn end whose last model round wrote no reply (``copilot/hooks/chief_inbox.py``).
KIND_TURN_FAILED = "turn-failed"
SOURCE_REAPER = "reaper"
#: v1-compatible event name, so ``wait-event`` still names the event.
LEGACY_EVENT_NAME = "SessionEnd"
#: v1-compatible event names by kind, as the worker's hook writes them.
LEGACY_EVENT_NAMES = {
    KIND_SESSION_END: LEGACY_EVENT_NAME, KIND_TURN_END: "Stop", KIND_TURN_FAILED: "Stop",
}


#: ``src.webapp_config.WEBAPP_CONFIG_PATH_ENV`` and ``DEFAULT_CONFIG_PATH``. Not
#: imported: ``webapp_config`` pulls in ``model_catalog``, so every edit to
#: either would mark the long-lived session-host stale. A test pins them equal.
CONFIG_PATH_ENV = "LAUNCHER_WEBAPP_CONFIG"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "webapp_config.json"

#: Mirror of ``WebappConfig.chief_managed_file`` / ``chief_inbox_dir`` defaults.
DEFAULT_STATE_DIR = Path.home() / ".copilot" / "hooks" / "state"
REGISTRY_KEY = "chief_managed_file"
INBOX_KEY = "chief_inbox_dir"


def resolve_paths() -> Tuple[Path, Path]:
    """``(registry file, inbox dir)`` from ``webapp_config.json``, else the
    user-scope defaults. A missing, unreadable or non-object config file, or a
    key that is not a non-empty string, falls back to the default for that
    path: the defaults are the contract's own location."""
    config_path = Path(os.environ.get(CONFIG_PATH_ENV, "").strip() or DEFAULT_CONFIG_PATH)
    raw: Any = {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning(f"⚠️ chief session-end: {config_path} unreadable ({exc}); using default paths")
    if not isinstance(raw, dict):
        raw = {}

    def pick(key: str, default: Path) -> Path:
        value = raw.get(key)
        return Path(value) if isinstance(value, str) and value.strip() else default

    return (
        pick(REGISTRY_KEY, DEFAULT_STATE_DIR / "chief-managed.json"),
        pick(INBOX_KEY, DEFAULT_STATE_DIR / "chief-inbox"),
    )


def read_registry(registry: Path) -> Optional[Dict[str, Any]]:
    """The chief-managed rows keyed by launcher sid.

    ``{}`` when the file is absent (a determination: nothing dispatched).
    ``None`` when it cannot be established: unreadable, not JSON, or not an
    object.
    """
    try:
        raw = registry.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning(f"⚠️ chief session-end undetermined: cannot read {registry} ({exc})")
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning(f"⚠️ chief session-end undetermined: {registry} is not JSON ({exc})")
        return None
    if not isinstance(data, dict):
        logger.warning(f"⚠️ chief session-end undetermined: {registry} is not a JSON object")
        return None
    return data


def _number(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def stamp_path(inbox: Path, kind: str, sid: str) -> Path:
    """The marker whose mtime is when ``kind`` was last published for ``sid``."""
    return inbox / f".{kind}-{sid}.stamp"


def stamp_mtime(stamp: Path) -> Optional[float]:
    """The marker's mtime, ``None`` when there is none."""
    try:
        return stamp.stat().st_mtime
    except OSError:
        return None


def touch_stamp(stamp: Path) -> None:
    """Stamp a publish now. A failed write is logged: the event is out, only a repeat is possible."""
    try:
        runtime_files.save_text(stamp, "")
    except OSError as exc:
        logger.warning(f"⚠️ chief mailbox stamp {stamp.name} not written: {exc}")


def make_event(
    sid: str,
    row: Dict[str, Any],
    agent: str,
    now: datetime,
    *,
    kind: str = KIND_SESSION_END,
    excerpt: Optional[str] = None,
    excerpt_unavailable: str = "session died without running a hook",
    source: str = SOURCE_REAPER,
) -> Dict[str, Any]:
    """One v2 mailbox event for ``sid`` from its chief-managed registry ``row``."""
    dispatch_id = row.get("dispatch_id")
    repo = row.get("repo")
    event: Dict[str, Any] = {
        "v": EVENT_VERSION,
        "id": uuid.uuid4().hex,
        "kind": kind,
        "event": LEGACY_EVENT_NAMES[kind],
        "launcher_sid": sid,
        "repo": repo if isinstance(repo, str) else None,
        "number": _number(row.get("number")),
        "agent": agent,
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dispatch_id": dispatch_id if isinstance(dispatch_id, str) and dispatch_id else None,
        "excerpt": excerpt,
        "excerpt_source": "transcript" if excerpt else None,
        "source": source,
    }
    if excerpt is None:
        event["excerpt_unavailable"] = excerpt_unavailable
    return event


def publish(inbox: Path, event: Dict[str, Any], now_ms: Optional[int] = None) -> Path:
    """Write ``event`` into ``inbox`` atomically; returns the published path.

    The one atomic writer for the chief-mailbox wire format:
    ``src.chief_job_failed`` imports this instead of keeping its own copy.
    ``now_ms`` defaults to the current time; a caller that already pins a
    ``datetime`` (e.g. :func:`publish_session_ends`) passes it explicitly so
    every event from one reaper pass shares a timestamp.
    """
    runtime_files.make_dirs(inbox)
    published_ms = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    tmp = inbox / f".event.{secrets.token_hex(8)}.tmp"
    final = inbox / f"{published_ms}-{event['id']}.json"
    try:
        runtime_files.save_text(tmp, json.dumps(event, ensure_ascii=False))
        runtime_files.replace(tmp, final)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return final


def publish_session_ends(
    sessions: Iterable[Any],
    *,
    registry: Path,
    inbox: Path,
    now: Optional[datetime] = None,
) -> List[Path]:
    """Publish one ``session-end`` event per reaped session the registry names
    and no earlier publish (the worker's own hook, a reconcile) has stamped.

    Per-event write failures are logged and skipped; the reaper keeps running.
    Returns the published paths.
    """
    reaped = [s for s in sessions if getattr(s, "session_id", "")]
    if not reaped:
        return []
    rows = read_registry(registry)
    if rows is None:
        sids = ", ".join(s.session_id[:8] for s in reaped)
        logger.warning(
            f"⚠️ chief session-end undetermined for {sids}: "
            f"registry unreadable, nothing published"
        )
        return []
    moment = now or datetime.now(timezone.utc)
    now_ms = int(moment.timestamp() * 1000)
    published: List[Path] = []
    for session in reaped:
        sid = session.session_id
        row = rows.get(sid)
        if row is None:
            continue
        stamp = stamp_path(inbox, KIND_SESSION_END, sid)
        if stamp_mtime(stamp) is not None:
            logger.info(f"ℹ️ chief session-end for {sid[:8]} already published; reaper skips it")
            continue
        event = make_event(
            sid, row if isinstance(row, dict) else {}, getattr(session, "agent", ""), moment
        )
        try:
            published.append(publish(inbox, event, now_ms))
        except OSError as exc:
            logger.warning(f"❌ chief session-end for {sid[:8]} not published: {exc}")
            continue
        touch_stamp(stamp)
        logger.info(
            f"✅ chief session-end published for {sid[:8]} ({event['repo']}#{event['number']})"
        )
    return published
