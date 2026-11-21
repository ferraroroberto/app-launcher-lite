"""Deterministic operations helper for the lite fleet chief (Copilot CLI).

The lite counterpart of fleet-config's `chief_ops.py`: one CLI the chief calls
instead of retyping board, plan and session operations. The modules behind
each subcommand are byte-identical ports of fleet-config's `skills/_lib/chief/`
wherever the format is shared with app-launcher-lite's readers; the state they
write lives under `~/.copilot/hooks/state/` (`fleet_core/hooks_state.py`,
override `COPILOT_HOOKS_STATE_DIR`).

Every launcher call goes to app-launcher-lite on the webapp port its own
`config/config.json` sets (`lite_ports.py`; 8465 when unset; `--base-url`
overrides, loopback only); every forge read goes through `glab`.

Subcommands
-----------
  board [--json]          ~12-line Board digest (`chief/digest.py`).
  sessions [--json]       repo occupancy: the "is this repo busy" read before a dispatch.
  exchange <sid> [--tail N]   the last assistant text of a live session.
  issues <repo>#<N> ...   one GitLab state line per ref (`glab issue view`).
  dispatch <repo> <N> [--mode start|yolo] [--model M] [--effort E] [--brief-file F]
      Guard-railed `POST /api/board/issues/start`: `rails.refuse_dispatch`
      (occupied repo, worker cap, unconfirmed yolo) and the `needs-owner` label
      refuse before the POST (`REFUSED=...`); a spawn the launcher may still be
      running is `DISPATCH=unconfirmed`, never retried. On success it marks the
      session chief-managed and prints `DISPATCHED`, `DISPATCH_ID=`.
  open <repo> [--message-file F] [--settle S] [--timeout T]
      A plain Copilot session in a Coding project through `POST /api/apps/<id>/launch`,
      behind `dispatch`'s occupied-repo and worker-cap rails. Not lane work: never
      marked chief-managed, never in the plan, left running. With a message it waits
      `--settle`, sends once, and prints `OPENED session=<sid>` plus `DELIVERED` only
      when the transcript shows the message as a user turn; otherwise `STRANDED` or
      `UNKNOWN`, `SESSION_LIVE` and exit 1, never `OPENED`.
  file <repo> --title T --body-file F
      Files one GitLab issue through the launcher's `POST /api/board/issues/file`
      (the Board's file button; no labels). Prints `FILED=<repo>#<N>` and `URL=`,
      `REFUSED=<reason>` for a 4xx or bad input, or `UNCONFIRMED=...` (exit 1) when
      the launcher may have filed it anyway: re-read the board, never retry blind.
  chief-sid               `CHIEF_SID=<sid>|none`.
  say <sid> [--file F] [--verify]   one POST of input; `--verify` reports
      DELIVERED / PENDING / STRANDED / UNKNOWN and never resends.
  stop <sid> [--kill]     quit (or kill) a live session.
  context [<sid>]         `CONTEXT sid=<8> percent=<n>|unknown`, from the session's
      newest Copilot `session.usage_record` (inputTokens / maxPromptTokens).
  compact-due             `COMPACT_DUE` / `COMPACT_NOT_DUE` / `COMPACT_OFF` against the
      Settings threshold; `COMPACT_DUE=unknown` (exit 1) when unreadable.
  self-compact | lane-compact   queue `/compact <focus>` on the caller's own
      session behind `chief/compact.py`'s refusals (`COMPACT_QUEUED`).
  mr-create <N> [--timeout S]   open the branch's merge request with `--yes` and a
      kill timeout: `MR=created url=...` or `MR=failed|timeout|unknown reason=...`.
  merge <repo>#<N> --branch B [--ask]   `glab mr merge --auto-merge`, then GitLab's
      state read back: `MERGE=merged|queued|refused|unknown`; with `--ask` a
      refusal becomes a plan question (`MERGE_ASKED=merge-<repo>-<N>`).
  verify <repo> --expect mr-open|merged [--branch B]
      `STATUS=CLEAN|DIRTY|UNKNOWN`: a clean checkout plus the lane's GitLab
      merge request open (or merged and back on the default branch).

  plan [--plan-file PATH] show|lane|drop-lane|add|set|move|remove|prune|wait|ask|unwait|clear ...
      The chief's structured plan, `chief-plan.json` format v1 (`chief_plan.py`
      is its only writer; `chief/plan_cli.py` the argv). Each transition is
      journalled beside the plan file.

  note <text> [--kind K] [--ref <repo>#<N>] [--journal-file PATH]
      Append one line to `chief-journal.jsonl` for the Board's "since you
      last looked" card (`chief/journal.py`).

  inbox [--count [--older-than SECS]] [--stale-after SECS]
      Mailbox v2 reader (`chief/mailbox.py`): re-offers claims unacked for
      `--stale-after` (default 900 s), then claims every pending event into
      `chief-inbox/.claimed/` and prints the batch (`INBOX=events n=N ...`, one
      block per event, then `ACK=`), or `INBOX=empty`. Nothing is deleted until
      `ack`. `--count` claims nothing and prints `PENDING=<n>`
      (`PENDING=unknown` and exit 1 when the directory can't be listed).

  ack <id> [<id> ...]
      Retires handled events. `ACKED=<n>`, plus `ACK_MISSING=<id>` and exit 1
      for an id with no file.

stdout carries the `KEY=value` result lines the chief acts on; breadcrumbs go to
stderr. Exit 2 with `ERROR: ...` on stderr for a refused write.
"""

from __future__ import annotations

import argparse
import logging
import sys
import urllib.error
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chief import (  # noqa: E402
    drain,
    journal,
    launcher,
    lite_commands,
    lite_compact,
    lite_file,
    lite_lanes,
    lite_merge,
    lite_sweep,
    lite_verify,
    plan_cli,
)


def build_parser() -> argparse.ArgumentParser:
    """The lite `chief_ops` parser: every subcommand this tree carries so far."""
    parser = argparse.ArgumentParser(prog="chief_ops", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    journal.add_parser(sub)
    plan_cli.add_cli(sub.add_parser("plan", help="read or change the chief's plan"))
    lite_commands.add_mailbox_parsers(sub)
    lite_lanes.add_lane_parsers(sub)
    lite_file.add_parser(sub)
    lite_verify.add_parser(sub)
    lite_merge.add_parser(sub)
    lite_sweep.add_parser(sub)
    drain.add_parsers(sub)
    lite_compact.add_compact_parsers(sub)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # Breadcrumbs go to stderr, so every command's stdout format is unchanged.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return args.func(args)
    except (ValueError, urllib.error.URLError, TimeoutError) as exc:
        failure = launcher.transport_failure(exc)
        if failure is not None:
            print(failure[0])
            return failure[1]
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ImportError as exc:
        # A verb whose helper this lite tree does not carry yet (e.g. `--kind owner`
        # needs owner_check): refused before anything is written.
        print(f"ERROR: not available in this checkout: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
