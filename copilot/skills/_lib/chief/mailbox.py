"""The chief mailbox readers (contract: docs/chief-mailbox.md): the `inbox`/`ack`
pair and the doorbell-or-waiter decision that says what wakes the chief.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fleet_core import hooks_state, runtime_files

from . import digest

LOGGER = logging.getLogger(__name__)


# ---- chief inbox: worker turn-end wake events -------------------------------

# Written by `hooks/chief_inbox.py` (one JSON file per event); read here by
# path, not import -- `hooks/` and `skills/_lib/` stay independent trees.
CHIEF_INBOX_DIRNAME = "chief-inbox"
# Same horizon as the chief-managed registry's TTL: an event older than the
# dispatch it belongs to describes nothing the chief can still act on.
INBOX_EVENT_TTL_SECONDS = 24 * 3600


# Mailbox claims (contract: docs/chief-mailbox.md) live under `.claimed/`
# until `ack` retires them.
CLAIMED_DIRNAME = ".claimed"
# A claim unacked this long is re-offered: the reader that took it is assumed
# dead. Longer than any chief turn that handles a batch, so a live chief is
# not handed its own batch twice.
CLAIM_STALE_SECONDS = 900.0
EVENT_KIND_TEXT = {"turn-end": "turn ended", "session-end": "session ended", "needs-input": "needs input",
                   "job-failed": "job failed", "ci-failed": "ci failed",
                   "machine-critical": "machine critical"}
# Offender rows past this many are left out of the rendered batch: the hub verdict
# the event links to carries the full list, and the chief reads it there.
MACHINE_OFFENDERS_SHOWN = 5
# With the doorbell on, the chief keeps one long waiter that wakes it only for
# an event that has waited this long: well past a ring's few seconds, so it
# never races the doorbell, short enough to bound a silently dead one.
FALLBACK_WAITER_SECONDS = 1800
FALLBACK_OLDER_THAN_SECONDS = 600
# Reading a just-claimed file retries a sharing violation this many times,
# backing off from this delay and doubling (10+20+40+80 ms): the concurrent
# claimer holding it finishes its rename in well under that.
CLAIM_READ_ATTEMPTS = 5
CLAIM_READ_BACKOFF_SECONDS = 0.01


def chief_inbox_dir() -> Path:
    return hooks_state.state_dir() / CHIEF_INBOX_DIRNAME


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _split_event_name(name: str) -> Tuple[Optional[int], str]:
    """`<ms>-<id>.json` -> `(ms, id)`; a name without that shape -> `(None, stem)`."""
    stem = name[:-len(".json")] if name.endswith(".json") else name
    prefix, dash, rest = stem.partition("-")
    if dash and prefix.isdigit() and rest:
        return int(prefix), rest
    return None, stem


def _event_files(directory: Path) -> List[Path]:
    """Published event files directly in `directory`: non-dot `*.json` regular files."""
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return []
    return sorted(
        (Path(entry.path) for entry in entries
         if entry.name.endswith(".json") and not entry.name.startswith(".") and entry.is_file()),
        key=lambda path: path.name,
    )


def count_pending(inbox: Path, now_ms: Optional[int] = None,
                  stale_after: float = CLAIM_STALE_SECONDS, older_than: float = 0.0) -> int:
    """Pending events by file name alone -- the reference for docs/chief-mailbox.md.

    Every root event file, plus every `.claimed/` file whose claim is at
    least `stale_after` old (or whose name carries no readable claim time).
    No file is opened or parsed. Raises `OSError` when a directory exists but
    cannot be listed: that is "unknown", never zero.

    `older_than` (seconds, 0 = off) counts only events that have waited at least
    that long: the doorbell's fallback waiter must not race a ring that is a few
    seconds old. A file name without a readable time still counts -- an event
    whose age cannot be established is never hidden.
    """
    moment = _now_ms() if now_ms is None else now_ms
    waited_ms = older_than * 1000
    pending = 0
    for path in _event_files(inbox):
        published_at, _ = _split_event_name(path.name)
        if published_at is None or moment - published_at >= waited_ms:
            pending += 1
    claim_ms = max(stale_after, older_than) * 1000
    for path in _event_files(inbox / CLAIMED_DIRNAME):
        claimed_at, _ = _split_event_name(path.name)
        if claimed_at is None or moment - claimed_at >= claim_ms:
            pending += 1
    return pending


def reoffer_stale_claims(inbox: Path, now_ms: int, stale_after: float = CLAIM_STALE_SECONDS) -> List[str]:
    """Move each stale `.claimed/` file back to the inbox root; the re-offered ids.

    The basename is kept: `<claimed_at_ms>-<id>.json` is already a valid root
    event name. A rename lost to a concurrent reader is skipped.
    """
    reoffered = []
    for path in _event_files(inbox / CLAIMED_DIRNAME):
        claimed_at, event_id = _split_event_name(path.name)
        if claimed_at is not None and now_ms - claimed_at < stale_after * 1000:
            continue
        try:
            # must-use-exempt: re-offers a claimed event by renaming it; nothing is written
            os.replace(path, inbox / path.name)
        except OSError:
            continue
        reoffered.append(event_id)
    return reoffered


def _read_claimed(held: Path) -> Tuple[str, Optional[Any]]:
    """Read a file this reader just renamed into a claim: `(outcome, event)`.

    Outcomes: `"read"` (event parsed), `"junk"` (read but not JSON), `"gone"`
    (another claimer holds it now) and `"locked"` (still unreadable after the
    retries). On Windows both of two racing renames can succeed: the rename
    opens the source by name and then moves it through that handle, so a
    claimer that opened it before our rename landed moves it again, out of our
    claim. Until it does, our read is a sharing violation; after, the file is
    gone from our path and that claimer reads it. So a
    failed read is never junk, and the caller must never delete the file.
    """
    delay = CLAIM_READ_BACKOFF_SECONDS
    for attempt in range(1, CLAIM_READ_ATTEMPTS + 1):
        try:
            text = held.read_text(encoding="utf-8")
        except FileNotFoundError:
            LOGGER.info("ℹ️ claim: %s moved to a concurrent claimer (attempt %d); it delivers it",
                        held.name, attempt)
            return "gone", None
        except OSError as exc:
            if attempt == CLAIM_READ_ATTEMPTS:
                LOGGER.warning("⚠️ claim: %s still unreadable after %d attempts (%s: %s)",
                               held.name, attempt, type(exc).__name__, exc)
                return "locked", None
            LOGGER.info("ℹ️ claim: %s not readable yet (attempt %d, %s); retrying in %.0f ms",
                        held.name, attempt, type(exc).__name__, delay * 1000)
            time.sleep(delay)
            delay *= 2
            continue
        try:
            return "read", json.loads(text)
        except ValueError:
            return "junk", None
    return "locked", None  # unreachable: the last attempt always returns


def claim_mailbox(inbox: Path, now_ms: Optional[int] = None,
                  stale_after: float = CLAIM_STALE_SECONDS) -> Tuple[List[Dict[str, Any]], List[str], int]:
    """`(events, reoffered_ids, dropped)`: claim every pending event, oldest first.

    Stale claims are re-offered first, then each root event file is renamed to
    `.claimed/<now_ms>-<id>.json` before it is read -- the rename is the
    claim, so a concurrent reader that loses the race skips the file. Nothing is
    deleted here except junk and events past the TTL; a handled event is retired by `ack_events`. Each returned event
    carries `id` from its file name, the handle `ack` takes.
    """
    moment = _now_ms() if now_ms is None else now_ms
    reoffered = reoffer_stale_claims(inbox, moment, stale_after)
    claimed = inbox / CLAIMED_DIRNAME
    now_dt = datetime.fromtimestamp(moment / 1000, tz=timezone.utc)
    events: List[Dict[str, Any]] = []
    dropped = 0
    for path in _event_files(inbox):
        _, event_id = _split_event_name(path.name)
        runtime_files.make_dirs(claimed, exist_ok=True)
        held = claimed / f"{moment}-{event_id}.json"
        try:
            # must-use-exempt: claims an event by renaming it; nothing is written
            os.replace(path, held)
        except OSError:
            continue  # another reader took it first
        outcome, event = _read_claimed(held)
        if outcome in ("gone", "locked"):
            # Never delete an unread claim: a "locked" one stays in `.claimed/`
            # and is re-offered once stale; a "gone" one is another reader's.
            continue
        age = _event_age_seconds(event, now_dt) if isinstance(event, dict) else None
        if not isinstance(event, dict) or (age is not None and age > INBOX_EVENT_TTL_SECONDS):
            try:
                held.unlink()
            except OSError:
                pass
            dropped += 1
            continue
        events.append({**event, "id": event_id})
    return collapse_incidents(events), reoffered, dropped


def ack_events(inbox: Path, ids: List[str]) -> Tuple[List[str], List[str]]:
    """`(acked, missing)`: delete each id's file, claimed or already re-offered."""
    acked, missing = [], []
    for event_id in ids:
        found = False
        for path in _event_files(inbox / CLAIMED_DIRNAME) + _event_files(inbox):
            stamp, name_id = _split_event_name(path.name)
            if stamp is None or name_id != event_id:
                continue
            try:
                path.unlink()
                found = True
            except FileNotFoundError:
                pass  # a concurrent ack or reader got there first
        (acked if found else missing).append(event_id)
    return acked, missing


