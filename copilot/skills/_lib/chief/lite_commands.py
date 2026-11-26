"""The lite `chief_ops.py` subcommands that are not byte-identical ports.

fleet-config's `chief/commands.py` wires every handler to the full launcher and
`gh`; the lite chief runs on Copilot CLI and GitLab, so its handlers live here
and call the ported decision modules (`mailbox`, `rails`, `digest`) unchanged.
Each handler prints the same `KEY=value` lines as its fleet-config counterpart.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

import lite_config
from fleet_core import hooks_state

from . import launcher, mailbox, rails


def reoffer_seconds(config: Optional[Path] = None) -> float:
    """Lite's `chief_reoffer_minutes` in seconds, the value the launcher's doorbell also
    counts stale claims by; `mailbox.CLAIM_STALE_SECONDS` when unset or unreadable."""
    return float(lite_config.minutes("chief_reoffer_minutes", mailbox.CLAIM_STALE_SECONDS, config))


def cmd_inbox(args: argparse.Namespace) -> int:
    """Mailbox v2: claim the pending batch for handling, or just count it."""
    inbox = mailbox.chief_inbox_dir()
    if args.stale_after is None:
        args.stale_after = reoffer_seconds()
    if args.count:
        try:
            print(f"PENDING={mailbox.count_pending(inbox, stale_after=args.stale_after, older_than=args.older_than)}")
        except OSError as exc:
            print(f"PENDING=unknown reason={type(exc).__name__}: {exc}")
            return 1
        return 0
    events, reoffered, dropped = mailbox.claim_mailbox(inbox, stale_after=args.stale_after)
    if events or reoffered or dropped:
        claimed = " ".join(str(e.get("id")) for e in events)
        hooks_state.note_decision(
            inbox.parent, "mailbox",
            f"claimed {len(events)} [{claimed}] reoffered {len(reoffered)} [{' '.join(reoffered)}] "
            f"dropped {dropped}")
    print(mailbox.format_mailbox_batch(events, reoffered, dropped, stale_after=args.stale_after))
    # The context check rides the path the chief runs every wake; it is silent
    # unless due or unknown, and only for the live chief itself.
    caller = rails.caller_session_id()
    if caller:
        from . import (
            lite_compact,  # deferred: only a launcher session pays for the board read
        )

        chief, line = lite_compact.chief_compact_status(args.base_url)
        if chief == caller and not line.startswith(("COMPACT_NOT_DUE", "COMPACT_OFF")):
            print(line)
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    """Retire handled events by id; exit 1 when an id has no file."""
    inbox = mailbox.chief_inbox_dir()
    acked, missing = mailbox.ack_events(inbox, args.ids)
    if args.ids:
        hooks_state.note_decision(
            inbox.parent, "mailbox",
            f"acked {len(acked)} [{' '.join(acked)}] missing {len(missing)} [{' '.join(missing)}]")
    print(f"ACKED={len(acked)}")
    for event_id in missing:
        print(f"ACK_MISSING={event_id}")
    return 1 if missing else 0


def add_mailbox_parsers(sub: argparse._SubParsersAction) -> None:
    """Mount `inbox` and `ack` with fleet-config's argv."""
    ib = sub.add_parser("inbox", help="mailbox v2: claim the pending batch, or --count it")
    ib.add_argument("--count", action="store_true", help="print PENDING=<n> and claim nothing")
    ib.add_argument("--stale-after", type=float, default=None,
                    help="seconds before an unacked claim is re-offered (default: Lite's chief_reoffer_minutes)")
    ib.add_argument("--older-than", type=float, default=0.0,
                    help="with --count: count only events that have waited at least this many seconds")
    ib.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    ib.set_defaults(func=cmd_inbox)

    ak = sub.add_parser("ack", help="mailbox v2: retire handled events by id")
    ak.add_argument("ids", nargs="+")
    ak.set_defaults(func=cmd_ack)
