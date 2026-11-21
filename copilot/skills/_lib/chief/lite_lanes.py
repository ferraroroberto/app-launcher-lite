"""The lite `chief_ops.py` lane verbs: board, sessions, exchange, issues, dispatch, open, say, stop.

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
import sys
import time
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

import chief_managed

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

DEFAULT_TAIL = 2000

DEFAULT_VERIFY_TIMEOUT = 20.0

# `POST /api/board/issues/start` spawns the PTY before answering, which takes tens
# of seconds under load. A client that gives up early leaves the server spawning
# anyway, and a blind retry then double-spawns.
DISPATCH_TIMEOUT = 120.0

# Seconds a just-spawned Copilot TUI needs before it reads input: a live probe sent text 2 s after
# spawn and it was lost while the launcher answered `delivered`; 8 s after spawn it landed.
DEFAULT_OPEN_SETTLE = 10.0

# How long `open` watches the transcript for the first user turn before calling it stranded.
DEFAULT_OPEN_TIMEOUT = 30.0

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
        print(digest.format_board_digest(board, launcher.fetch_running_fleet_jobs(args.base_url)))
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    board = launcher._request(args.base_url, "/api/board")
    self_sid = rails.caller_session_id()
    columns = board.get("columns") or {}
    chief_sid = rails.find_chief_session(columns)
    occupancy = rails.repo_occupancy(columns, exclude_sid=self_sid, hide_chief=True)
    if args.json:
        print(json.dumps(occupancy, indent=2))
    else:
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


def lane_preamble(mode: str) -> str:
    """The lines every dispatch brief opens with, naming this checkout's own issue skills.

    The launcher types `/issue-<mode> <N>`, which resolves to whatever sits in the user-scope
    skills folder. On a PC that also runs the full fleet that folder is the full fleet's, whose
    GitHub skill would run `gh` and its own completion ping, so the lane is told where lite's
    skills are before it starts and the guard hook denies the full fleet's commands if it forgets.
    """
    skills = Path(__file__).resolve().parents[2].as_posix()
    return (
        f"This is a fleet-config-lite lane on GitLab. The skill text you were handed for `/issue-{mode}` may be "
        f"another fleet's GitHub skill, which uses `gh` and sends its own notifications. Before any other step, "
        f"read `{skills}/issue-{mode}/SKILL.md` and follow it instead; ship with `{skills}/issue-finish/SKILL.md`. "
        f"Those paths only locate the skills: the issue's repository is this session's working directory. "
        f"Use `glab` and the helpers under `{skills}/_lib`; run no `gh` command, no `notify_complete.py`, and no "
        f"helper from a Claude Code user-scope skills or hooks folder (the session's tool guard denies them).\n\n"
    )


def cmd_dispatch(args: argparse.Namespace) -> int:
    brief = None
    if args.brief_file is not None:
        try:
            brief = load_dispatch_brief(args.brief_file)
        except ValueError as exc:
            print(f"REFUSED={exc}")
            return 1
    fleet_wide = rails.is_fleet_wide_brief(brief)
    brief = (lane_preamble(args.mode) + (brief or "")).rstrip() + "\n"

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
            raise  # refused/unreachable/4xx: nothing was spawned, a plain failure
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


def _find_coding_project(base_url: str, repo: str) -> Optional[Dict[str, Any]]:
    """The launcher's Coding-tab row whose id or name is `repo` (case-insensitive), else None."""
    key = repo.strip().lower()
    for app in launcher._request(base_url, "/api/apps").get("apps") or []:
        if app.get("kind") != "claude-code":
            continue
        if key in (str(app.get("id") or "").lower(), str(app.get("name") or "").lower()):
            return app
    return None


def _normalise_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _transcript_has_user_turn(transcript: Dict[str, Any], message: str) -> bool:
    """True when a `user` entry of the transcript is `message` (or its truncated head)."""
    wanted = _normalise_text(message)
    for entry in transcript.get("entries") or []:
        if entry.get("kind") != "user":
            continue
        seen = _normalise_text(entry.get("text"))
        if seen and (seen.startswith(wanted[:OPEN_MATCH_CHARS]) or wanted.startswith(seen)):
            return True
    return False


