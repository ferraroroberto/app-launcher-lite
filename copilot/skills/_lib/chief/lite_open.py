"""The lite `chief_ops.py` `open` verb: a Copilot session in a Coding project, plain or a lane.

It shares `dispatch`'s rails and the transcript helpers with `lite_lanes.py`.
"""

from __future__ import annotations

import argparse
import re
import time
import urllib.error
import uuid
from typing import Any, Dict, Optional, Tuple

from chief.steer_delivery import DEFAULT_VERIFY_POLL_INTERVAL, INPUT_NEGATIVE_REASONS

from . import launcher, rails
from .lite_lanes import (
    DISPATCH_TIMEOUT,
    _transcript_has_user_turn,
    _unconfirmed_dispatch_reason,
    _worker_cap_inputs,
    chief_managed,
    load_dispatch_brief,
)

# Seconds a just-spawned Copilot TUI needs before it reads input: a live probe sent text 2 s after
# spawn and it was lost while the launcher answered `delivered`; 8 s after spawn it landed.
DEFAULT_OPEN_SETTLE = 10.0

# How long `open` watches the transcript for the first user turn before calling it stranded.
DEFAULT_OPEN_TIMEOUT = 30.0

# Slash commands that start lane work. Their end must reach the chief's mailbox, which only a
# chief-managed session's hooks write, so a plain `open` carrying one is refused.
LANE_COMMAND = re.compile(r"(?<![\w/])/(issue-[a-z-]+|upstream-sync|downstream-sync|cleanup-fleet)\b")


def _find_coding_project(base_url: str, repo: str) -> Optional[Dict[str, Any]]:
    """The launcher's Coding-tab row whose id or name is `repo` (case-insensitive), else None."""
    key = repo.strip().lower()
    for app in launcher._request(base_url, "/api/apps").get("apps") or []:
        if app.get("kind") != "claude-code":
            continue
        if key in (str(app.get("id") or "").lower(), str(app.get("name") or "").lower()):
            return app
    return None


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


def _mark_open_lane(sid: str, repo: str, number: Optional[int]) -> None:
    """Mark an opened lane chief-managed and print its dispatch id; a marking failure is reported,
    never fatal, because the session is already running."""
    dispatch_id = uuid.uuid4().hex
    try:
        chief_managed.mark(sid, repo, number, dispatch_id=dispatch_id)
    except OSError as exc:
        print(f"WARNING=lane not marked chief-managed ({exc}); its end will not reach the mailbox")
        return
    print(f"DISPATCH_ID={dispatch_id}", flush=True)


def cmd_open(args: argparse.Namespace) -> int:
    """A Copilot session in a Coding project, optionally with a first message, left running.

    Plain by default: nothing is marked chief-managed, so its end never reaches the mailbox, and a
    message that starts lane work is refused. `--issue N` or `--lane` makes it a lane: it is marked
    chief-managed, with a dispatch id, before its first message, so its hooks write turn-end and
    session-end events like a dispatched worker's."""
    message = None
    if args.message_file is not None:
        try:
            message = load_dispatch_brief(args.message_file)
        except ValueError as exc:
            print(f"REFUSED={str(exc).replace('brief file', 'message file')}")
            return 1
    lane = args.lane or args.issue is not None
    if message is not None and not lane:
        command = LANE_COMMAND.search(message)
        if command:
            print(f"REFUSED=the message runs lane work ({command.group(0)}); a plain open never reaches "
                  f"the mailbox. Use `dispatch {args.repo} <N>`, or `open --issue <N>` (`--lane` for a queue)")
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
    if lane:
        _mark_open_lane(sid, str(project.get("name") or args.repo), args.issue)
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


def add_parser(sub: argparse._SubParsersAction) -> None:
    op = sub.add_parser("open", help="open a plain Copilot session in a project, optionally with a first message")
    op.add_argument("repo", help="a Coding-tab project id or name")
    op.add_argument("--message-file", default=None, help="the first message; omit to open without one")
    op.add_argument("--issue", type=int, default=None,
                    help="lane work on this issue: marked chief-managed before the first message")
    op.add_argument("--lane", action="store_true",
                    help="lane work with no single issue (a queue): marked chief-managed, number null")
    op.add_argument("--settle", type=float, default=DEFAULT_OPEN_SETTLE,
                    help="seconds to wait after spawn before sending the message")
    op.add_argument("--timeout", type=float, default=DEFAULT_OPEN_TIMEOUT,
                    help="seconds to watch the transcript for the message before reporting STRANDED")
    op.add_argument("--poll-interval", type=float, default=DEFAULT_VERIFY_POLL_INTERVAL)
    op.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    op.set_defaults(func=cmd_open)
