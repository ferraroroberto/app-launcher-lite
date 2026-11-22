"""The lite `chief_ops.py` `open` verb: a Copilot session in a Coding project, plain or a lane.

It shares `dispatch`'s rails and the transcript helpers with `lite_lanes.py`. An open reports
three facts on their own lines, because each can fail on its own under load: `LAUNCHED` (the
session exists), `PROMPT_DELIVERED` (the launcher accepted the first input) and
`EXCHANGE_CONFIRMED` (the agent's transcript shows it).
"""

from __future__ import annotations

import argparse
import re
import time
import urllib.error
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from chief.steer_delivery import DEFAULT_VERIFY_POLL_INTERVAL, INPUT_NEGATIVE_REASONS

from . import launcher, rails
from .lite_lanes import (
    _transcript_copies,
    _transcript_has_user_turn,
    _worker_cap_inputs,
    chief_managed,
    load_dispatch_brief,
    note_lane_mark,
)

# Seconds a just-spawned Copilot TUI needs before it reads input: a live probe sent text 2 s after
# spawn and it was lost while the launcher answered `delivered`; 8 s after spawn it landed.
DEFAULT_OPEN_SETTLE = 10.0

# How long `open` watches the transcript for the first user turn before calling it stranded.
DEFAULT_OPEN_TIMEOUT = 30.0

# The launch call waits on the session-host's create call (45 s, `_CREATE_TIMEOUT` in
# `src/clients/session.py`) plus the launcher's own checks, so it is held this long;
# past it the launch is polled by its request id, never posted again.
LAUNCH_TIMEOUT = 60.0
# How long a launch that outlived LAUNCH_TIMEOUT is polled before it is reported unknown.
LAUNCH_POLL_WINDOW = 60.0
LAUNCH_POLL_INTERVAL = 2.0

# The first input waits on the launcher's hold for a just-spawned TUI (`_READY_WAIT_S`,
# 15 s) and then the host's input call (`_INPUT_TIMEOUT`, 15 s): its budget covers both,
# so a silent launcher past it really is unanswered, not just slower than the caller.
INPUT_TIMEOUT = 35.0

# Each fact's states: proven, disproved, not established, or not asked (no message to
# deliver). `unknown` never folds into `yes`.
YES = "yes"
NO = "no"
UNKNOWN = "unknown"
NONE = "none"

# Slash commands that start lane work. Their end must reach the chief's mailbox, which only a
# chief-managed session's hooks write, so a plain `open` carrying one is refused.
LANE_COMMAND = re.compile(r"(?<![\w/])/(issue-[a-z-]+|upstream-sync|downstream-sync|cleanup-fleet)\b")


@dataclass
class Fact:
    """One of `open`'s three reported facts and why it stands where it does."""

    state: str
    reason: str = ""

    def line(self, name: str, extra: str = "") -> str:
        tail = f" reason={self.reason}" if self.reason else ""
        return f"{name}={self.state}{extra}{tail}"


@dataclass
class Launch:
    """Where the launch ended: its session id (None when not established), whether it was a
    repeat of an earlier open with the same request id, and why it is unknown."""

    sid: Optional[str]
    resumed: bool = False
    reason: str = ""


def _find_coding_project(base_url: str, repo: str) -> Optional[Dict[str, Any]]:
    """The launcher's Coding-tab row whose id or name is `repo` (case-insensitive), else None."""
    key = repo.strip().lower()
    for app in launcher._request(base_url, "/api/apps").get("apps") or []:
        if app.get("kind") != "claude-code":
            continue
        if key in (str(app.get("id") or "").lower(), str(app.get("name") or "").lower()):
            return app
    return None


def _await_first_turn(args: argparse.Namespace, sid: str, message: str) -> Fact:
    """`exchange-confirmed` once the session's transcript shows `message` as a user turn, or the
    timeout passes. The Copilot exchange endpoint cannot be read yet, so the transcript is the
    only evidence: unreadable means `unknown`, readable-but-absent means `no`."""
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
                return Fact(YES)
        if time.monotonic() >= deadline:
            break
        time.sleep(args.poll_interval)
    if readable:
        return Fact(NO, f"no user turn in the transcript after {args.timeout:g}s; not resent")
    return Fact(UNKNOWN, "the session transcript could not be read")


