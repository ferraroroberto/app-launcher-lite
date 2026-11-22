"""Publish the turn end or exit of a chief-managed lane whose own hook never did.

A chief-managed worker reports through its ``agentStop`` / ``sessionEnd``
hooks (``copilot/hooks/chief_inbox.py``), and the session-host's reaper covers
a worker killed too hard to run one (:mod:`src.chief_session_end`). Neither
helps when a hook was missed on a session that is still open, or when the
hook's 45-second turn-end debounce swallowed a real later turn end: the lane
then sits at its prompt and nothing ever wakes the chief.

Every :data:`TICK_SECONDS` the canonical webapp reads each managed lane's
Copilot transcript (``events.jsonl``, found through the hook state row that
carries its launcher session id) for the terminal evidence Copilot writes
itself: ``session.task_complete`` closes a turn, and ``session.shutdown``
closes the session. A turn end or exit older than :data:`GRACE_SECONDS` that
the lane's mailbox stamp does not already cover gets one event, published and
stamped exactly as the hook would, so the doorbell rings once for it.

An active lane never gets an event: a ``user.message`` or
``assistant.turn_start`` after the last ``session.task_complete`` means a turn
is running. A plain session is never in the registry and is never read.
"Could not tell" is not "nothing to do": an unreadable registry or transcript
is logged and publishes nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.board.state import read_sessions_state
from src.chief_session_end import (
    KIND_SESSION_END,
    KIND_TURN_END,
    make_event,
    publish,
    read_registry,
    stamp_mtime,
    stamp_path,
    touch_stamp,
)

logger = logging.getLogger(__name__)

SOURCE_RECONCILE = "reconcile"
TICK_SECONDS = 60.0
#: A hook lands within a couple of seconds of the transcript line it reports;
#: two minutes leaves it every chance to win before a reconcile speaks for it.
GRACE_SECONDS = 120.0
#: Enough of ``events.jsonl`` for the last turn without reading a long session whole.
TRANSCRIPT_TAIL_BYTES = 512 * 1024
#: The hook's excerpt length (``chief_inbox.EXCERPT_CHARS``).
EXCERPT_CHARS = 1500

_TURN_STARTS = frozenset({"user.message", "assistant.turn_start"})


@dataclass(frozen=True)
class Evidence:
    """What a transcript tail proves; ``None`` fields are not proven."""

    turn_ended_at: Optional[float] = None
    shutdown_at: Optional[float] = None
    excerpt: Optional[str] = None


def _epoch(raw: Any) -> Optional[float]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def transcript_evidence(path: Path) -> Optional[Evidence]:
    """The terminal evidence in the tail of a Copilot ``events.jsonl``; ``None`` when unreadable."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    turn_ended_at: Optional[float] = None
    shutdown_at: Optional[float] = None
    excerpt: Optional[str] = None
    for line in tail.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # the first line of a mid-file tail, or a torn last write
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind == "session.task_complete":
            turn_ended_at = _epoch(entry.get("timestamp"))
        elif kind in _TURN_STARTS:
            turn_ended_at = None
        elif kind == "session.shutdown":
            shutdown_at = _epoch(entry.get("timestamp"))
        elif kind == "assistant.message":
            content = (entry.get("data") or {}).get("content")
            if isinstance(content, str) and content.strip():
                excerpt = content.strip()[-EXCERPT_CHARS:]
    return Evidence(turn_ended_at=turn_ended_at, shutdown_at=shutdown_at, excerpt=excerpt)


def _lane_transcripts(state_rows: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The newest hook state row per launcher session id that names a transcript."""
    out: Dict[str, Dict[str, Any]] = {}
    for row in state_rows.values():
        sid = row.get("launcher_session_id")
        transcript = row.get("transcript_path")
        if not (isinstance(sid, str) and sid and isinstance(transcript, str) and transcript):
            continue
        held = out.get(sid)
        if held is None or str(row.get("updated_at") or "") > str(held.get("updated_at") or ""):
            out[sid] = row
    return out


def _publish_once(
    inbox: Path, sid: str, reg_row: Dict[str, Any], agent: str, kind: str, evidence: Evidence,
) -> Optional[Path]:
    event = make_event(
        sid, reg_row, agent, datetime.now(timezone.utc), kind=kind,
        excerpt=evidence.excerpt if kind == KIND_TURN_END else None,
        excerpt_unavailable=(
            "no assistant reply in the transcript tail" if kind == KIND_TURN_END
            else "session ended; no reply on this event"
        ),
        source=SOURCE_RECONCILE,
    )
    try:
        path = publish(inbox, event)
    except OSError as exc:
        logger.warning(f"❌ chief reconcile: {kind} for {sid[:8]} not published: {exc}")
        return None
    touch_stamp(stamp_path(inbox, kind, sid))
    logger.info(
        f"✅ chief reconcile: {kind} published for {sid[:8]} "
        f"({event['repo']}#{event['number']}); its hook never reported it"
    )
    return path


def reconcile(
    *,
    registry: Path,
    inbox: Path,
    state_rows: Dict[str, Dict[str, Any]],
    now: Optional[float] = None,
    grace: float = GRACE_SECONDS,
) -> List[Path]:
    """Publish each managed lane's unreported turn end or exit; returns the published paths."""
    rows = read_registry(registry)
    if rows is None:
        logger.warning("⚠️ chief reconcile undetermined: registry unreadable, nothing published")
        return []
    if not rows:
        return []
    moment = time.time() if now is None else now
    lanes = _lane_transcripts(state_rows)
    published: List[Path] = []
    for sid, reg_row in rows.items():
        state_row = lanes.get(sid)
        if state_row is None:
            continue  # no hook has named its transcript yet: nothing to read
        evidence = transcript_evidence(Path(state_row["transcript_path"]))
        if evidence is None:
            logger.info(f"ℹ️ chief reconcile undetermined for {sid[:8]}: transcript unreadable")
            continue
        reg = reg_row if isinstance(reg_row, dict) else {}
        agent = str(state_row.get("agent") or "copilot")
        ended = evidence.turn_ended_at
        if ended is not None and ended <= moment - grace:
            stamped = stamp_mtime(stamp_path(inbox, KIND_TURN_END, sid))
            if stamped is None or stamped < ended:
                path = _publish_once(inbox, sid, reg, agent, KIND_TURN_END, evidence)
                if path is not None:
                    published.append(path)
        shut = evidence.shutdown_at
        if shut is not None and shut <= moment - grace:
            if stamp_mtime(stamp_path(inbox, KIND_SESSION_END, sid)) is None:
                path = _publish_once(inbox, sid, reg, agent, KIND_SESSION_END, evidence)
                if path is not None:
                    published.append(path)
    return published


def reconcile_from_config(cfg: Any) -> List[Path]:
    """One pass over the paths ``cfg`` names (``WebappConfig``)."""
    state = read_sessions_state(Path(cfg.sessions_state_file))
    return reconcile(
        registry=Path(cfg.chief_managed_file),
        inbox=Path(cfg.chief_inbox_dir),
        state_rows=state["rows"],
    )


async def tick_forever(get_cfg: Callable[[], Any]) -> None:
    """The lifespan loop, canonical instance only: a gate webapp must never write the real inbox.
    ``get_cfg`` is re-read every tick so a Settings save moves the paths without a restart."""
    while True:
        await asyncio.sleep(TICK_SECONDS)
        cfg = get_cfg()
        if cfg is None:
            continue
        try:
            await asyncio.to_thread(reconcile_from_config, cfg)
        except Exception:  # noqa: BLE001 -- never take the lifespan down
            logger.exception("❌ chief reconcile tick failed")
