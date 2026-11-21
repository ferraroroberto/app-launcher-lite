"""The lite `chief_ops.py` compaction verbs: context, compact-due, self-compact, lane-compact.

fleet-config's handler bodies (`chief/commands.py`) with one seam changed: the
context percentage comes from the Copilot session's own `session.usage_record`
(`chief/copilot_context.py`), not the launcher's `/context` endpoint, which
reads a statusline Copilot does not print. The decisions (`compact.py`: the
threshold, the refusals, the focus text) are the verbatim port. An unreadable
percentage is `unknown` and never compacts.

`/compact <focus>` is posted to the caller's own session input; Copilot queues a
slash command typed mid-turn and runs it when the turn ends. That queueing is a
`copilot_probe.py` manual item (`compact_queued_midturn`), unknown until recorded.
"""

from __future__ import annotations

import argparse
import time
import urllib.error
from pathlib import Path
from typing import Optional, Tuple

import chief_plan
import gate_evidence
import runtime_files
from fleet_core import hooks_state

from . import compact, copilot_context, launcher, rails


def _chief_sid(base_url: str) -> Optional[str]:
    return rails.find_chief_session((launcher._request(base_url, "/api/board").get("columns") or {}))


def chief_compact_status(base_url: str) -> Tuple[Optional[str], str]:
    """`(live chief sid, compact status line)`; any read failure is `COMPACT_DUE=unknown`."""
    try:
        chief = _chief_sid(base_url)
        if not chief:
            return None, "COMPACT_DUE=unknown reason=no live chief session"
        settings, unread = launcher.fetch_chief_settings(base_url)
        threshold, _ = compact.resolve_compact_threshold(settings, None, unread)
        if threshold is None:
            return chief, compact.compact_status_line(None, None)
        info = copilot_context.context_info(chief)
    except (OSError, ValueError) as exc:
        return None, f"COMPACT_DUE=unknown reason={type(exc).__name__}"
    _, percent = compact.format_context_line(chief, info)
    return chief, compact.compact_status_line(percent, threshold, str(info.get("reason") or "percent not reported"))


def cmd_context(args: argparse.Namespace) -> int:
    """Context-window use of `sid` (default: the live chief). Exit 1 when unknown."""
    sid = args.sid
    if sid is None:
        sid = _chief_sid(args.base_url)
        if not sid:
            print("CONTEXT sid=none percent=unknown reason=no live chief session")
            return 1
    else:
        resolved, reason = launcher.resolve_sid_via_board(args.base_url, sid)
        if reason is not None:
            print(f"UNRESOLVABLE reason={reason}")
            return 1
        sid = resolved
    line, percent = compact.format_context_line(str(sid), copilot_context.context_info(str(sid)))
    print(line)
    return 0 if percent is not None else 1


def cmd_compact_due(args: argparse.Namespace) -> int:
    """Whether the live chief's context is at or over the Settings threshold. Exit 1 when unknown."""
    _, line = chief_compact_status(args.base_url)
    print(line)
    return 1 if line.startswith("COMPACT_DUE=unknown") else 0


def _queue_compact(base_url: str, sid: str, focus: str, marker: Path) -> int:
    """Post `/compact <focus>` to `sid`'s own input and leave the queued marker."""
    delivery = launcher.post_session_input(base_url, sid, f"/compact {focus}".strip())
    if not delivery["ok"]:
        print(f"NOT_QUEUED reason={delivery['reason'] or delivery['error']}")
        return 2
    try:
        runtime_files.make_dirs(marker.parent)
        runtime_files.save_text(marker, str(int(time.time())))
    except (OSError, ValueError) as exc:
        print(f"ℹ️ could not write the queued marker ({exc}); the stop hook may hold once more")
    print(f"COMPACT_QUEUED sid={sid[:8]} verdict={delivery['reason'] or 'submitted'} "
          "-- end this turn; it runs when the turn ends")
    return 0


