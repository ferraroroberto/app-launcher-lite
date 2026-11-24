"""The lite `chief_ops.py` lane verbs: board, sessions, exchange, issues, dispatch, say, stop.

The handler bodies are fleet-config's `chief/commands.py`, unchanged where the
lite launcher answers the same endpoint, so every stdout line keeps its contract.
What differs: `board` prints no grokbot line, `dispatch` sends `--model` as typed
(a Copilot model id; there is no Codex catalog to resolve against), and issue
state comes from `launcher.fetch_issue_state`, which reads GitLab through `glab`.
The rails (`refuse_dispatch`, the worker cap, `owner_check_refusal`) are the
ported `chief/rails.py`, verbatim.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

import chief_managed
from fleet_core import hooks_state

from chief.steer_delivery import (
    DEFAULT_VERIFY_POLL_INTERVAL,
    INPUT_DEFERRED,
    INPUT_NEGATIVE_REASONS,
    VERDICT_REASONS,
    DeliveryState,
    classify_exchange_marker,
    deferred_submit_outcome,
    finalize_delivery,
    format_output_age,
    format_verdict_line,
    last_output_age_seconds,
    pending_reason_key,
    recent_output_window,
)

from . import digest, launcher, mailbox, rails

# Slash commands that start lane work. Their end must reach the chief's mailbox, which only a
# chief-managed session's hooks write, so a plain `open`, or a `say` into a session that is not
# chief-managed, carrying one is refused.
LANE_COMMAND = re.compile(r"(?<![\w/])/(issue-[a-z-]+|upstream-sync|downstream-sync|cleanup-fleet)\b")


def note_lane_mark(sid: str, repo: str, number: Any, dispatch_id: str) -> None:
    """Leave the lane mark in `chief-decisions.log` beside the registry it was written to.

    The ported `chief_managed.mark` stays byte-identical to its source, so its Lite callers
    leave the breadcrumb Settings > Save diagnostics reads.
    """
    hooks_state.note_decision(chief_managed.state_file().parent, "lane-mark",
                              f"marked {sid} chief-managed for {repo}#{number} dispatch={dispatch_id}")


DEFAULT_TAIL = 2000

DEFAULT_VERIFY_TIMEOUT = 20.0

# `POST /api/board/issues/start` spawns the PTY before answering, which takes tens
# of seconds under load. A client that gives up early leaves the server spawning
# anyway, and a blind retry then double-spawns.
DISPATCH_TIMEOUT = 120.0

# The head of the message compared against the transcript's user turn (long text arrives truncated).
OPEN_MATCH_CHARS = 80


def effort_outcome(requested: Optional[str], result: dict) -> Optional[str]:
    """The `EFFORT=` line for a dispatch that asked for one, else None.
    Applied only when the launcher's response echoes the same effort: a
    launcher that ignores the field still spawns the lane at its persisted
    per-model effort, so silence is reported as not confirmed, never as applied."""
    if not requested:
        return None
    echoed = result.get("effort")
    if echoed == requested:
        return f"EFFORT={requested} applied=yes"
    reason = "launcher did not echo effort" if echoed is None else f"launcher reported {echoed}"
    return f"EFFORT={requested} applied=not-confirmed reason={reason}"


def load_dispatch_brief(path: str) -> str:
    """A `--brief-file`'s text, or ValueError naming why it can't be one.
    Checked before any board read, so a bad brief never spawns a lane that
    would then start on the bare issue alone."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"brief file unreadable: {path} ({exc.strerror or exc})") from exc
    if not text.strip():
        raise ValueError(f"brief file is empty: {path}")
    return text


def read_brief(file_arg: Optional[str]) -> str:
    """`--file`'s content, or stdin when omitted. `say` never accepts prose
    as a bare CLI arg — it carries the brief the model already composed,
    it never assembles one."""
    if file_arg:
        return Path(file_arg).read_text(encoding="utf-8")
    return sys.stdin.read()


def cmd_board(args: argparse.Namespace) -> int:
    board = launcher._request(args.base_url, "/api/board")
    if args.json:
        print(json.dumps(board, indent=2))
    else:
        print(digest.format_board_digest(board, launcher.board_running_fleet_jobs(board)))
    return 0


# How long `sessions` gives the Board before answering from the session-host roster
# instead: the Board joins transcript reads, which stall under load.
ROSTER_BOARD_TIMEOUT = 5.0


