"""Session-claim/merge logic for the Board tab (split out of ``board.py``).

Joins the **live session list** from the session-host
(``session_client.list_sessions``) with the hook-written state rows
(:mod:`src.board_state`) into board cards. The join is exact launcher id +
agent when a writer supplies those fields; agent-less rows fall back to an
agent-gated normalized-cwd claim. Two legacy live sessions in one directory
tie-break by most recent ``started_at``; the rest show ``unknown``.

Shared session title: the state row also carries ``name``/``name_source``
(the agent's live session title, where one exists). :func:`merge_sessions`
copies those onto every card as ``shared_name``/``shared_name_source``, and
:func:`attach_shared_names` runs the identical agent-aware claim walk for the
Coding tab's own ``/api/coding/sessions`` list — so a live session
resolves to the same state row, and therefore the same title, on both tabs.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AbstractSet, Any, Dict, List, Optional, Tuple

from src.board_state import STATE_STALE_AFTER, _age_seconds, _now, _parse_iso

logger = logging.getLogger(__name__)

# External-row liveness: a hook state is semantic evidence, not proof that its
# process still exists. Only a recently written transcript lets an unmatched
# row render as an external session; otherwise missing cloud/bridge rows and
# quiet waiting rows would linger for the whole retention window. Five minutes
# is an inherently imperfect fallback (a process that writes once and exits
# inside the window is indistinguishable from one still running); the two
# deterministic checks in :func:`_external_row_liveness` run first.
_EXTERNAL_ACTIVITY_AFTER = timedelta(minutes=5)

# Statuses the hook writer emits; anything else renders as "unknown". The
# writer's ``needs-you`` is surfaced on the card as ``awaiting-input`` (see
# :func:`_card_status`) — the raw string never reaches a card.
_KNOWN_STATUSES = frozenset({"working", "needs-you", "idle"})

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

# One breadcrumb per rejected state id per webapp process. GET /api/board polls
# every five seconds; logging every rejection on every poll would bury the
# useful diagnosis in noise. Bound the set so a very long-lived tray cannot
# accumulate ids without limit.
_LOGGED_SUPPRESSED_ROWS: set[str] = set()
_SUPPRESSED_LOG_CAP = 512


def _normalize_dir(raw: Any) -> str:
    """Forward slashes, lowercase, no trailing slash — the hook-side rule."""
    return str(raw or "").replace("\\", "/").rstrip("/").lower()


def _row_agent(row: Dict[str, Any]) -> str:
    """State rows with no ``agent`` field default to the launcher's agent."""
    return str(row.get("agent") or "copilot").strip().lower()


def _session_started(session: Dict[str, Any]) -> Optional[datetime]:
    """When a live session started, or ``None`` if unreadable.

    The session-host reports ``started_at`` as an epoch float
    (``PtySession.to_api``); ISO strings are accepted too, since state rows
    and older fixtures carry those.
    """
    raw = session.get("started_at")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return _parse_iso(raw)


def _session_agent(session: Dict[str, Any]) -> str:
    return str(session.get("agent") or "copilot").strip().lower()


def _cwd_agent_key(session: Dict[str, Any]) -> tuple:
    """(normalized project dir, agent) — the cwd fallback's matching grain."""
    return (_normalize_dir(session.get("project_dir")), _session_agent(session))