def _deliver_first_message(args: argparse.Namespace, sid: str, message: str,
                           resumed: bool) -> Tuple[Fact, Fact]:
    """`(prompt-delivered, exchange-confirmed)` for the first message, sent at most once.

    A fresh spawn waits `--settle` first: a message sent while the TUI is still drawing is
    dropped although the launcher answers `delivered`, so the answer to the POST is never
    taken as the exchange. A resumed launch (a repeat of an earlier request id) may already
    carry the message, so the transcript is read first and an unreadable one sends nothing.
    A POST that went unanswered still gets the transcript watch: the write may have landed.
    """
    if resumed:
        copies = _transcript_copies(args.base_url, sid, message)
        if copies:
            return Fact(YES, "already in the transcript from the earlier open; not resent"), Fact(YES)
        if copies is None:
            return (Fact(UNKNOWN, "resumed launch and the transcript could not be read; not resent"),
                    Fact(UNKNOWN, "the session transcript could not be read"))
    else:
        time.sleep(args.settle)
    posted = launcher.post_session_input(args.base_url, sid, message, timeout=INPUT_TIMEOUT)
    if posted["ok"]:
        prompt = Fact(YES, f"the launcher accepted the input ({posted.get('reason') or 'no reason given'})")
    elif posted.get("reason") in INPUT_NEGATIVE_REASONS:
        return (Fact(NO, f"the launcher refused the input ({posted['error']})"),
                Fact(NO, "the input was refused, so there is no turn to confirm"))
    elif posted.get("transport") == launcher.NOT_SENT:
        return (Fact(NO, f"nothing was sent ({posted['error']})"),
                Fact(NO, "nothing was sent, so there is no turn to confirm"))
    else:
        prompt = Fact(UNKNOWN, f"send not answered within {INPUT_TIMEOUT:g}s ({posted['error']}); "
                               f"a write may have landed, not resent")
    exchange = _await_first_turn(args, sid, message)
    if exchange.state == YES and prompt.state != YES:
        prompt = Fact(YES, "confirmed by the transcript")
    return prompt, exchange


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