def _event_age_seconds(event: Dict[str, Any], now: datetime) -> Optional[float]:
    try:
        stamp = datetime.strptime(str(event.get("ts")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - stamp).total_seconds()


def _job_failed_lines(event: Dict[str, Any]) -> List[str]:
    """A `job-failed` event's head line and one detail line, ahead of its excerpt.

    A Job has no worker session, so the worker form (`repo#n`, sid) does not
    apply. A reaped run has no exit code and an unowned script no repo: both
    read `-`. `outcome` is shown only when it is not a plain `failed`, so a
    run whose result could not be confirmed never reads as a confirmed failure.
    """
    def field(name: str) -> str:
        value = event.get(name)
        return "-" if value in (None, "") else str(value)

    outcome = event.get("outcome")
    head = (f"- job {field('job_name')} failed exit={field('exit_code')} "
            f"run={field('run_id')} repo={field('repo')}")
    if outcome and outcome != "failed":
        head += f" outcome={outcome}"
    return [head, f"  job_id={field('job_id')} trigger={field('trigger')} script={field('script_path')}"]


def _ci_failed_lines(event: Dict[str, Any]) -> List[str]:
    """A `ci-failed` event's head line and one detail line, ahead of its excerpt.

    A CI run has no worker session, so the worker form (`repo#n`, sid) does not
    apply. A field the producer left out reads `-` rather than rejecting the
    event: it still wakes the chief, which can read the run itself. The head
    shortens the sha to 7 characters; the detail keeps the run's URL and its
    failed job names.
    """
    def field(name: str) -> str:
        value = event.get(name)
        return "-" if value in (None, "") else str(value)

    sha = event.get("head_sha")
    jobs = event.get("failed_jobs")
    names = ", ".join(str(name) for name in jobs) if isinstance(jobs, list) and jobs else "-"
    head = (f"- ci {field('workflow')} failed repo={field('repo')} "
            f"sha={sha[:7] if isinstance(sha, str) and sha else '-'} run={field('run_id')}")
    return [head, f"  conclusion={field('conclusion')} event={field('event')} at={field('created_at')} "
                  f"jobs={names} url={field('run_url')}"]


def _machine_critical_lines(event: Dict[str, Any]) -> List[str]:
    """A `machine-critical` event's head line, a detail line and one line per top offender.

    The launcher's health publisher writes it, so there is no repo, issue or sid.
    A field the producer left out reads `-` rather than rejecting the event: it
    still wakes the chief, which reads the hub verdict itself. `repeats=<n>` is
    set by `collapse_incidents` when later events of the same incident were folded in.
    """
    def show(row: Dict[str, Any], name: str) -> str:
        value = row.get(name)
        return "-" if value in (None, "") else str(value)

    head = (f"- machine {show(event, 'host')} critical metric={show(event, 'metric')} "
            f"value={show(event, 'value')} threshold={show(event, 'threshold')}")
    repeats = len(event.get("duplicate_ids") or [])
    if repeats:
        head += f" repeats={repeats}"
    lines = [head, f"  first_seen={show(event, 'first_seen')} verdict={show(event, 'verdict_url')}"]
    offenders = event.get("offenders")
    rows = offenders if isinstance(offenders, list) else []
    for row in rows[:MACHINE_OFFENDERS_SHOWN]:
        row = row if isinstance(row, dict) else {}
        lines.append(f"  offender {show(row, 'name')} pid={show(row, 'pid')} owner={show(row, 'owner')} "
                     f"repo={show(row, 'repo')} value={show(row, 'value')}")
    if len(rows) > MACHINE_OFFENDERS_SHOWN:
        lines.append(f"  offenders: {len(rows) - MACHINE_OFFENDERS_SHOWN} more at the verdict")
    elif not rows:
        lines.append("  offenders: none listed")
    return lines


def collapse_incidents(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold `machine-critical` events of one incident (host + metric + first_seen) into the first.

    The kept event carries `duplicate_ids`, which the batch's `ACK=` line also
    names, so one ack retires them all and a chief that dies mid-batch is
    re-offered every file. Nothing is deleted here. An event missing any of the
    three key fields is never folded: an incident that cannot be identified
    must not hide another.
    """
    kept: List[Dict[str, Any]] = []
    first_of: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for event in events:
        host, metric, first_seen = (event.get(name) for name in ("host", "metric", "first_seen"))
        if (event.get("kind") != "machine-critical"
                or not all(isinstance(part, str) and part for part in (host, metric, first_seen))):
            kept.append(event)
            continue
        owner = first_of.setdefault((host, metric, first_seen), event)
        if owner is event:
            kept.append(event)
        else:
            owner.setdefault("duplicate_ids", []).append(event["id"])
    return kept


def _cap_raised_lines(event: Dict[str, Any]) -> List[str]:
    """A `cap-raised` event's one line: the Settings worker cap went up, so the chief fills the new slots.

    The launcher writes it, not a worker, so there is no repo, issue or sid to
    show. A bound that is not an integer reads `-` rather than rejecting the
    event: it still rings, and the chief re-reads the real cap at `dispatch`.
    """
    def bound(name: str) -> str:
        value = event.get(name)
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else "-"

    return [f"- worker cap raised old={bound('old')} new={bound('new')}"]


def _proposals_lines(event: Dict[str, Any]) -> List[str]:
    """A `proposals` event's head line and one line per candidate (`[pid8] repo: title (type) -- reason`).

    The weekly learning-log run writes it, so there is no repo, issue or sid. A `candidates`
    that is not a list reads `n=-` and still delivers; an unreadable candidate reads `?` for
    its fields rather than hiding the rest.
    """
    cands = event.get("candidates")
    count = str(len(cands)) if isinstance(cands, list) else "-"
    lines = [f"- weekly proposals n={count} week={event.get('week') or '-'}"]
    for cand in cands if isinstance(cands, list) else []:
        cand = cand if isinstance(cand, dict) else {}
        lines.append(f"  [{str(cand.get('pid') or '?')[:8]}] {cand.get('repo') or '?'}: "
                     f"{cand.get('title') or '?'} ({cand.get('type') or '?'}) -- {cand.get('reason') or '?'}")
    return lines


def _kind_lines(event: Dict[str, Any]) -> Optional[List[str]]:
    """The head (and detail) lines of an event kind that is not a worker's, or None for a worker's."""
    kind = event.get("kind")
    if kind == "job-failed":
        return _job_failed_lines(event)
    if kind == "ci-failed":
        return _ci_failed_lines(event)
    if kind == "machine-critical":
        return _machine_critical_lines(event)
    if kind == "cap-raised":
        return _cap_raised_lines(event)
    if kind == "proposals":
        return _proposals_lines(event)
    return None


def _event_line(event: Dict[str, Any], moment: datetime) -> str:
    """`- <repo>#<n>: <what> (<agent>) sid=<8> age=<age>` -- the v1 line, unchanged."""
    kind = event.get("kind")
    what = EVENT_KIND_TEXT.get(kind) if kind else None
    if what is None:
        what = "turn ended" if event.get("event") == "Stop" else "session ended" \
            if event.get("event") == "SessionEnd" else str(kind or event.get("event"))
    age = _event_age_seconds(event, moment)
    issue = f"{event.get('repo')}#{event.get('number')}"
    busy = " (busy)" if event.get("busy") else ""
    return (
        f"- {issue}: {what}{busy} ({event.get('agent') or '?'}) "
        f"sid={str(event.get('launcher_sid') or '')[:8]} "
        f"{'age=' + digest._fmt_age(age) if age is not None else 'age=?'}"
    )


def _excerpt_lines(event: Dict[str, Any]) -> List[str]:
    """Indented excerpt lines for a v2 event; none for a v1 event."""
    if "excerpt" not in event:
        return []
    text = event.get("excerpt")
    if not isinstance(text, str) or not text:
        return [f"  excerpt: unavailable ({event.get('excerpt_unavailable') or 'no reason recorded'})"]
    return [f"  excerpt ({event.get('excerpt_source') or '?'}, {len(text)} chars):"] + \
        [f"  | {line}" for line in text.splitlines()]


def format_mailbox_batch(events: List[Dict[str, Any]], reoffered: List[str], dropped: int,
                         now: Optional[datetime] = None, stale_after: float = CLAIM_STALE_SECONDS) -> str:
    """The `inbox` command's batch: every event with its `id`, then the `ack` line."""
    if not events:
        return f"INBOX=empty dropped={dropped}" if dropped else "INBOX=empty"
    moment = now or datetime.now(timezone.utc)
    again = set(reoffered)
    lines = [f"INBOX=events n={len(events)} reoffered={len(reoffered)} dropped={dropped}"]
    for event in events:
        tail = f" kind={event.get('kind') or '?'} id={event['id']} dispatch={event.get('dispatch_id') or '-'}"
        again_mark = " re-offered" if event["id"] in again else ""
        own = _kind_lines(event)
        if own is not None:
            head, *detail = own
            lines.extend([head + tail + again_mark, *detail])
        else:
            lines.append(_event_line(event, moment) + tail + again_mark)
        lines.extend(_excerpt_lines(event))
    ack_ids = [event_id for event in events for event_id in [event["id"], *(event.get("duplicate_ids") or [])]]
    lines.append("ACK=chief_ops.py ack " + " ".join(ack_ids))
    lines.append(f"Next: handle each (a re-offered id may already be handled), then run the ACK line; "
                 f"an unacked batch is re-offered after {int(stale_after // 60)}m.")
    return "\n".join(lines)


# ---- which reader wakes the chief ---------------------------------------------

def resolve_wake_mode(
    config: Optional[Dict[str, Any]], unread_reason: str = "launcher unreachable",
) -> Tuple[str, bool]:
    """`(WAKE= line, known)` from the launcher's `chief_doorbell` switch.

    A confirmed `true` rings the chief but still names a long fallback waiter
    A doorbell that fails silently otherwise leaves nothing to wake
    a chief that sits idle. An unread or non-boolean setting is `unknown` and
    says arm the full waiter, so a chief never ends a turn with no wake path.
    The line keeps its `WAKE=doorbell` prefix: `hooks/chief_waiter_stop.py`
    reads that prefix and the fallback adds no heartbeat requirement.
    """
    if config is None:
        return f"WAKE=waiter doorbell=unknown reason={unread_reason}", False
    value = config.get("chief_doorbell")
    if value is True:
        return f"WAKE=doorbell doorbell=on fallback=waiter seconds={FALLBACK_WAITER_SECONDS}", True
    if value is False:
        return "WAKE=waiter doorbell=off", True
    return "WAKE=waiter doorbell=unknown reason=launcher config has no boolean chief_doorbell", False