def _match_state_row(
    session: Dict[str, Any],
    unmatched: Dict[str, Dict[str, Any]],
    *,
    ambiguous_cwd: bool = False,
) -> Optional[str]:
    """Claim an agent-compatible state row for one live session.

    A writer-provided ``launcher_session_id`` is exact and wins first. Legacy
    agent-less rows have neither field and retain the normalized
    cwd fallback. Rows carrying a different launcher id are never allowed to
    fall back by cwd — that would reintroduce the same-session collision exact
    identity exists to prevent.

    The cwd fallback also requires the row's ``updated_at`` to be at-or-after
    the session's own ``started_at`` — a row genuinely written by this
    session can never predate the session's existence, so an older row can
    only be some other, unrelated conversation's leftover state in the same
    directory (e.g. a race where this session's own row hasn't landed yet).
    Without this guard the "most recently updated" tie-break could hand a
    brand-new session an hours-old sibling's title.

    ``ambiguous_cwd``: true when 2+ *live* sessions share this
    session's (cwd, agent) — set by :func:`_claim_walk` from the full live
    list, not just the rows still unclaimed. With only recency to go on, the
    cwd fallback has no way to tell which of several live siblings a given
    row actually belongs to; picking the highest-``updated_at`` one anyway
    cross-wires cards to the wrong session's transcript (reproduced live: 3
    concurrent sessions in one directory, all 3 assigned to each other's
    rows). Degrading to no match (``unknown`` card, no transcript) is safer
    than a confident wrong answer. Sessions that
    carry their own ``launcher_session_id`` (the exact-match path above)
    never reach here, so this only affects legacy/external sessions with no
    launcher id sharing a directory.
    """
    session_id = str(session.get("session_id") or "")
    agent = _session_agent(session)
    exact_sid: Optional[str] = None
    exact_stamp = _EPOCH
    for sid, row in unmatched.items():
        launcher_sid = str(row.get("launcher_session_id") or "")
        if launcher_sid and launcher_sid == session_id and _row_agent(row) == agent:
            stamp = _parse_iso(row.get("updated_at")) or _EPOCH
            if exact_sid is None or stamp > exact_stamp:
                exact_sid, exact_stamp = sid, stamp
    if exact_sid is not None:
        return exact_sid

    if ambiguous_cwd:
        return None

    project_dir_norm = _normalize_dir(session.get("project_dir"))
    if not project_dir_norm:
        return None
    session_started = _session_started(session) or _EPOCH
    best_sid: Optional[str] = None
    best_stamp = _EPOCH
    for sid, row in unmatched.items():
        if _row_agent(row) != agent or row.get("launcher_session_id"):
            continue
        row_cwd = _normalize_dir(row.get("cwd"))
        if row_cwd != project_dir_norm and not row_cwd.startswith(project_dir_norm + "/"):
            continue
        stamp = _parse_iso(row.get("updated_at")) or _EPOCH
        if stamp < session_started:
            continue
        if best_sid is None or stamp > best_stamp:
            best_sid, best_stamp = sid, stamp
    return best_sid


def _claim_walk(
    live: List[Dict[str, Any]], state_rows: Dict[str, Dict[str, Any]]
) -> tuple:
    """Assign state rows to live sessions, newest session first.

    The single source of the claim order — ``merge_sessions`` and
    ``attach_shared_names`` both resolve rows through it, so the Board and the
    Coding tab agree on which row belongs to which session. Returns ``(pairs, leftovers)`` where ``pairs``
    is ``[(session, row-or-None, sid-or-None), ...]`` (the
    ``state_sid`` — the claimed row's own key — rides alongside the row so
    ``merge_sessions`` can put it on the card) and ``leftovers`` the unclaimed
    rows.
    """
    unmatched = dict(state_rows)
    pairs: List[tuple] = []

    def started(sess: Dict[str, Any]) -> datetime:
        return _session_started(sess) or _EPOCH

    # Counted over the FULL live list (not the shrinking unmatched-rows
    # walk) — the ambiguity is about how many live sessions are competing
    # for this directory, independent of claim order.
    cwd_agent_counts = Counter(_cwd_agent_key(sess) for sess in live)

    for sess in sorted(live, key=started, reverse=True):
        ambiguous = cwd_agent_counts[_cwd_agent_key(sess)] > 1
        sid = _match_state_row(sess, unmatched, ambiguous_cwd=ambiguous)
        pairs.append((sess, unmatched.pop(sid) if sid else None, sid))
    return pairs, unmatched


def _claimed_transcript_paths(pairs: List[tuple]) -> "set[str]":
    """Transcript paths already backing a live, matched session's own row
    — an unmatched row pointing at the same file is a superseded
    leftover of that same session (re-keyed when a worker moved from one
    issue to the next), not independent evidence of a second live process."""
    claimed: "set[str]" = set()
    for sess, row, _sid in pairs:
        if not sess.get("alive") or not row:
            continue
        path = row.get("transcript_path")
        if path:
            claimed.add(path)
    return claimed