def _post_open_launch(args: argparse.Namespace, project: Dict[str, Any],
                      request_id: str) -> Optional[Launch]:
    """POST the plain Copilot launch under `request_id`; None after printing a refusal.

    The launcher answers a repeat of the same id from its record of the first call, so a
    launch that outlived LAUNCH_TIMEOUT is polled by its id (`_poll_launch`), and a re-run
    of `open --request-id` resumes it instead of opening a second session."""
    path = f"/api/apps/{project['id']}/launch"
    body = {"mode": "pty", "agent": "copilot", "request_id": request_id}
    resumed = False
    try:
        result = launcher._request(args.base_url, path, method="POST", body=body, timeout=LAUNCH_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            detail = exc.read().decode("utf-8", "replace").strip()[:300] or str(exc.reason)
            print(f"REFUSED=the launcher refused the launch (HTTP {exc.code}): {detail}")
            return None
        reason = f"http-{exc.code}"
    except OSError as exc:
        # A refused connection sent nothing, so it stays a plain failure; a timeout or a
        # connection lost after the request went out may have spawned, so it is polled.
        if launcher.transient_failure(exc) != launcher.MAYBE_SENT:
            raise
        reason = "timeout" if launcher.is_timeout(exc) else f"connection lost ({type(exc).__name__})"
    else:
        sid = (result.get("session") or {}).get("session_id")
        if sid:
            return Launch(str(sid), resumed=bool(result.get("resumed")))
        if result.get("state") != "pending":
            return Launch(None, reason="the launcher answered without a session id")
        # A 202 answers only a repeat whose first call is still spawning.
        reason, resumed = "pending", True
    return _poll_launch(args, project, request_id, reason, resumed)


def _poll_launch(args: argparse.Namespace, project: Dict[str, Any], request_id: str,
                 reason: str, resumed: bool) -> Optional[Launch]:
    """Read the launcher's record of `request_id` until it is done or failed, or
    LAUNCH_POLL_WINDOW passes; None after printing a refusal. A record this launcher does not
    hold (restarted, or too old to resume launches) leaves the launch unknown, never
    "nothing launched"."""
    path = f"/api/apps/{project['id']}/launch/{request_id}"
    deadline = time.monotonic() + LAUNCH_POLL_WINDOW
    last = reason
    while True:
        try:
            record = launcher._request(args.base_url, path)
        except urllib.error.HTTPError as exc:
            last = ("the launcher holds no record of this request" if exc.code == 404
                    else f"launch record unreadable (HTTP {exc.code})")
        except (OSError, ValueError) as exc:
            last = f"launch record unreadable ({type(exc).__name__})"
        else:
            state = record.get("state")
            sid = (record.get("session") or {}).get("session_id")
            if state == "done" and sid:
                return Launch(str(sid), resumed=resumed)
            if state == "failed" and (record.get("status") or 500) < 500:
                print(f"REFUSED=the launcher refused the launch (HTTP {record.get('status')}): "
                      f"{record.get('detail')}")
                return None
            last = f"launch {state or 'record unreadable'}"
            if state == "failed":
                last += f": {record.get('detail')}"
        if time.monotonic() >= deadline:
            return Launch(None, resumed, f"{reason}; then {last} after {LAUNCH_POLL_WINDOW:g}s of polling")
        time.sleep(LAUNCH_POLL_INTERVAL)


def _launch_record_exists(args: argparse.Namespace, project: Dict[str, Any], request_id: str) -> bool:
    """True when the launcher holds a record of `request_id`; an unreadable answer is False,
    so the rails still apply."""
    try:
        launcher._request(args.base_url, f"/api/apps/{project['id']}/launch/{request_id}")
    except (OSError, ValueError):
        return False
    return True


def _mark_open_lane(sid: str, repo: str, number: Optional[int]) -> None:
    """Mark an opened lane chief-managed and print its dispatch id; a marking failure is reported,
    never fatal, because the session is already running."""
    dispatch_id = uuid.uuid4().hex
    try:
        chief_managed.mark(sid, repo, number, dispatch_id=dispatch_id)
    except OSError as exc:
        print(f"WARNING=lane not marked chief-managed ({exc}); its end will not reach the mailbox")
        return
    note_lane_mark(sid, repo, number, dispatch_id)
    print(f"DISPATCH_ID={dispatch_id}", flush=True)


def _print_facts(launched: Fact, prompt: Optional[Fact], exchange: Optional[Fact],
                 message: Optional[str], sid: Optional[str] = None) -> None:
    """The three facts, one line each and always all three: a fact never reached is `unknown`
    when a message was due and `none` when there was no message."""
    unreached = Fact(NONE) if message is None else Fact(UNKNOWN, "not reached: the launch is not confirmed")
    print(launched.line("LAUNCHED", f" session={sid}" if sid else ""))
    print((prompt or unreached).line("PROMPT_DELIVERED"))
    print((exchange or unreached).line("EXCHANGE_CONFIRMED"))


def _plain_open_refusal(args: argparse.Namespace, message: Optional[str], lane: bool) -> Optional[str]:
    """Why a plain open's message cannot go out (it runs lane work), else None."""
    command = LANE_COMMAND.search(message or "") if not lane else None
    if command is None:
        return None
    return (f"the message runs lane work ({command.group(0)}); a plain open never reaches the "
            f"mailbox. Use `dispatch {args.repo} <N>`, or `open --issue <N>` (`--lane` for a queue)")


def cmd_open(args: argparse.Namespace) -> int:
    """A Copilot session in a Coding project, optionally with a first message, left running.

    Plain by default: nothing is marked chief-managed, so its end never reaches the mailbox, and a
    message that starts lane work is refused. `--issue N` or `--lane` makes it a lane: it is marked
    chief-managed, with a dispatch id, before its first message, so its hooks write turn-end and
    session-end events like a dispatched worker's. Exit 0 only when every fact due is `yes`."""
    message = None
    if args.message_file is not None:
        try:
            message = load_dispatch_brief(args.message_file)
        except ValueError as exc:
            print(f"REFUSED={str(exc).replace('brief file', 'message file')}")
            return 1
    lane = args.lane or args.issue is not None
    refusal = _plain_open_refusal(args, message, lane)
    if refusal is not None:
        print(f"REFUSED={refusal}")
        return 1
    project = _find_coding_project(args.base_url, args.repo)
    if project is None:
        print(f"REFUSED=no Coding project named {args.repo!r} in the launcher")
        return 1
    # Resuming a launch the launcher holds skips the rails: the project is busy with the very
    # session it resumes. An id it does not hold is a new launch and meets them as one.
    resuming = bool(args.request_id) and _launch_record_exists(args, project, args.request_id)
    refusal = None if resuming else _open_refusal(args, project)
    if refusal is not None:
        print(f"REFUSED={refusal}")
        return 1
    request_id = args.request_id or uuid.uuid4().hex
    print(f"REQUEST_ID={request_id}", flush=True)
    launch = _post_open_launch(args, project, request_id)
    if launch is None:
        return 1
    if launch.sid is None:
        print(f"OPEN=unconfirmed reason={launch.reason} repo={args.repo}\n"
              f"The launcher may still be spawning this session. Resume with `open {args.repo} "
              f"--request-id {request_id}` and the same message file: it never opens a second "
              f"session. A fresh `open` without the id can double-spawn.")
        _print_facts(Fact(UNKNOWN, launch.reason), None, None, message)
        return 1
    return _finish_open(args, project, launch, message, lane)


def _finish_open(args: argparse.Namespace, project: Dict[str, Any], launch: Launch,
                 message: Optional[str], lane: bool) -> int:
    """Mark a lane, deliver the first message and print the verdict and the three facts."""
    sid = str(launch.sid)
    print(f"LAUNCHED session={sid} repo={args.repo}" + (" (resumed)" if launch.resumed else ""), flush=True)
    if lane:
        _mark_open_lane(sid, str(project.get("name") or args.repo), args.issue)
    launched = Fact(YES, "an earlier open with this request id" if launch.resumed else "")
    if message is None:
        print(f"OPENED session={sid} repo={args.repo}")
        print("MESSAGE=none")
        _print_facts(launched, None, None, message, sid)
        return 0
    prompt, exchange = _deliver_first_message(args, sid, message, launch.resumed)
    if prompt.state == YES and exchange.state == YES:
        print(f"OPENED session={sid} repo={args.repo}")
        print(f"DELIVERED sid={sid} chars={len(message)}")
        _print_facts(launched, prompt, exchange, message, sid)
        return 0
    verdict = "STRANDED" if exchange.state == NO else "UNKNOWN"
    print(f"{verdict} sid={sid} chars={len(message)} reason={exchange.reason or prompt.reason}")
    print(f"SESSION_LIVE session={sid} repo={args.repo}: the session runs without a confirmed first "
          f"message; decide whether to `say` it again or `stop {sid}`")
    _print_facts(launched, prompt, exchange, message, sid)
    return 1


def add_parser(sub: argparse._SubParsersAction) -> None:
    op = sub.add_parser("open", help="open a plain Copilot session in a project, optionally with a first message")
    op.add_argument("repo", help="a Coding-tab project id or name")
    op.add_argument("--message-file", default=None, help="the first message; omit to open without one")
    op.add_argument("--issue", type=int, default=None,
                    help="lane work on this issue: marked chief-managed before the first message")
    op.add_argument("--lane", action="store_true",
                    help="lane work with no single issue (a queue): marked chief-managed, number null")
    op.add_argument("--request-id", default=None,
                    help="resume an earlier open that ended unconfirmed (its REQUEST_ID=); the launcher "
                         "answers from its record of that launch and never spawns a second session")
    op.add_argument("--settle", type=float, default=DEFAULT_OPEN_SETTLE,
                    help="seconds to wait after spawn before sending the message")
    op.add_argument("--timeout", type=float, default=DEFAULT_OPEN_TIMEOUT,
                    help="seconds to watch the transcript for the message before reporting STRANDED")
    op.add_argument("--poll-interval", type=float, default=DEFAULT_VERIFY_POLL_INTERVAL)
    op.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    op.set_defaults(func=cmd_open)