def _roster_columns(base_url: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """`(columns, None)` from the Board, or `(columns, why)` built from the bounded
    `/api/claude-code/roster` probe when the Board did not answer in time; each roster
    card's status is the host's lane state, `unknown` where the host cannot tell.
    A roster that also fails raises, so `main` reports it as its own state."""
    try:
        board = launcher._request(base_url, "/api/board", timeout=ROSTER_BOARD_TIMEOUT)
        return board.get("columns") or {}, None
    except (OSError, ValueError) as exc:
        why = (f"board did not answer within {ROSTER_BOARD_TIMEOUT:g}s" if launcher.is_timeout(exc)
               else f"board unreadable ({type(exc).__name__})")
    rows = launcher._request(base_url, "/api/claude-code/roster").get("sessions") or []
    cards = [{**row, "alive": True, "status": row.get("state") or "unknown",
              "age_seconds": row.get("quiet_seconds")} for row in rows if isinstance(row, dict)]
    return {"claude_turn": cards, "your_turn": []}, why


def cmd_sessions(args: argparse.Namespace) -> int:
    columns, fallback = _roster_columns(args.base_url)
    self_sid = rails.caller_session_id()
    chief_sid = rails.find_chief_session(columns)
    occupancy = rails.repo_occupancy(columns, exclude_sid=self_sid, hide_chief=True)
    if args.json:
        print(json.dumps(occupancy, indent=2))
    else:
        if fallback:
            print(f"SOURCE=session-host reason={fallback}")
        print(digest.format_occupancy(occupancy))
        # Never silently: this is the pre-dispatch "is the repo busy" read, so
        # it has to answer the same question `dispatch` will, and say which
        # card it left out of the answer.
        if self_sid:
            print(f"(excluding this session: {self_sid})")
        if chief_sid and chief_sid != self_sid:
            print(f"(excluding the standing chief: {chief_sid})")
    return 0


def cmd_exchange(args: argparse.Namespace) -> int:
    resolved_sid, reason = launcher.resolve_sid_via_board(args.base_url, args.sid)
    if reason is not None:
        print(f"UNRESOLVABLE reason={reason}")
        return 1
    result = launcher._request(args.base_url, f"/api/board/sessions/{resolved_sid}/exchange")
    if not result.get("available"):
        print(f"UNAVAILABLE reason={result.get('reason')}")
        return 1
    assistant = result.get("assistant") or {}
    text = str(assistant.get("text") or "")
    print(f"SOURCE={result.get('source')}")
    print(f"TIMESTAMP={assistant.get('timestamp')}")
    print(text[-args.tail:])
    return 0


def cmd_issues(args: argparse.Namespace) -> int:
    owner_unknown = False
    for ref in args.refs:
        repo, number = rails.parse_issue_ref(ref)
        data = launcher.fetch_issue_state(repo, number, owner=args.owner)
        if "error" in data:
            # An unresolved namespace is not "no such issue": it exits 1 under its own state.
            unknown = str(data["error"]).startswith(launcher.OWNER_UNKNOWN)
            owner_unknown = owner_unknown or unknown
            print(f"{repo}#{number}: {data['error'] if unknown else 'error: ' + str(data['error'])}")
            continue
        labels = ",".join(str(lab.get("name")) for lab in (data.get("labels") or []))
        print(
            f"{repo}#{number}: {data.get('state')} "
            f"\"{data.get('title')}\" [{labels}] updated={data.get('updatedAt')}"
        )
    return 1 if owner_unknown else 0


def _owner_check_refusal(repo: str, number: int) -> Optional[str]:
    """Refuse a `needs-owner` issue; an unreadable one only prints its warning."""
    refusal, warning = rails.owner_check_refusal(repo, number, launcher.fetch_issue_state(repo, number))
    if warning is not None:
        print(f"WARNING={warning}")
    return refusal


def _chief_managed_sids() -> Tuple[Optional[Set[str]], str]:
    """`(sids, error)` from the chief-managed marks: a missing file is an empty set (nothing
    dispatched yet), an unreadable one is `None` with the reason, never an empty set."""
    path = chief_managed.state_file()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return set(), ""
    except OSError as exc:
        return None, f"{path.name} unreadable: {exc}"
    try:
        rows = json.loads(text)
    except ValueError as exc:
        return None, f"{path.name} is not JSON: {exc}"
    if not isinstance(rows, dict):
        return None, f"{path.name} is not a mapping"
    return set(rows), ""


def _worker_cap_inputs(chief_settings: Dict[str, Any]) -> Tuple[int, Optional[Set[str]], str]:
    """`(cap, managed_sids, scope_error)` from the launcher's chief settings: the cap, the session
    ids the cap counts (`None` counts every live session), and why the scope is unconfirmed."""
    raw_cap = chief_settings.get("worker_cap")
    cap = int(raw_cap) if raw_cap is not None else rails.DEFAULT_WORKER_CAP
    scope, error = rails.resolve_cap_scope(chief_settings.get("worker_cap_scope"))
    if scope != "chief":
        return cap, None, error or ""
    managed_sids, error = _chief_managed_sids()
    return cap, managed_sids, error


def _unconfirmed_dispatch_reason(exc: Exception) -> Optional[str]:
    """Why a failed start POST left the spawn unconfirmed (`timeout`, `http-5xx`), or `None` when
    nothing spawned: a refused/unreachable connection or a 4xx the launcher rejected."""
    if isinstance(exc, urllib.error.HTTPError):
        # A 5xx does not prove nothing spawned: the launcher can answer 503 and still spawn the lane.
        return f"http-{exc.code}" if exc.code >= 500 else None
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return "timeout" if isinstance(reason, TimeoutError) else None


def _unconfirmed_dispatch_text(repo: str, number: int, reason: str) -> str:
    return (
        f"DISPATCH=unconfirmed reason={reason} repo={repo} issue={number}\n"
        f"The launcher may still be spawning this lane, even after an error: the lane can "
        f"surface after a `sessions` read shows nothing. Poll `chief_ops.py sessions` "
        f"for up to a minute and do NOT dispatch again until it shows no {repo} "
        f"session; a retry before then double-spawns. A lane that appears was not marked "
        f"by this call: `chief_managed.py mark <sid> {repo} {number}`."
    )


def lane_preamble(mode: str, skills_folder: Optional[str] = None) -> str:
    """The lines every dispatch brief opens with: where the lane's issue skills are, and how it works.

    The launcher types `/issue-<mode> <N>`, which resolves to whatever sits in the user-scope
    skills folder. On a PC that also runs the full fleet that folder is the full fleet's, whose
    GitHub skill would run `gh` and its own completion ping, so the lane is told where its skills
    are before it starts and the guard hook denies the full fleet's commands if it forgets.
    `skills_folder` is the launcher's `skills_dir` when one is set (the machine's own skills
    repo); None means this checkout's bundled skills. Lite's GitLab helpers stay under its own
    `_lib` either way. The remediation paragraph lives here, not in the skill text, because a
    machine's own skills repo may not say it.
    """
    lite = Path(__file__).resolve().parents[2].as_posix()
    skills = skills_folder or lite
    return (
        f"This is an app-launcher-lite lane on GitLab. The skill text you were handed for `/issue-{mode}` may be "
        f"another fleet's GitHub skill, which uses `gh` and sends its own notifications. Before any other step, "
        f"read `{skills}/issue-{mode}/SKILL.md` and follow it instead; ship with `{skills}/issue-finish/SKILL.md`. "
        f"Those paths only locate the skills: the issue's repository is this session's working directory. "
        f"Use `glab` and the helpers under `{lite}/_lib`; run no `gh` command, no `notify_complete.py`, and no "
        f"helper from a Claude Code user-scope skills or hooks folder (the session's tool guard denies them).\n\n"
        f"Keep going until the issue is merged or you are genuinely blocked. A red gate, an e2e failure or a "
        f"blocking review finding is work to fix, then re-run; it is not a reason to stop. Stop only for a step "
        f"that needs the owner, a destructive git action that needs the owner's confirmation, an environment "
        f"problem you cannot resolve, or a failure that is still red after the bounded retry policy; then say "
        f"which one and what you tried. Run exactly one adversarial review per yolo run.\n\n"
    )


def _skills_folder(base_url: str) -> Tuple[Optional[str], Optional[str]]:
    """`(folder, None)` when all four issue skills are found (folder None = bundled), else `(None, why)`.

    Not established is a refusal too: a lane launched on an unknown answer may have no
    `/issue-start` to run."""
    try:
        reply = launcher._request(base_url, "/api/board/chief/skills")
    except (TimeoutError, urllib.error.URLError, ValueError) as exc:
        return None, f"issue skills not confirmed: the launcher's skills check did not answer ({exc})"
    if not reply.get("ready"):
        return None, str(reply.get("refusal") or "issue skills not confirmed: the launcher gave no reason")
    return (str(reply.get("folder")) if reply.get("source") == "skills_dir" else None), None


def _http_refusal(exc: Exception) -> Optional[str]:
    """The launcher's own words for a 4xx start, else None."""
    if not isinstance(exc, urllib.error.HTTPError) or exc.code >= 500:
        return None
    try:
        detail = json.loads(exc.read().decode("utf-8")).get("detail")
    except (OSError, ValueError, AttributeError):
        detail = None
    return f"launcher refused the start (http-{exc.code}): {detail or exc.reason}"


def cmd_dispatch(args: argparse.Namespace) -> int:
    brief = None
    if args.brief_file is not None:
        try:
            brief = load_dispatch_brief(args.brief_file)
        except ValueError as exc:
            print(f"REFUSED={exc}")
            return 1
    fleet_wide = rails.is_fleet_wide_brief(brief)
    skills_folder, skills_refusal = _skills_folder(args.base_url)
    if skills_refusal:
        print(f"REFUSED={skills_refusal}")
        return 1
    brief = (lane_preamble(args.mode, skills_folder) + (brief or "")).rstrip() + "\n"

    # Copilot is the only agent: a --model value is a Copilot model id, sent as typed.
    model = args.model or None

    board = launcher._request(args.base_url, "/api/board")
    columns = board.get("columns") or {}
    settings = launcher._request(args.base_url, "/api/board/chief/settings")
    worker_cap, managed_sids, scope_error = _worker_cap_inputs(settings.get("settings") or {})
    if scope_error:
        # What the cap counts is unknown: refuse rather than count either way.
        print(f"REFUSED=worker cap scope not confirmed: {scope_error}")
        return 1

    self_sid = rails.caller_session_id()
    reason = rails.refuse_dispatch(
        args.repo,
        args.mode,
        rails.repo_occupancy(columns, exclude_sid=self_sid),
        rails.alive_worker_count(columns, exclude_sid=self_sid, managed_sids=managed_sids),
        worker_cap,
        args.yolo_confirmed,
    ) or _owner_check_refusal(args.repo, args.number)
    if reason is not None:
        print(f"REFUSED={reason}")
        return 1

    running, jobs_error = launcher.fetch_running_fleet_jobs(args.base_url)
    reason = rails.fleet_job_refusal(running, jobs_error, fleet_wide,
                                     args.allow_during_fleet_job)
    if reason is not None:
        print(f"REFUSED={reason}")
        return 1
    if running is None:
        print(f"WARNING=fleet-jobs-unknown reason={jobs_error}")
    for job_id in running or []:
        print(f"WARNING=fleet-job-running job={job_id}")

    body = {"repo": args.repo, "number": args.number, "mode": args.mode}
    if model:
        body["model"] = model
    body["brief"] = brief
    if args.effort:
        body["effort"] = args.effort
    try:
        result = launcher._request(args.base_url, "/api/board/issues/start", method="POST", body=body,
                                   timeout=DISPATCH_TIMEOUT)
    except (TimeoutError, urllib.error.URLError) as exc:
        unconfirmed = _unconfirmed_dispatch_reason(exc)
        if unconfirmed is None:
            refused = _http_refusal(exc)
            if refused is None:
                raise  # refused/unreachable: nothing was spawned, a plain failure
            print(f"REFUSED={refused}")
            return 1
        # The server may still be spawning -- never a refusal, never a blind retry.
        print(_unconfirmed_dispatch_text(args.repo, args.number, unconfirmed))
        return 1
    sid = (result.get("session") or {}).get("session_id")
    # One handle per dispatch, carried by every mailbox event the worker
    # writes, so a re-dispatch of the same issue is told apart.
    dispatch_id = uuid.uuid4().hex
    if sid:
        try:
            chief_managed.mark(str(sid), args.repo, args.number, dispatch_id=dispatch_id)
            note_lane_mark(str(sid), args.repo, args.number, dispatch_id)
        except OSError:
            pass  # best-effort -- a marking failure must never undo a real dispatch
    print(f"DISPATCHED session={sid} repo={args.repo} issue={args.number}")
    print(f"DISPATCH_ID={dispatch_id}")
    if result.get("launched"):
        print(f"LAUNCHED={result['launched']}")
    effort_line = effort_outcome(args.effort, result)
    if effort_line:
        print(effort_line)
    return 0


def _normalise_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _user_turn_count(transcript: Dict[str, Any], message: str) -> int:
    """How many `user` entries of the transcript are `message` (or its truncated head)."""
    wanted = _normalise_text(message)
    count = 0
    for entry in transcript.get("entries") or []:
        if entry.get("kind") != "user":
            continue
        seen = _normalise_text(entry.get("text"))
        if seen and (seen.startswith(wanted[:OPEN_MATCH_CHARS]) or wanted.startswith(seen)):
            count += 1
    return count


def _transcript_has_user_turn(transcript: Dict[str, Any], message: str) -> bool:
    """True when a `user` entry of the transcript is `message` (or its truncated head)."""
    return _user_turn_count(transcript, message) > 0


# What the transcript says about a steer: a copy of it is there (`PRESENT`), the transcript
# was read and holds none (`ABSENT`), or it could not be read (`UNREADABLE`).
PRESENT = "present"
ABSENT = "absent"
UNREADABLE = "unreadable"


def _transcript_copies(base_url: str, sid: str, text: str) -> Optional[int]:
    """How many user turns carry `text`, or None when the transcript cannot be read.
    A transcript that reports `available: false` says nothing about the session, so it is
    unreadable here, never an empty one."""
    try:
        transcript = launcher._request(base_url, f"/api/claude-code/sessions/{sid}/transcript")
    except (OSError, ValueError):
        return None
    if not isinstance(transcript, dict) or transcript.get("available") is False:
        return None
    return _user_turn_count(transcript, text)


def _steer_evidence(base_url: str, sid: str, text: str, baseline: int) -> str:
    """`PRESENT` only when more copies of `text` are in the transcript than before the first
    send, so an identical earlier steer is never taken for this one."""
    copies = _transcript_copies(base_url, sid, text)
    if copies is None:
        return UNREADABLE
    return PRESENT if copies > baseline else ABSENT


# Why a verdict line says a steer is `pending` or `unknown` after every POST failed on transport.
SEND_FAILED_REASONS = {
    "pending": (
        "every send attempt failed on transport and the transcript shows no copy "
        "of the steer; delivery is neither confirmed nor disproved; not resent "
        "further, operator decides"
    ),
    "unknown": (
        "every send attempt failed on transport and the transcript could not be "
        "read, so a copy of the steer may have landed; not resent further, "
        "operator decides"
    ),
}


class SendOutcome:
    """What `_post_with_recovery` did: the last POST's result, how many POSTs it made, and
    the transcript's last word on the steer (None when no transcript read was needed)."""

    def __init__(self, posted: Dict[str, Any], attempts: int, evidence: Optional[str]) -> None:
        self.posted = posted
        self.attempts = attempts
        self.evidence = evidence


def _post_with_recovery(args: argparse.Namespace, text: str, baseline: int) -> SendOutcome:
    """POST `text`, and after a transient transport failure look at the transcript before
    each further POST, at most `RECOVERY_ATTEMPTS` in all. The exact-text check gates every
    resend, so a steer that landed is never sent twice:

    - the transcript holds the steer -> stop, `evidence == PRESENT`;
    - it is readable and holds no copy -> resend;
    - it is unreadable -> resend only while every failure so far proves the request was never
      handled (a refused connection); after a timeout or a reset a write may have landed,
      so it stops.

    An HTTP answer (a 502/409/400/403 negative, or any other status) is never retried.
    """
    maybe_landed = False
    for attempt, delay in enumerate(launcher.RECOVERY_BACKOFF, start=1):
        posted = launcher.post_session_input(args.base_url, args.sid, text)
        if posted["ok"] or posted.get("transport") is None:
            return SendOutcome(posted, attempt, None)
        maybe_landed = maybe_landed or posted["transport"] == launcher.MAYBE_SENT
        time.sleep(delay)
        evidence = _steer_evidence(args.base_url, args.sid, text, baseline)
        if evidence == PRESENT or attempt == launcher.RECOVERY_ATTEMPTS:
            return SendOutcome(posted, attempt, evidence)
        if evidence == UNREADABLE and maybe_landed:
            return SendOutcome(posted, attempt, evidence)
    raise AssertionError("unreachable: the last attempt returns")


def cmd_chief_sid(args: argparse.Namespace) -> int:
    board = launcher._request(args.base_url, "/api/board")
    sid = rails.find_chief_session(board.get("columns") or {})
    print(f"CHIEF_SID={sid or 'none'}")
    return 0 if sid else 1


def _unmanaged_lane_steer(sid: str, text: str) -> Optional[str]:
    """Why a steer that starts lane work cannot go to ``sid``: the session is not chief-managed, so
    the lane's turn ends would never reach the mailbox. An unreadable registry reads as empty."""
    command = LANE_COMMAND.search(text or "")
    if command is None or sid in chief_managed.read_rows(chief_managed.state_file()):
        return None
    return (f"the steer runs lane work ({command.group(0)}) in session {sid[:8]}, which is not "
            f"chief-managed, so its turn ends never reach the mailbox. Mark it first with "
            f"`chief_managed.py mark {sid} <repo> <N>`, or open the lane with `open <project> --issue N` "
            f"(`--lane` for a queue)")


def cmd_say(args: argparse.Namespace) -> int:
    resolved_sid, reason = launcher.resolve_sid_via_board(args.base_url, args.sid)
    if reason is not None:
        print(f"UNRESOLVABLE reason={reason}")
        return 1
    # Resolved once, before the one POST: everything below (the POST, the
    # exchange poll, the board/card reads, and every printed line) must speak
    # about the same session, or the verdict describes a different one.
    args.sid = resolved_sid

    text = read_brief(args.file)
    refusal = _unmanaged_lane_steer(args.sid, text)
    if refusal is not None:
        print(f"REFUSED={refusal}")
        return 2

    send_time = datetime.now(timezone.utc)
    if not args.verify:
        # Plain `say` is one POST: the operator who sees `FAILED` decides on a
        # resend. A traceback here would turn a steer they could deliberately
        # retry into a crash they have to diagnose, so the failure is reported.
        posted = launcher.post_session_input(args.base_url, args.sid, text)
        if not posted["ok"]:
            print(f"FAILED sid={args.sid} chars={len(text)} reason={posted['error']}")
            return 1
        print(f"SENT sid={args.sid} chars={len(text)}")
        return 0

    # Copies of the steer already in the transcript, so a repeated steer is never mistaken
    # for this one. Unreadable counts as none: any copy then reads as new.
    baseline = _transcript_copies(args.base_url, args.sid, text) or 0
    # `--verify` sends at most `RECOVERY_ATTEMPTS` times, and only a transient transport
    # failure with no copy of the steer in the transcript earns a resend.
    outcome = _post_with_recovery(args, text, baseline)
    posted = outcome.posted
    if outcome.evidence == PRESENT:
        print(f"DELIVERED sid={args.sid} chars={len(text)}")
        return 0

    post_reason = posted.get("reason")
    # An authoritative negative needs no poll — the endpoint already said the
    # input was never submitted. Polling an exchange for movement that cannot
    # come would only spend the timeout budget before reporting the same thing.
    if not posted["ok"] and post_reason not in INPUT_NEGATIVE_REASONS:
        print(_unsent_verdict(args.sid, len(text), outcome))
        return 1

    state, marker = DeliveryState.PENDING, {"available": False, "timestamp": None}
    if posted["ok"]:
        state, marker, exact = _poll_delivery(args, post_reason, send_time, text, baseline)
        if exact:
            print(f"DELIVERED sid={args.sid} chars={len(text)}")
            return 0
    return _report_verdict(args, text, state, marker, posted)


def _report_verdict(
    args: argparse.Namespace, text: str, state: DeliveryState,
    marker: Dict[str, Any], posted: Dict[str, Any],
) -> int:
    """Classify the poll's last state against the board and the session card, print the
    verdict line and return the exit code: 0 only for `DELIVERED`."""
    post_reason = posted.get("reason")
    board: Dict[str, Any] = {}
    try:
        board = launcher._request(args.base_url, "/api/board")
    except (urllib.error.URLError, ValueError):
        board = {}
    target_status = rails.find_session_status(board.get("columns") or {}, args.sid)
    card = launcher.fetch_session_card(args.base_url, args.sid)
    last_input = launcher._last_input(card)
    # Measured once: the classifier and the printed line must agree about how
    # long the target has been quiet, or the verdict can contradict the
    # evidence beside it.
    output_age = last_output_age_seconds(card.get("last_output_at"))
    output_window = recent_output_window(args.poll_interval)

    state = finalize_delivery(
        state, target_status,
        marker_available=bool(marker.get("available")),
        post_reason=post_reason,
        last_input=last_input,
        last_output_age=output_age,
        output_window=output_window,
    )
    # DELIVERED is printed only after the full classification, never straight
    # out of the poll loop: exchange movement alone is not proof.
    if state == DeliveryState.DELIVERED:
        print(f"DELIVERED sid={args.sid} chars={len(text)}")
        return 0

    last_input_reason = (last_input or {}).get("reason")
    reason = _verdict_reason(state, post_reason, target_status, output_age,
                             last_input_reason, posted.get("error"))
    print(format_verdict_line(
        state, args.sid, len(text), target_status,
        format_output_age(output_age),
        reason, last_input_reason,
    ))
    # Every non-delivered verdict is non-zero — including the friendly-sounding
    # PENDING. "Likely delivered" is not "delivered", and the moment it exits 0
    # a caller starts treating it as success.
    return 1


def _unsent_verdict(sid: str, chars: int, outcome: SendOutcome) -> str:
    """The verdict line for a steer no POST was accepted for. Never `stranded`: no HTTP answer
    said the input was refused, so a transcript that was read and holds no copy is `pending`
    and one that could not be read is `unknown`."""
    error = outcome.posted["error"]
    if outcome.evidence is None:
        # An HTTP answer that is neither a negative nor transient (a 404, a 500): nothing to read.
        return format_verdict_line("unknown", sid, chars, None, "unknown", f"send failed: {error}")
    key, state = (("pending", "pending") if outcome.evidence == ABSENT else ("unknown", "unknown"))
    detail = f"{SEND_FAILED_REASONS[key]} (attempts={outcome.attempts}; last error: {error})"
    return format_verdict_line(state, sid, chars, None, "unknown", detail)


def _poll_delivery(
    args: argparse.Namespace, post_reason: Optional[str], send_time: datetime,
    text: str, baseline: int,
) -> Tuple[DeliveryState, Dict[str, Any], bool]:
    """Poll after an accepted POST until the transcript holds the steer, the exchange
    shows delivery, the deferred watcher reports, or `--timeout` passes:
    `(state, last marker, exact)`, where `exact` is the transcript proof, the only
    evidence that outranks every other signal."""
    deadline = time.monotonic() + args.timeout
    while True:
        if _steer_evidence(args.base_url, args.sid, text, baseline) == PRESENT:
            return DeliveryState.DELIVERED, {"available": False, "timestamp": None}, True
        marker = launcher.fetch_exchange_marker(args.base_url, args.sid)
        state = classify_exchange_marker(marker["available"], marker["timestamp"], send_time)
        if post_reason == INPUT_DEFERRED:
            # The target is mid-turn, so its exchange advances whether or
            # not this paste was submitted — wait on the watcher's own
            # record instead.
            card = launcher.fetch_session_card(args.base_url, args.sid)
            if deferred_submit_outcome(post_reason, launcher._last_input(card)) != "in_flight":
                return state, marker, False
        elif state == DeliveryState.DELIVERED:
            return state, marker, False
        if time.monotonic() >= deadline:
            return state, marker, False
        time.sleep(args.poll_interval)


def _verdict_reason(
    state: DeliveryState, post_reason: Optional[str], target_status: Optional[str],
    output_age: Optional[float], last_input_reason: Optional[str], post_error: Optional[str],
) -> str:
    """The reason printed beside a non-delivered verdict."""
    if state == DeliveryState.PENDING:
        return VERDICT_REASONS[pending_reason_key(post_reason, target_status, output_age)]
    if state == DeliveryState.UNKNOWN:
        return VERDICT_REASONS["unknown"]
    if post_reason in INPUT_NEGATIVE_REASONS or last_input_reason in INPUT_NEGATIVE_REASONS:
        reason = VERDICT_REASONS["stranded_negative"]
        return f"{reason} ({post_error})" if post_error else reason
    return VERDICT_REASONS["stranded"]


def cmd_stop(args: argparse.Namespace) -> int:
    resolved_sid, reason = launcher.resolve_sid_via_board(args.base_url, args.sid)
    if reason is not None:
        print(f"UNRESOLVABLE reason={reason}")
        return 1
    mode = "kill" if args.kill else "quit"
    try:
        launcher._request(
            args.base_url, f"/api/claude-code/sessions/{resolved_sid}/stop",
            method="POST", body={"mode": mode},
        )
    except (TimeoutError, urllib.error.URLError) as exc:
        if not launcher.is_timeout(exc):
            raise
        return _stop_after_timeout(args.base_url, resolved_sid, mode)
    print(f"STOPPED sid={resolved_sid} mode={mode}")
    return 0


def _stop_after_timeout(base_url: str, sid: str, mode: str) -> int:
    """The stop POST went unanswered: one board read says whether it landed.
    The write alone is never taken as success, and an unreadable board is
    left as `UNCONFIRMED`."""
    try:
        board = launcher._request(base_url, "/api/board")
    except (TimeoutError, urllib.error.URLError, ValueError) as exc:
        print(f"UNCONFIRMED=timeout after {launcher.REQUEST_TIMEOUT:g}s; "
              f"follow-up board read failed ({type(exc).__name__}) sid={sid} mode={mode}")
        return 1
    if rails.session_holds_live_pty(board.get("columns") or {}, sid):
        print(f"STILL_LIVE sid={sid} mode={mode} (stop timed out; the board still shows the session alive)")
        return 1
    print(f"STOPPED sid={sid} mode={mode} (stop timed out; the board no longer shows a live session)")
    return 0


def cmd_wake(args: argparse.Namespace) -> int:
    """How the chief wakes next: the launcher's doorbell switch, then its fallback
    wake. Exit 1 when the doorbell switch is unknown."""
    config: Optional[Dict[str, Any]] = None
    reason = "launcher unreachable"
    try:
        result = launcher._request(args.base_url, "/api/config")
        config = result if isinstance(result, dict) else None
        reason = "launcher config reply was not an object"
    except urllib.error.HTTPError as exc:
        reason = f"launcher config unreadable (HTTP {exc.code})"
    except (urllib.error.URLError, ValueError):
        pass
    line, known = mailbox.resolve_wake_mode(config, reason)
    print(line)
    print(mailbox.resolve_fallback_wake(config))
    return 0 if known else 1


def add_lane_parsers(sub: argparse._SubParsersAction) -> None:
    """Mount the lane verbs with fleet-config's argv (the model help names Copilot ids)."""
    b = sub.add_parser("board", help="~12-line board digest")
    b.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    b.add_argument("--json", action="store_true")
    b.set_defaults(func=cmd_board)

    s = sub.add_parser("sessions", help="repo occupancy")
    s.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sessions)

    e = sub.add_parser("exchange", help="last assistant text for a live session")
    e.add_argument("sid")
    e.add_argument("--tail", type=int, default=DEFAULT_TAIL)
    e.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    e.set_defaults(func=cmd_exchange)

    i = sub.add_parser("issues", help="multi-repo issue-state table (glab)")
    i.add_argument("refs", nargs="+", help="<repo>#<number> ...")
    i.add_argument("--owner", default=None, help="GitLab namespace (default: the launcher's gitlab_group)")
    i.set_defaults(func=cmd_issues)

    d = sub.add_parser("dispatch", help="guard-railed issue-start")
    d.add_argument("repo")
    d.add_argument("number", type=int)
    d.add_argument("--mode", choices=("start", "yolo"), default="start")
    d.add_argument("--model", default=None, help="a Copilot model id, sent as typed")
    d.add_argument("--effort", default=None, choices=("low", "medium", "high", "xhigh"),
                   help="reported applied only when the launcher echoes it")
    d.add_argument("--brief-file", default=None, help="lane brief, delivered in the launch command")
    d.add_argument("--yolo-confirmed", action="store_true")
    d.add_argument("--allow-during-fleet-job", action="store_true",
                   help="dispatch a fleet-wide brief while a fleet-wide job runs")
    d.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    d.set_defaults(func=cmd_dispatch)

    cs = sub.add_parser("chief-sid",help="find the standing chief's live session id")
    cs.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    cs.set_defaults(func=cmd_chief_sid)

    say_p = sub.add_parser("say", help="send input to a live session")
    say_p.add_argument("sid")
    say_p.add_argument("--file", default=None)
    say_p.add_argument("--verify", action="store_true",
                       help="poll the exchange after posting and report DELIVERED/PENDING/STRANDED/UNKNOWN; "
                            "resends only after a transient transport failure, and only while the "
                            "transcript holds no copy of the steer (at most 3 sends)")
    say_p.add_argument("--timeout", type=float, default=DEFAULT_VERIFY_TIMEOUT)
    say_p.add_argument("--poll-interval", type=float, default=DEFAULT_VERIFY_POLL_INTERVAL)
    say_p.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    say_p.set_defaults(func=cmd_say)

    wk = sub.add_parser("wake", help="doorbell or not: read the launcher's chief_doorbell switch")
    wk.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    wk.set_defaults(func=cmd_wake)

    st = sub.add_parser("stop", help="stop a live session (quit by default)")
    st.add_argument("sid")
    st.add_argument("--kill", action="store_true")
    st.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    st.set_defaults(func=cmd_stop)