def _await_first_turn(args: argparse.Namespace, sid: str, message: str) -> Tuple[str, str]:
    """`(verdict, reason)` once the session's transcript shows `message` as a user turn, or the
    timeout passes. The Copilot exchange endpoint cannot be read yet, so the transcript is the
    only delivery evidence: unreadable means `unknown`, readable-but-absent means `stranded`."""
    deadline = time.monotonic() + args.timeout
    readable = False
    while True:
        try:
            transcript = launcher._request(args.base_url, f"/api/claude-code/sessions/{sid}/transcript")
        except (urllib.error.URLError, ValueError, TimeoutError):
            pass
        else:
            readable = True
            if _transcript_has_user_turn(transcript, message):
                return "DELIVERED", ""
        if time.monotonic() >= deadline:
            break
        time.sleep(args.poll_interval)
    if readable:
        return "STRANDED", f"no user turn in the transcript after {args.timeout:g}s; not resent"
    return "UNKNOWN", "the session transcript could not be read"


def _send_first_message(args: argparse.Namespace, sid: str, message: str) -> Tuple[str, str]:
    """Wait out the spawn, post `message` exactly once, then confirm it in the transcript.
    A message sent while the TUI is still drawing is dropped even though the launcher answers
    `delivered`, so the answer to the POST is never taken as the verdict."""
    time.sleep(args.settle)
    posted = launcher.post_session_input(args.base_url, sid, message)
    if not posted["ok"]:
        if posted.get("reason") in INPUT_NEGATIVE_REASONS:
            return "STRANDED", f"the launcher refused the input ({posted['error']})"
        return "UNKNOWN", f"send failed: {posted['error']}"
    return _await_first_turn(args, sid, message)


def _open_refusal(args: argparse.Namespace, project: Dict[str, Any]) -> Optional[str]:
    """`dispatch`'s occupied-repo and worker-cap rails for a plain open, else None."""
    board = launcher._request(args.base_url, "/api/board")
    columns = board.get("columns") or {}
    settings = launcher._request(args.base_url, "/api/board/chief/settings")
    worker_cap, managed_sids, scope_error = _worker_cap_inputs(settings.get("settings") or {})
    if scope_error:
        return f"worker cap scope not confirmed: {scope_error}"
    self_sid = rails.caller_session_id()
    return rails.refuse_dispatch(
        str(project.get("name") or args.repo),
        "start",
        rails.repo_occupancy(columns, exclude_sid=self_sid),
        rails.alive_worker_count(columns, exclude_sid=self_sid, managed_sids=managed_sids),
        worker_cap,
        False,
    )


