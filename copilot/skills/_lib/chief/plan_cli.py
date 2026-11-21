"""`chief_ops.py plan`: the argv for the plan writer, and the journal line each transition leaves.

`chief_plan.py` owns the format, the validation and every mutation; this module
parses the command line, applies one mutation under the state lock and prints
the one-line result. After the plan is written, each transition it made is
journalled (`chief/journal.py`, `docs/chief-journal.md`); a journal failure is
logged and never changes the command's result.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from chief_plan import (
    LANE_STATUSES,
    MODELS,
    QUEUE_STATUSES,
    QueueStatus,
    add_item,
    add_question,
    add_waiting,
    clear_waiting,
    drop_lane,
    load,
    move_item,
    plan_file,
    prune_plan_file,
    remove_item,
    set_item,
    set_lane,
    split_ref,
    update,
)

from chief import journal


def _summary(doc: Dict[str, Any]) -> str:
    return (f"PLAN=written lanes={len(doc['lanes'])} queue={len(doc['queue'])} "
            f"waiting={len(doc['waiting_on_roberto'])} updated_at={doc['updated_at']}")


def render(doc: Dict[str, Any]) -> str:
    """The plan as JSON with one row per line -- still parseable, but a question's
    options stay on its own line instead of spreading over a screen."""
    def dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    def tail(i: int, n: int) -> str:
        return "," if i < n - 1 else ""

    lines = ["{"]
    for i, (key, value) in enumerate(doc.items()):
        if isinstance(value, list) and value:
            lines.append(f"  {dump(key)}: [")
            lines += [f"    {dump(row)}{tail(j, len(value))}" for j, row in enumerate(value)]
            lines.append(f"  ]{tail(i, len(doc))}")
        else:
            lines.append(f"  {dump(key)}: {dump(value)}{tail(i, len(doc))}")
    lines.append("}")
    return "\n".join(lines)


def _ask(doc: Dict[str, Any], args: argparse.Namespace) -> str:
    return add_question(doc, args.question, repo=args.repo, ref=args.ref,
                        detail=args.detail, recommendation=args.recommend,
                        options=args.option, recommended=args.recommended,
                        multi=args.multi, item_id=args.id, text=args.text)


JournalEvent = Tuple[str, str, Optional[str]]  # (kind, text, ref) for chief/journal.py


def _row_ref(row: Dict[str, Any]) -> str:
    return f"{row['repo']}{row['ref']}"


def _lane_mutation(args: argparse.Namespace, events: List[JournalEvent]) -> Callable[[Dict[str, Any]], None]:
    def mutate(doc: Dict[str, Any]) -> None:
        prior = next((lane.get("item") for lane in doc["lanes"] if lane["repo"] == args.repo), None)
        set_lane(doc, args.repo, args.status, args.item, args.session, args.model)
        # A status change on a lane already on the item is not a new dispatch.
        if args.item and args.item != prior:
            ref = f"{args.repo}{args.item}"
            events.append(("dispatched", f"lane started on {ref}", ref))
    return mutate


def _set_mutation(args: argparse.Namespace, events: List[JournalEvent]) -> Callable[[Dict[str, Any]], None]:
    def mutate(doc: Dict[str, Any]) -> None:
        repo, local = split_ref(args.ref)
        row = next((r for r in doc["queue"] if r["repo"] == repo and r["ref"] == local), None)
        prior = row["status"] if row else None
        set_item(doc, args.ref, args.status, args.note, args.title, args.model, args.kind,
                 args.brief, args.after)  # raises for a ref that is not in the queue
        if row is None or row["status"] == prior:
            return
        if row["status"] == QueueStatus.MERGED:
            detail = f" ({row['note']})" if row.get("note") else ""
            events.append(("merged", f"merged: {row['title']}{detail}", _row_ref(row)))
        elif row["status"] == QueueStatus.PARKED:
            why = row.get("blocked_by") or row.get("kind")
            events.append(("parked", f"parked: {row['title']}" + (f" ({why})" if why else ""), _row_ref(row)))
    return mutate


def _ask_mutation(add: Callable[[Dict[str, Any]], str], added: List[str],
                  events: List[JournalEvent]) -> Callable[[Dict[str, Any]], None]:
    def mutate(doc: Dict[str, Any]) -> None:
        item_id = add(doc)
        added.append(item_id)
        row = next(item for item in doc["waiting_on_roberto"] if item.get("id") == item_id)
        events.append(("asked", f"asked: {row['text']}", row.get("ref")))
    return mutate


def _unwait_mutation(args: argparse.Namespace, events: List[JournalEvent]) -> Callable[[Dict[str, Any]], None]:
    def mutate(doc: Dict[str, Any]) -> None:
        before = list(doc["waiting_on_roberto"])
        clear_waiting(doc, args.key)
        events.extend(("answered", f"answered: {row['text']}", row.get("ref"))
                      for row in before if row not in doc["waiting_on_roberto"])
    return mutate


def _mutation(action: str, args: argparse.Namespace, added: List[str],
              events: List[JournalEvent]) -> Callable[[Dict[str, Any]], None]:
    """The one document mutation for a writing `plan` action; the transitions it makes are
    appended to `events` for the journal."""
    def add(doc: Dict[str, Any]) -> None:
        add_item(doc, args.ref, args.title, args.status, args.note, args.at, args.model, args.kind,
                 args.brief, args.after, args.children)
        repo, local = split_ref(args.ref)
        events.append(("filed", f"filed: {args.title}", f"{repo}{local}"))

    simple: Dict[str, Callable[[Dict[str, Any]], None]] = {
        "drop-lane": lambda d: drop_lane(d, args.repo),
        "move": lambda d: move_item(d, args.ref, args.to),
        "remove": lambda d: remove_item(d, args.ref),
        "clear": lambda d: None,
    }
    if action in simple:
        return simple[action]
    return {
        "lane": lambda: _lane_mutation(args, events),
        "add": lambda: add,
        "set": lambda: _set_mutation(args, events),
        "wait": lambda: _ask_mutation(lambda d: add_waiting(d, args.text, args.ref), added, events),
        "ask": lambda: _ask_mutation(lambda d: _ask(d, args), added, events),
        "unwait": lambda: _unwait_mutation(args, events),
    }[action]()


def _run(args: argparse.Namespace) -> int:
    path = Path(args.plan_file) if args.plan_file else plan_file()
    action = args.plan_action
    if action == "show":
        print(render(load(path)))
        return 0
    if action == "prune":
        pruned = prune_plan_file(path, args.status or (QueueStatus.MERGED,))
        print(f"PRUNED={pruned} " + _summary(load(path)))
        return 0
    added: List[str] = []
    events: List[JournalEvent] = []
    doc = update(path, _mutation(action, args, added, events), reset=action == "clear")
    # The plan is already written; a journal failure must not turn it into a failed command.
    for kind, text, ref in events:
        journal.log_transition(journal.journal_for_plan(path), kind, text, ref)
    print(_summary(doc) + "".join(f" id={item}" for item in added))
    return 0


_BRIEF_HELP = "path to the lane brief file `drain` hands the lane; \"\" clears it"
_AFTER_HELP = ("conditions `drain` waits for, space-separated: <repo>#<N> (merged or closed) "
               "and no-lane:<repo>; \"\" clears it")
_KIND_HELP = "why it is parked (needed with --status parked): owner | umbrella | 'blocked-by <repo>#<N>'"


def add_cli(parser: argparse.ArgumentParser) -> None:
    """Mount the plan actions on `chief_ops.py plan`."""
    parser.add_argument("--plan-file", default=None,
                        help="override the plan path (tests); default hooks/state/chief-plan.json")
    acts = parser.add_subparsers(dest="plan_action", required=True)

    acts.add_parser("show", help="print the current plan")

    lane = acts.add_parser("lane", help="set the lane for a repo (upsert)")
    lane.add_argument("repo")
    lane.add_argument("status", choices=LANE_STATUSES)
    lane.add_argument("--item", default=None, help="#N the lane is on")
    lane.add_argument("--session", default=None)
    lane.add_argument("--model", default=None, help=f"{'|'.join(MODELS)}; omitted keeps it, \"\" clears it")

    drop = acts.add_parser("drop-lane", help="remove a repo's lane")
    drop.add_argument("repo")

    add = acts.add_parser("add", help="queue <repo>#<N>")
    add.add_argument("ref")
    add.add_argument("title")
    add.add_argument("--status", choices=QUEUE_STATUSES, default=QueueStatus.QUEUED.value)
    add.add_argument("--note", default=None)
    add.add_argument("--at", type=int, default=None, help="1-based position (default: end)")
    add.add_argument("--model", default=None, help="|".join(MODELS))
    add.add_argument("--kind", default=None, help=_KIND_HELP)
    add.add_argument("--brief", default=None, help=_BRIEF_HELP)
    add.add_argument("--after", default=None, help=_AFTER_HELP)
    add.add_argument("--children", default=None,
                     help="umbrella/wave expansion: <repo>#<N>,... each queued after no-lane:<its repo>, "
                          "with this row's --brief and --model")

    set_p = acts.add_parser("set", help="change a queued item's status, note, title, model, brief or after")
    set_p.add_argument("ref")
    set_p.add_argument("--status", choices=QUEUE_STATUSES, default=None)
    set_p.add_argument("--note", default=None, help='"" clears it')
    set_p.add_argument("--title", default=None)
    set_p.add_argument("--model", default=None, help=f"{'|'.join(MODELS)}; \"\" clears it")
    set_p.add_argument("--kind", default=None, help=_KIND_HELP)
    set_p.add_argument("--brief", default=None, help=_BRIEF_HELP)
    set_p.add_argument("--after", default=None, help=_AFTER_HELP)

    move = acts.add_parser("move", help="move a queued item to a 1-based position")
    move.add_argument("ref")
    move.add_argument("--to", type=int, required=True)

    rm = acts.add_parser("remove", help="drop an item from the queue")
    rm.add_argument("ref")

    prune = acts.add_parser("prune", help="drop every queue row with a status (default merged)")
    prune.add_argument("--status", action="append", choices=QUEUE_STATUSES, default=None,
                       help="status to drop; repeat to widen (default: merged)")

    wait = acts.add_parser("wait", help="add a waiting-on-Roberto item")
    wait.add_argument("text", help="filed as a question (the short form of `ask`, #1102)")
    wait.add_argument("--ref", default=None, help="<repo>#<N>")

    ask = acts.add_parser("ask", help="add a structured question for the Board's answer sheet")
    ask.add_argument("question")
    ask.add_argument("--repo", default=None, help="fleet repo (default: from --ref)")
    ask.add_argument("--ref", default=None, help="<repo>#<N>")
    ask.add_argument("--detail", default=None, help="descriptive context")
    ask.add_argument("--recommend", default=None, help="your recommendation, free text")
    ask.add_argument("--option", action="append", default=None,
                     help='"Label::description"; repeat, up to 4')
    ask.add_argument("--recommended", action="append", default=None,
                     help="an --option label to mark recommended (more than one needs --multi)")
    ask.add_argument("--multi", action="store_true", help="more than one option may be chosen")
    ask.add_argument("--id", default=None, help="stable id (default: the next q<N>)")
    ask.add_argument("--text", default=None,
                     help="the short card line (default: the question, trimmed)")

    unwait = acts.add_parser("unwait", help="clear a waiting item (index, id, repo#N or text)")
    unwait.add_argument("key")

    acts.add_parser("clear", help="reset to an empty plan")
    parser.set_defaults(func=_run)
