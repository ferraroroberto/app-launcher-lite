"""Persist per-session state for the app-launcher-lite Board.

Maintains ``sessions-state.json`` — one row per recent Copilot CLI session
(``project``, ``status``, ``transcript_path``, ``cwd``, ``updated_at``,
``agent``, optional ``launcher_session_id``) keyed by the payload's
``session_id`` — so the Board tab can render "Bot's turn" / "Your turn"
columns without owning any hook plumbing. The Board only *reads* the file;
this module is the only writer.

Copilot CLI payloads carry no event-name field, so the hook config
(``hooks/session-state.template.json``) passes the event name as
``argv[1]``. What each event does to a row is declared once, in
:func:`next_transition`; ``docs/session-state.md`` holds the state table.

The payload's ``session_id`` is Copilot's session UUID (the same id
``copilot --resume=<id>`` accepts). App Launcher Lite injects its exact
identity as inherited ``APP_LAUNCHER_SESSION_ID`` / ``APP_LAUNCHER_AGENT``
env values; when present this writer persists them for an exact agent-aware
consumer join. External sessions keep the normalized-cwd fallback join.

Advisory-only: any failure is swallowed and the hook exits 0. The state file
lives under ``~/.copilot/hooks/state/``; ``COPILOT_HOOKS_STATE_DIR``
overrides the directory so tests stay hermetic. Rows untouched for 24h are
pruned on each write.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402
import chief_inbox  # noqa: E402

# Appended, not inserted: skills/_lib must never shadow a hook module of the same name.
sys.path.append(str(Path(__file__).resolve().parents[1] / "skills" / "_lib"))
import lite_ports  # noqa: E402

STATE_FILENAME = "sessions-state.json"

_PRUNE_AFTER = timedelta(hours=24)
_REPLACE_ATTEMPTS = 3  # os.replace can hit a transient PermissionError under a concurrent Windows reader


class SessionStatus(str, Enum):
    """What the Board shows for one session row. The set is closed: a new status is a new member."""

    IDLE = "idle"  # started, no turn handed over yet
    WORKING = "working"  # the user handed over a turn and the agent has not stopped
    NEEDS_YOU = "needs-you"  # the agent finished a turn and waits for the user


class Effect(Enum):
    """What an event does to the session's row."""

    WRITE = "write"  # (re)write the whole row with the transition's status
    REFRESH = "refresh"  # keep the row as it is and only bump ``updated_at``
    REMOVE = "remove"  # delete the row


@dataclass(frozen=True)
class Transition:
    effect: Effect
    status: Optional[SessionStatus] = None  # set exactly when ``effect`` is WRITE


def next_transition(existing: Optional[SessionStatus], event: str) -> Optional[Transition]:
    """The only place an event is mapped to a row change; ``None`` ignores the event.

    ``existing`` is the row's current status, or ``None`` when there is no row or
    its stored status is not a ``SessionStatus``. ``sessionStart`` may arrive
    after ``userPromptSubmitted`` (non-interactive runs, verified live on CLI
    1.0.70), so it only fills a missing row and never downgrades a real status.
    """
    if event == "userPromptSubmitted":
        return Transition(Effect.WRITE, SessionStatus.WORKING)
    if event == "agentStop":
        return Transition(Effect.WRITE, SessionStatus.NEEDS_YOU)
    if event == "sessionStart":
        if existing is None:
            return Transition(Effect.WRITE, SessionStatus.IDLE)
        return Transition(Effect.REFRESH)
    if event == "sessionEnd":
        return Transition(Effect.REMOVE)
    return None


def _stored_status(row: Any) -> Optional[SessionStatus]:
    """The row's status, or ``None`` for a missing row or a status no writer produces."""
    if not isinstance(row, dict):
        return None
    try:
        return SessionStatus(row.get("status"))
    except ValueError:
        return None