def _post_open_launch(args: argparse.Namespace, project: Dict[str, Any]) -> Tuple[Optional[str], int]:
    """POST the plain Copilot launch: `(sid, 0)`, or `(None, exit code)` after printing why not."""
    path = f"/api/apps/{project['id']}/launch"
    try:
        result = launcher._request(args.base_url, path, method="POST",
                                   body={"mode": "pty", "agent": "copilot"}, timeout=DISPATCH_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            detail = exc.read().decode("utf-8", "replace").strip()[:300] or str(exc.reason)
            print(f"REFUSED=the launcher refused the launch (HTTP {exc.code}): {detail}")
            return None, 1
        unconfirmed = f"http-{exc.code}"
    except (TimeoutError, urllib.error.URLError) as exc:
        unconfirmed = _unconfirmed_dispatch_reason(exc)
        if unconfirmed is None:
            raise
    else:
        sid = (result.get("session") or {}).get("session_id")
        if sid:
            return str(sid), 0
        unconfirmed = "no-session-id"
    print(f"OPEN=unconfirmed reason={unconfirmed} repo={args.repo}\n"
          f"The launcher may still be spawning this session. Poll `chief_ops.py sessions` for up to a "
          f"minute and do NOT open again until it shows no {args.repo} session; a retry double-spawns.")
    return None, 1


def cmd_open(args: argparse.Namespace) -> int:
    """A plain Copilot session in a Coding project, optionally with a first message. It is not
    lane work: nothing is marked chief-managed or tracked in the plan, and the session is left running."""
    message = None
    if args.message_file is not None:
        try:
            message = load_dispatch_brief(args.message_file)
        except ValueError as exc:
            print(f"REFUSED={str(exc).replace('brief file', 'message file')}")
            return 1
    project = _find_coding_project(args.base_url, args.repo)
    if project is None:
        print(f"REFUSED=no Coding project named {args.repo!r} in the launcher")
        return 1
    reason = _open_refusal(args, project)
    if reason is not None:
        print(f"REFUSED={reason}")
        return 1
    sid, code = _post_open_launch(args, project)
    if sid is None:
        return code
    print(f"LAUNCHED session={sid} repo={args.repo}", flush=True)
    if message is None:
        print(f"OPENED session={sid} repo={args.repo}")
        print("MESSAGE=none")
        return 0
    verdict, reason = _send_first_message(args, sid, message)
    if verdict == "DELIVERED":
        print(f"OPENED session={sid} repo={args.repo}")
        print(f"DELIVERED sid={sid} chars={len(message)}")
        return 0
    print(f"{verdict} sid={sid} chars={len(message)} reason={reason}")
    print(f"SESSION_LIVE session={sid} repo={args.repo}: the session runs without its first message; "
          f"decide whether to `say` it again or `stop {sid}`")
    return 1


def cmd_chief_sid(args: argparse.Namespace) -> int:
    board = launcher._request(args.base_url, "/api/board")
    sid = rails.find_chief_session(board.get("columns") or {})
    print(f"CHIEF_SID={sid or 'none'}")
    return 0 if sid else 1


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

    send_time = datetime.now(timezone.utc)
    # The one and only POST, on both the plain and the --verify path. Nothing
    # below this line ever sends again.
    posted = launcher.post_session_input(args.base_url, args.sid, text)

    if not args.verify:
        # Plain `say` is the most-used call, and a traceback here turns a
        # steer the operator could deliberately retry into a crash they have
        # to diagnose. Report the failure instead.
        if not posted["ok"]:
            print(f"FAILED sid={args.sid} chars={len(text)} reason={posted['error']}")
            return 1
        print(f"SENT sid={args.sid} chars={len(text)}")
        return 0

    post_reason = posted.get("reason")
    # An authoritative negative needs no poll — the endpoint already said the
    # input was never submitted. Polling an exchange for movement that cannot
    # come would only spend the timeout budget before reporting the same thing.
    if not posted["ok"] and post_reason not in INPUT_NEGATIVE_REASONS:
        # The endpoint could not be reached at all — that is not evidence
        # either way, so it is `unknown`, never `stranded`.
        print(format_verdict_line(
            "unknown", args.sid, len(text), None, "unknown",
            f"send failed: {posted['error']}",
        ))
        return 1

    state, marker = DeliveryState.PENDING, {"available": False, "timestamp": None}
    if posted["ok"]:
        state, marker = _poll_delivery(args, post_reason, send_time)

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


def _poll_delivery(
    args: argparse.Namespace, post_reason: Optional[str], send_time: datetime,
) -> Tuple[DeliveryState, Dict[str, Any]]:
    """Poll the exchange after an accepted POST until it is delivered, the
    deferred watcher reports, or `--timeout` passes: `(state, last marker)`."""
    deadline = time.monotonic() + args.timeout
    while True:
        marker = launcher.fetch_exchange_marker(args.base_url, args.sid)
        state = classify_exchange_marker(marker["available"], marker["timestamp"], send_time)
        if post_reason == INPUT_DEFERRED:
            # The target is mid-turn, so its exchange advances whether or
            # not this paste was submitted — wait on the watcher's own
            # record instead.
            card = launcher.fetch_session_card(args.base_url, args.sid)
            if deferred_submit_outcome(post_reason, launcher._last_input(card)) != "in_flight":
                return state, marker
        elif state == DeliveryState.DELIVERED:
            return state, marker
        if time.monotonic() >= deadline:
            return state, marker
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
    """How the chief wakes next: the launcher's doorbell switch. Exit 1 when unknown."""
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

    op = sub.add_parser("open", help="open a plain Copilot session in a project, optionally with a first message")
    op.add_argument("repo", help="a Coding-tab project id or name")
    op.add_argument("--message-file", default=None, help="the first message; omit to open without one")
    op.add_argument("--settle", type=float, default=DEFAULT_OPEN_SETTLE,
                    help="seconds to wait after spawn before sending the message")
    op.add_argument("--timeout", type=float, default=DEFAULT_OPEN_TIMEOUT,
                    help="seconds to watch the transcript for the message before reporting STRANDED")
    op.add_argument("--poll-interval", type=float, default=DEFAULT_VERIFY_POLL_INTERVAL)
    op.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    op.set_defaults(func=cmd_open)

    cs = sub.add_parser("chief-sid",help="find the standing chief's live session id")
    cs.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    cs.set_defaults(func=cmd_chief_sid)

    say_p = sub.add_parser("say", help="send input to a live session")
    say_p.add_argument("sid")
    say_p.add_argument("--file", default=None)
    say_p.add_argument("--verify", action="store_true",
                       help="poll the exchange after posting and report DELIVERED/PENDING/STRANDED/UNKNOWN; "
                            "never auto-retries")
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