def cmd_self_compact(args: argparse.Namespace) -> int:
    """The chief's automatic compaction of itself, behind every refusal (own session only)."""
    self_sid = rails.caller_session_id()
    if not self_sid:
        print("REFUSED=not a launcher session; self-compact only compacts its own chief session")
        return 1
    if _chief_sid(args.base_url) != self_sid:
        print("REFUSED=the caller is not the live chief; self-compact never compacts another session")
        return 1
    settings, unread = (None, "") if args.threshold is not None else launcher.fetch_chief_settings(args.base_url)
    threshold, note = compact.resolve_compact_threshold(settings, args.threshold, unread)
    print(note)
    if threshold is None:
        print("REFUSED=auto-compact is off")
        return 1
    line, percent = compact.format_context_line(self_sid, copilot_context.context_info(self_sid))
    print(line)
    state = hooks_state.state_dir()
    reason = compact.self_compact_refusal(
        percent, threshold,
        compact.file_age_seconds(state / "chief-handover.md"),
        compact.file_age_seconds(state / chief_plan.STATE_FILENAME),
        args.fresh_within,
    )
    if reason is not None:
        print(f"REFUSED={reason}")
        return 1
    return _queue_compact(args.base_url, self_sid, args.focus, state / compact.COMPACT_QUEUED_MARKER)


def cmd_lane_compact(args: argparse.Namespace) -> int:
    """A chief-dispatched lane compacts its own session after writing its handoff."""
    self_sid = rails.caller_session_id()
    if not self_sid:
        print("REFUSED=not a launcher session; lane-compact only compacts its own lane session")
        return 1
    try:
        chief = _chief_sid(args.base_url)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"NOT_QUEUED reason=launcher unreachable ({type(exc).__name__})")
        return 2
    if chief == self_sid:
        print("REFUSED=the chief compacts itself with self-compact")
        return 1
    state = hooks_state.state_dir()
    handoff = state / compact.LANE_HANDOFF_DIRNAME / f"{self_sid}.md"
    reason = compact.lane_compact_refusal(
        compact.file_age_seconds(handoff), gate_evidence.gate_in_flight(Path.cwd()), args.fresh_within)
    if reason is not None:
        print(f"REFUSED={reason}")
        return 1
    focus = args.focus or compact.lane_compact_focus(handoff.as_posix())
    return _queue_compact(args.base_url, self_sid, focus, state / compact.LANE_QUEUED_DIRNAME / self_sid)


def add_compact_parsers(sub: argparse._SubParsersAction) -> None:
    """Mount the compaction verbs with fleet-config's argv."""
    ctx = sub.add_parser("context", help="a session's context-window use (default: the chief)")
    ctx.add_argument("sid", nargs="?", default=None)
    ctx.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    ctx.set_defaults(func=cmd_context)

    cd = sub.add_parser("compact-due", help="is the chief's context at or over the Settings threshold?")
    cd.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    cd.set_defaults(func=cmd_compact_due)

    sc = sub.add_parser("self-compact", help="the chief compacts itself behind its refusals")
    sc.add_argument("--threshold", type=int, default=None,
                    help="override the Settings threshold (tests); default: launcher Settings, else 30")
    sc.add_argument("--focus", default=compact.DEFAULT_COMPACT_FOCUS)
    sc.add_argument("--fresh-within", type=float, default=compact.COMPACT_FRESH_SECONDS,
                    help="max age in seconds of the handover log and the plan")
    sc.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    sc.set_defaults(func=cmd_self_compact)

    lc = sub.add_parser("lane-compact", help="a dispatched lane compacts itself after writing its handoff")
    lc.add_argument("--focus", default=None, help="compaction focus; default names the handoff file")
    lc.add_argument("--fresh-within", type=float, default=compact.COMPACT_FRESH_SECONDS,
                    help="max age in seconds of the handoff")
    lc.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    lc.set_defaults(func=cmd_lane_compact)