def state_file() -> Path:
    """Resolve the state-file path at call time so the env override always wins."""
    root = os.environ.get("COPILOT_HOOKS_STATE_DIR")
    base = Path(root) if root else Path.home() / ".copilot" / "hooks" / "state"
    return base / STATE_FILENAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_updated_at(row: Any) -> Optional[datetime]:
    if not isinstance(row, dict):
        return None
    raw = row.get("updated_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_rows(path: Path) -> Dict[str, Any]:
    """Current rows, or {} on a missing/corrupt file — the writer self-heals."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_rows(path: Path, rows: Dict[str, Any]) -> None:
    """Atomic tmp+replace write, retried because a concurrent reader on Windows
    can hold the target and fail ``os.replace`` with a transient PermissionError."""
    payload = json.dumps(rows, indent=2, sort_keys=True)
    for attempt in range(_REPLACE_ATTEMPTS):
        tmp_name: Optional[str] = None
        try:
            fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, path)
            return
        except OSError:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            time.sleep(0.05 * (attempt + 1))


def _new_row(payload: Dict[str, Any], status: SessionStatus, existing: Any) -> Dict[str, Any]:
    cwd_path = _lib.cwd(payload)
    transcript = payload.get("transcript_path")
    # Keep a transcript_path learned on a previous event (only agentStop
    # carries one) instead of erasing it on the next prompt.
    if not (isinstance(transcript, str) and transcript) and isinstance(existing, dict):
        transcript = existing.get("transcript_path")
    return {
        "project": cwd_path.name,
        "status": status.value,
        "transcript_path": transcript if isinstance(transcript, str) and transcript else None,
        "cwd": str(cwd_path),
        "name": None,
        "name_source": None,
        "agent": (
            os.environ.get("APP_LAUNCHER_AGENT", "").strip().lower() or "copilot"
        ),
        "launcher_session_id": (
            os.environ.get("APP_LAUNCHER_SESSION_ID", "").strip() or None
        ),
        "updated_at": _isoformat(_now()),
    }


def apply_event(payload: Dict[str, Any], event: str) -> None:
    """Apply one hook event to the payload's session row.

    Silent no-op without a ``session_id``, for an unmapped event, or for a
    ``sessionEnd`` whose row is already gone.
    """
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return

    path = state_file()
    rows = _read_rows(path)
    existing = rows.get(session_id)
    transition = next_transition(_stored_status(existing), event)
    if transition is None:
        return

    if transition.effect is Effect.REMOVE:
        if session_id in rows:
            del rows[session_id]
            _write_rows(path, rows)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    if transition.effect is Effect.REFRESH:
        # Refresh the heartbeat only; keep the stronger status.
        row = dict(existing)
        row["updated_at"] = _isoformat(_now())
        rows[session_id] = row
    elif transition.status is not None:
        rows[session_id] = _new_row(payload, transition.status, existing)

    cutoff = _now() - _PRUNE_AFTER
    kept: Dict[str, Any] = {}
    for sid, row in rows.items():
        stamp = _parse_updated_at(row)
        if stamp is not None and stamp >= cutoff:
            kept[sid] = row

    _write_rows(path, kept)


# On a machine that also runs the full fleet the installer passes this flag, so the
# hook records only sessions app-launcher-lite itself launched (see lite_session).
LITE_SESSIONS_ONLY_FLAG = "--lite-sessions-only"
# app-launcher-lite's session-host list, on the port its config.json sets
# (`lite_ports`); this env var names the whole list URL instead.
LITE_SESSION_HOST_ENV = "FLEET_LITE_SESSION_HOST"
# A loopback list read; a host slower than this is treated as unreachable.
LITE_HOST_TIMEOUT_S = 1.5


def lite_session(launcher_sid: str) -> bool:
    """True only when app-launcher-lite's session-host lists `launcher_sid`.

    Any failure (no id, host down, bad reply) is False: in shared mode an
    unestablished session is not recorded, so nothing is written for it.
    """
    if not launcher_sid:
        return False
    url = os.environ.get(LITE_SESSION_HOST_ENV, "").strip() or lite_ports.session_host_url()
    try:
        with urllib.request.urlopen(url, timeout=LITE_HOST_TIMEOUT_S) as resp:  # noqa: S310 -- loopback list
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    rows = data.get("sessions") if isinstance(data, dict) else data
    return any(isinstance(r, dict) and r.get("session_id") == launcher_sid for r in rows or [])


def main() -> None:
    try:
        event = sys.argv[1] if len(sys.argv) > 1 else ""
        if LITE_SESSIONS_ONLY_FLAG in sys.argv[2:] and not lite_session(
                os.environ.get("APP_LAUNCHER_SESSION_ID", "").strip()):
            _lib.allow()
        payload = _lib.read_stdin_json()
        apply_event(payload, event)
        # The chief's mailbox rides the same invocation: a chief-managed worker's
        # turn end or exit becomes one event file (no-op for every other session).
        chief_inbox.record_safely(payload, event)
    except Exception:  # noqa: BLE001 — state is advisory; never disturb the session
        pass
    _lib.allow()


if __name__ == "__main__":
    main()