def _live_launcher_session_ids(live: List[Dict[str, Any]]) -> "set[str]":
    """Every currently-alive session-host id — the session-host is the
    authority on which PTYs it owns, so a hook row whose own
    ``launcher_session_id`` is absent from this set is provably dead, however
    recently its transcript happened to be touched."""
    return {
        str(sess.get("session_id"))
        for sess in live
        if sess.get("alive") and sess.get("session_id")
    }


def attach_shared_names(
    live: List[Dict[str, Any]], state_rows: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Join live session-host sessions with the state file's shared session
    title (``name``/``name_source``), for the Coding tab's
    Running-sessions list.

    Uses the exact same agent-aware :func:`_claim_walk` as :func:`merge_sessions`
    — both consumers must resolve a given live session to the same state row,
    or the Board tab and the Coding tab could show two different titles for
    the same session. Returns new dicts (each session's own fields plus
    ``shared_name``/``shared_name_source``, both ``None`` on no match) — the
    input dicts are never mutated.
    """
    pairs, _ = _claim_walk(live, state_rows)
    return [
        {
            **sess,
            "shared_name": (row or {}).get("name"),
            "shared_name_source": (row or {}).get("name_source"),
        }
        for sess, row, _sid in pairs
    ]


def _card_status(raw_status: Any) -> str:
    """The card status for a hook row's raw status.

    The writer's ``needs-you`` becomes ``awaiting-input`` (a prompt is
    expected); ``working`` / ``idle`` pass through; anything else is
    ``unknown``. No transcript is read: a status the hook did not write is
    never inferred.
    """
    if raw_status == "needs-you":
        return "awaiting-input"
    return raw_status if raw_status in _KNOWN_STATUSES else "unknown"


def _transcript_mtime(transcript_path: Any) -> Optional[datetime]:
    """The transcript file's mtime as a UTC datetime, or ``None`` on any
    OSError (missing file, permission, …)."""
    try:
        return datetime.fromtimestamp(
            Path(str(transcript_path)).stat().st_mtime, tz=timezone.utc
        )
    except OSError:
        return None


def _external_row_liveness(
    row: Optional[Dict[str, Any]],
    now: datetime,
    *,
    claimed_transcripts: AbstractSet[str] = frozenset(),
    live_launcher_session_ids: AbstractSet[str] = frozenset(),
) -> Tuple[bool, str]:
    """Whether an unmatched hook row has independent process-liveness proof.

    A hook row can survive a hard kill or a cloud/bridge lifecycle gap for the
    writer's whole 24-hour retention window. Its status says what the agent was
    doing at the last event; it does not say the process is still alive. Two
    deterministic checks run before the transcript-freshness fallback:

    * ``launcher_session_id`` present but absent from the *current* live
      session-host list is definitive: the session-host is the authority on
      which PTYs it owns, so a row whose own PTY has been reaped is proven
      dead regardless of how recently its transcript happened to be touched.
    * A transcript path already claimed by a live, *matched* card's own row
      (``claimed_transcripts``) means this unmatched row is a superseded
      leftover of that same session — re-keyed when a worker moved from one
      issue to the next, still pointing at the transcript the live session
      keeps writing — not independent evidence of a second process.

    Only once neither applies does a transcript written in the last 5
    minutes remain the (inherently imperfect) fallback evidence for a
    genuinely external row the launcher has no other way to verify.

    The reason string is deliberately condition-specific so the caller can
    leave one useful info-level breadcrumb for the next recurrence.
    """
    if row is None:
        return False, "missing state row"
    launcher_sid = row.get("launcher_session_id")
    if launcher_sid and str(launcher_sid) not in live_launcher_session_ids:
        return False, "launcher session no longer live"
    transcript = row.get("transcript_path")
    if transcript and transcript in claimed_transcripts:
        return False, "transcript claimed by a live matched session"
    if not transcript:
        return False, "missing transcript path"
    mtime = _transcript_mtime(transcript)
    if mtime is None:
        return False, "transcript file unavailable"
    if now - mtime > _EXTERNAL_ACTIVITY_AFTER:
        return False, "transcript quiet past 5 minutes"
    return True, "recent transcript activity"


def _live_card(
    sess: Dict[str, Any],
    row: Optional[Dict[str, Any]],
    sid: Optional[str],
    now: datetime,
) -> Dict[str, Any]:
    """The board card for one live session-host session and its claimed row."""
    project_dir = sess.get("project_dir")

    status = _card_status((row or {}).get("status"))
    anchor = (_parse_iso(row.get("updated_at")) if row else None) or (
        _session_started(sess)
    )
    project = (row or {}).get("project") or Path(str(project_dir or "")).name
    return {
        "session_id": sess.get("session_id"),
        "state_sid": sid,
        "kind": sess.get("kind"),
        "agent": sess.get("agent"),
        "label": sess.get("label") or "",
        "project_dir": project_dir,
        "name": sess.get("name"),
        "alive": bool(sess.get("alive", True)),
        "started_at": sess.get("started_at"),
        "live_title": sess.get("live_title") or "",
        "prompt_title": sess.get("prompt_title") or "",
        "manual_title": sess.get("manual_title") or "",
        "shared_name": (row or {}).get("name"),
        "shared_name_source": (row or {}).get("name_source"),
        "project": project,
        "status": status,
        "age_seconds": _age_seconds(anchor, now),
    }


def _external_card(
    sid: str,
    row: Dict[str, Any],
    now: datetime,
    *,
    claimed_transcripts: AbstractSet[str],
    live_launcher_session_ids: AbstractSet[str],
) -> Optional[Dict[str, Any]]:
    """The card for a state row with no live match, or None when not provably live."""
    stamp = _parse_iso(row.get("updated_at"))
    if stamp is None or now - stamp > STATE_STALE_AFTER:
        return None  # cold leftovers: not worth a card
    cwd = row.get("cwd")
    project = row.get("project") or Path(str(cwd or "")).name
    status = _card_status(row.get("status"))
    externally_live, reason = _external_row_liveness(
        row, now,
        claimed_transcripts=claimed_transcripts,
        live_launcher_session_ids=live_launcher_session_ids,
    )
    if not externally_live:
        if sid not in _LOGGED_SUPPRESSED_ROWS:
            if len(_LOGGED_SUPPRESSED_ROWS) >= _SUPPRESSED_LOG_CAP:
                _LOGGED_SUPPRESSED_ROWS.clear()
            _LOGGED_SUPPRESSED_ROWS.add(sid)
            logger.info(
                "ℹ️ Board suppressed unverifiable state row %s (%s, %s): %s",
                sid[:8], project, status, reason,
            )
        return None
    return {
        "session_id": None,
        "kind": "external",
        "agent": _row_agent(row),
        "project_dir": cwd,
        "name": str(project),
        "alive": False,
        "started_at": None,
        "live_title": "",
        "prompt_title": "",
        "manual_title": "",
        "shared_name": row.get("name"),
        "shared_name_source": row.get("name_source"),
        "project": str(project),
        "status": status,
        "age_seconds": _age_seconds(stamp, now),
    }


def merge_sessions(
    live: List[Dict[str, Any]],
    state_rows: Dict[str, Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Join live session-host sessions with hook state rows into board cards.

    Live sessions are walked newest-first. Exact launcher-session id + agent
    claims win; agent-less rows fall back to cwd recency. Later sessions in
    the same directory render ``unknown``. A state row with no live match only
    becomes an external card when a recently written transcript independently
    proves the process still exists — hook state alone is not liveness.

    A card's status is the hook row's own status (:func:`_card_status`); the
    transcript content is never read, so a status the hook did not write is
    never inferred.

    Live-session cards also carry ``state_sid`` — the claimed state row's own
    key (``None`` when unmatched) — so a Slack ping's deep link, which only
    knows the hook's transcript UUID, can still resolve to the right card
    State-only cards don't get one: they have no
    session-host id and no drawer target, so a deep link to one is out of scope.
    """
    now = now or _now()
    cards: List[Dict[str, Any]] = []
    pairs, unmatched = _claim_walk(live, state_rows)

    for sess, row, sid in pairs:
        cards.append(_live_card(sess, row, sid, now))

    claimed_transcripts = _claimed_transcript_paths(pairs)
    live_launcher_session_ids = _live_launcher_session_ids(live)

    for sid, row in unmatched.items():
        card = _external_card(
            sid, row, now,
            claimed_transcripts=claimed_transcripts,
            live_launcher_session_ids=live_launcher_session_ids,
        )
        if card is not None:
            cards.append(card)

    return cards
