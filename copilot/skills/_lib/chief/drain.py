"""`chief_ops.py drain`: queue unattended audit issues, then fill free worker slots.

Lite: fleet-config's `chief/drain.py` with four seams changed, everything else
as upstream. The open-issue scan reads the lite GitLab fleet through `glab`
(`fetch_open_issues`, rows in the shape `gh_issue_fetch` produced); the
audit-managed marker comes from `issue_host`; there is no machine-local
projects table, so no repo is switched off with `auto_ship = false`; dispatch
goes through the lite `chief/lite_lanes.py` with its rails. The generic brief is
the lite `drain_brief.md`. Owner-check labels are read through the GitLab owner
check (`owner_check/github.py`), so an opted-in scoped label still excludes an issue.

The chief runs this at the end of every turn, beside `wake`. It does the
mechanical half of "drain the backlog that can run unattended" and leaves the
judgment (verifying each merge, closing each lane) with the chief:

1. **Switch.** `auto_drain` from `/api/board/chief/settings`. Only a literal
   `true` is on. Off, missing or unreadable prints `DRAIN=off reason=...`
   and nothing else happens.
2. **Intake.** An open issue joins the plan queue only when its body starts
   with an `<!-- audit-managed: kind=<kind> -->` marker (not
   `cleanup-deferred`), it has no owner or ledger label, it still lists an
   unchecked `- [ ]` finding, it is not already in
   the plan, drain never added it before, and its repo is not
   `auto_ship = false` in `hooks/projects.toml`.
3. **Dispatch.** Queued rows are dispatched top-down through `cmd_dispatch`, with
   every one of its refusals, skipping a busy repo. A row that carries its own
   `brief` file is briefed with it, whoever queued the row; a row drain added
   with none gets the generic per-kind brief. Each dispatch prints
   `DRAIN_BRIEF=<path> source=row|generic`. A hand-queued row is dispatched
   only when it carries its own `brief` file, every `after` condition holds,
   its repo has no live session and its issue is open without an owner,
   validation or ledger label; each hand row left out prints why. A pass
   dispatches at most the free slots it measured at its start (a fresh spawn is
   not on the board for a while, so dispatch's own live count would read one
   lane too few). The first refusal or unconfirmed dispatch ends the drain for
   this turn.

Drain's ledger (`chief-drain.json` in the hooks state dir) lists every ref it
ever queued. That is how it tells its own rows from hand-queued ones (which
only get the extra checks above), and how a row the chief removed stays
removed: the ref is in the ledger but no longer in the plan.

Every action prints one line (`DRAIN_ADDED=`, `DRAIN_DISPATCHED=`,
`DRAIN_BRIEF=`, `DRAIN_SKIPPED=<ref> reason=`, `DRAIN_STOPPED=<ref> reason=`), then the
summary `DRAIN=done added=N dispatched=M`. `--dry-run` prints `DRAIN_WOULD_*`
lines and writes nothing: no plan, no ledger, no brief, no dispatch.

**Idle slots.** When worker slots are still free after the last dispatch, the
summary is followed by `SLOTS_IDLE=<n> reason=<no-eligible-rows|all-after-unmet|
dispatch-stopped>` (plus `no_lane_blocked=<repos>` for rows only a live lane holds
back); an unreadable cap prints `SLOTS_IDLE=unknown`, and a full cap prints
nothing. A real run records the line, and `slots-idle` (the chief's Stop hook)
reads it back while it is under two minutes old, else counts the slots itself.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import re
import time
import urllib.error
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

import chief_plan
import runtime_files
from active_issue import _iso_z, _now, state_lock, write_rows
from fleet_core.hooks_state import state_dir
from issue_host import _MARKER_RE
from owner_check import github as forge

from . import launcher, lite_lanes, rails

logger = logging.getLogger(__name__)

LEDGER_FILENAME = "chief-drain.json"
# The last real drain's idle-slot line; the Stop hook reads it through `slots-idle` while it is this fresh.
LAST_FILENAME = "chief-drain-last.json"
LAST_FRESH_SECONDS = 120
# The `plan ask --id` that answers for idle slots when no agreed work is left to queue.
IDLE_ASK_ID = "slots-idle"
BRIEF_DIRNAME = "chief-drain-briefs"
BRIEF_TEMPLATE = Path(__file__).with_name("drain_brief.md")

# Lane model per audit kind, by docs/model-tiers.md difficulty; unlisted kinds are easy tier.
KIND_MODELS = {
    "design-drift": "sonnet",
    "design-review": "sonnet",
    "stale": "sonnet",
    "documentation": "sonnet",
    "perf-review": "opus",
    "prompt-drift": "opus",
}
DEFAULT_MODEL = "sonnet"

# A deferral record, not a findings list: nothing in it is for a lane to fix.
EXCLUDED_KINDS = frozenset({"cleanup-deferred"})
# Owner-gated work, and the /codebase-audit ledger, which is not actionable.
EXCLUDED_LABELS = frozenset({"needs-owner", "needs-validation", "audit-meta"})

_DISPATCHED = re.compile(r"^DISPATCHED session=(\S+)", re.MULTILINE)
# An open finding is an unchecked task-list item. A standing ledger with none (a perf-review
# issue whose Fixes section reads "_Nothing over budget._") has no work for a lane.
_OPEN_FINDING = re.compile(r"^[ \t]*[-*][ \t]+\[ \][ \t]", re.MULTILINE)


# ---- pure decisions -----------------------------------------------------------

def drain_switch(reply: Any) -> Optional[str]:
    """None when `auto_drain` is on, else the `DRAIN=off` line. Unknown is never on."""
    settings = reply.get("settings") if isinstance(reply, dict) else None
    if not isinstance(settings, dict):
        return "DRAIN=off reason=settings-unreadable"
    if "auto_drain" not in settings:
        return "DRAIN=off reason=setting-missing"
    value = settings["auto_drain"]
    if value is True:
        return None
    if value is False:
        return "DRAIN=off reason=switch-off"
    return f"DRAIN=off reason=setting-not-boolean value={json.dumps(value)}"


def no_ship_repos(tables: Dict[str, dict]) -> Set[str]:
    """Repos whose merge publishes (`auto_ship = false`), lowercased."""
    return {name.lower() for name, table in tables.items() if table.get("auto_ship") is False}


def plan_refs(doc: Dict[str, Any]) -> Set[str]:
    """Every `<repo>#<N>` the plan names, in its queue or as a lane's item."""
    refs = {f"{row['repo']}{row['ref']}" for row in doc["queue"]}
    refs.update(f"{lane['repo']}{lane['item']}" for lane in doc["lanes"] if lane.get("item"))
    return {ref.lower() for ref in refs}


def _clean_title(title: str, fallback: str) -> str:
    text = re.sub(r"\s+", " ", title or "").strip()
    return (text or fallback)[: chief_plan.MAX_TEXT]


def marker_kind(body: str) -> Optional[str]:
    """The kind named by a body's top-line audit-managed marker, or None."""
    m = _MARKER_RE.match((body or "").lstrip())
    return m.group(1) if m else None


def select_intake(
    issues: List[Dict[str, Any]],
    doc: Dict[str, Any],
    ledger_refs: Set[str],
    no_ship: Set[str],
) -> Tuple[List[Dict[str, str]], List[Tuple[str, str]]]:
    """`(adds, skips)`: the audit issues to queue, and each audit issue left out with why.

    An issue with no audit marker is not drain's business and is never reported.
    """
    in_plan = plan_refs(doc)
    known = {ref.lower() for ref in ledger_refs}
    adds: List[Dict[str, str]] = []
    skips: List[Tuple[str, str]] = []
    for issue in issues:
        kind = marker_kind(issue.get("body") or "")
        if kind is None:
            continue
        repo = (issue.get("repository") or {}).get("name") or ""
        ref = f"{repo}#{issue.get('number')}"
        labels = {(label.get("name") or "").lower() for label in issue.get("labels") or []}
        if kind in EXCLUDED_KINDS:
            reason = f"kind-{kind}"
        elif labels & EXCLUDED_LABELS:
            reason = "label-" + sorted(labels & EXCLUDED_LABELS)[0]
        elif not _OPEN_FINDING.search(issue.get("body") or ""):
            reason = "no-open-findings"
        elif ref.lower() in in_plan:
            reason = "in-plan"
        elif ref.lower() in known:
            reason = "removed-from-plan"
        elif repo.lower() in no_ship:
            reason = "auto-ship-off"
        else:
            adds.append({"ref": ref, "kind": kind, "model": KIND_MODELS.get(kind, DEFAULT_MODEL),
                         "title": _clean_title(issue.get("title") or "", kind)})
            continue
        skips.append((ref, reason))
    return adds, skips


def apply_adds(doc: Dict[str, Any], adds: List[Dict[str, str]]) -> None:
    for add in adds:
        chief_plan.add_item(doc, add["ref"], add["title"], model=add["model"], note=f"auto-drain {add['kind']}")


def queued_rows(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every `queued` row in plan order; parked, merged, waiting and in-flight rows are never drain's."""
    return [row for row in doc["queue"] if row["status"] == chief_plan.QueueStatus.QUEUED]


def _issue_closed(doc: Dict[str, Any], repo: str, local: str) -> Tuple[bool, str]:
    """`(closed, detail)`: the plan says merged, else GitHub says closed; unreadable is not closed."""
    for row in doc["queue"]:
        if (row["repo"].lower() == repo.lower() and row["ref"] == local
                and row["status"] == chief_plan.QueueStatus.MERGED):
            return True, ""
    state = launcher.fetch_issue_state(repo, int(local[1:]))
    if state.get("error"):
        return False, "unreadable"
    return str(state.get("state") or "").upper() == "CLOSED", ""


def _unmet_iter(after: str, doc: Dict[str, Any], busy: Set[str]) -> Iterator[str]:
    """Each `after` condition that does not hold, as `<token> (<why>)`, in order and lazily."""
    for token in after.split():
        if token.startswith("no-lane:"):
            if token[len("no-lane:"):].lower() in busy:
                yield f"{token} (lane live)"
            continue
        repo, local = chief_plan.split_ref(token)
        closed, detail = _issue_closed(doc, repo, local)
        if not closed:
            yield f"{token} ({detail or 'not merged'})"


def unmet_after(after: str, doc: Dict[str, Any], busy: Set[str]) -> Optional[str]:
    """The first `after` condition that does not hold, as `<token> (<why>)`; None when all do."""
    return next(_unmet_iter(after, doc, busy), None)


def no_lane_blockers(after: str, doc: Dict[str, Any], busy: Set[str]) -> Set[str]:
    """The repos whose live lane holds a row back, when that is all holding it back; else empty."""
    unmet = list(_unmet_iter(after, doc, busy))
    if not unmet or not all(item.startswith("no-lane:") for item in unmet):
        return set()
    return {item.split(" ", 1)[0][len("no-lane:"):].lower() for item in unmet}


def _brief_unreadable(path: str) -> Optional[str]:
    """The `reason=` text when the brief file cannot be loaded for dispatch; None when it can."""
    try:
        lite_lanes.load_dispatch_brief(path)
    except ValueError as exc:
        return f"brief-unreadable ({exc})"
    return None


def hand_row_skip(row: Dict[str, Any], doc: Dict[str, Any], busy: Set[str]) -> Optional[str]:
    """Why a hand-queued row is not dispatched this turn (the `reason=` text); None means eligible.

    Cheapest checks first: a row with no brief never costs a `gh` read. A read that
    cannot say is its own reason, never a pass.
    """
    if not row.get("brief"):
        return "no-brief"
    unreadable = _brief_unreadable(row["brief"])
    if unreadable:
        return unreadable
    unmet = unmet_after(row["after"], doc, busy) if row.get("after") else None
    if unmet is not None:
        return f"after-unmet ({unmet})"
    if row["repo"].lower() in busy:
        return "repo-busy"
    state = launcher.fetch_issue_state(row["repo"], int(row["ref"][1:]))
    if state.get("error"):
        return "state-unknown"
    if str(state.get("state") or "").upper() == "CLOSED":
        return "issue-closed"
    labels = {(label.get("name") or "").lower() for label in state.get("labels") or []}
    if labels & EXCLUDED_LABELS:
        return "label-" + sorted(labels & EXCLUDED_LABELS)[0]
    return None


def row_skip(row: Dict[str, Any], doc: Dict[str, Any], known: Set[str], busy: Set[str]) -> Optional[str]:
    """Why a queued row is not dispatched this turn; None means eligible.

    `known` is drain's own refs (lowercased): those only need a free repo, hand rows all of `hand_row_skip`.
    A brief the chief set on a drain-owned row must be readable too: dispatch uses it in place of the
    generic one, and falling back to the generic brief would reverse the chief's scope.
    """
    if f"{row['repo']}{row['ref']}".lower() in known:
        if row["repo"].lower() in busy:
            return "repo-busy"
        return _brief_unreadable(row["brief"]) if row.get("brief") else None
    return hand_row_skip(row, doc, busy)


def note_skip(reason: str, row: Dict[str, Any], doc: Dict[str, Any], busy: Set[str],
              reasons: List[str], blockers: Set[str]) -> None:
    """Collect a skipped row's reason, and the repos whose live lane alone holds it back."""
    reasons.append(reason)
    if reason.startswith("after-unmet (no-lane:"):
        blockers.update(no_lane_blockers(row["after"], doc, busy))


def idle_line(free: Optional[int], reasons: List[str], blockers: Set[str],
              stopped: bool = False, dispatchable: bool = False) -> Optional[str]:
    """The `SLOTS_IDLE=` line for `free` slots nothing filled; None when no slot is idle.

    `free` None is an unreadable cap or occupancy: its own state, never silence. `reasons` holds
    every skipped row's `reason=` text.
    """
    if free is None:
        return "SLOTS_IDLE=unknown reason=cap-unreadable"
    if free <= 0:
        return None
    if stopped:
        reason = "dispatch-stopped"
    elif dispatchable:
        reason = "dispatchable-rows"
    elif reasons and all(item.startswith("after-unmet") for item in reasons):
        reason = "all-after-unmet"
    else:
        reason = "no-eligible-rows"
    suffix = f" no_lane_blocked={','.join(sorted(blockers))}" if blockers else ""
    return f"SLOTS_IDLE={free} reason={reason}{suffix}"


def record_lane(doc: Dict[str, Any], repo: str, local_ref: str, sid: str, model: Optional[str]) -> None:
    """The lane exactly as `plan lane <repo> building --item #N --session <sid>` writes it."""
    chief_plan.set_lane(doc, repo, "building", item=local_ref, session=sid, model=model)
    chief_plan.set_item(doc, f"{repo}{local_ref}", status=chief_plan.QueueStatus.BUILDING)


# ---- state ----------------------------------------------------------------------

def ledger_file() -> Path:
    return state_dir() / LEDGER_FILENAME


def load_ledger(path: Path) -> Dict[str, Any]:
    """`{ref: {kind, added_at}}`; a missing file is empty, an unparseable one raises ValueError."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path.name} unreadable: {exc}") from exc
    refs = doc.get("refs") if isinstance(doc, dict) else None
    if not isinstance(refs, dict):
        raise ValueError(f"{path.name} has no refs mapping")
    return refs


def last_file() -> Path:
    return state_dir() / LAST_FILENAME


def record_last(line: str) -> None:
    """Keep the last real drain's idle-slot line for the Stop hook; a failed write only costs a re-check."""
    path = last_file()
    try:
        with state_lock(path):
            write_rows(path, {"at": time.time(), "line": line})
    except OSError as exc:
        logger.warning("⚠️ chief drain: could not record %s (%s); the Stop hook re-checks live", LAST_FILENAME, exc)


def read_last(now: float) -> Optional[str]:
    """The recorded idle-slot line when it is under `LAST_FRESH_SECONDS` old; else None."""
    try:
        doc = json.loads(last_file().read_text(encoding="utf-8"))
        age = now - float(doc["at"])
        line = doc["line"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return line if isinstance(line, str) and line.startswith("SLOTS_IDLE=") and 0 <= age < LAST_FRESH_SECONDS else None


def remember(path: Path, adds: List[Dict[str, str]]) -> None:
    with state_lock(path):
        refs = load_ledger(path)
        stamp = _iso_z(_now())
        for add in adds:
            refs[add["ref"]] = {"kind": add["kind"], "added_at": stamp}
        write_rows(path, {"version": 1, "refs": refs})


def write_brief(ref: str, kind: str) -> Path:
    text = BRIEF_TEMPLATE.read_text(encoding="utf-8").replace("__REF__", ref).replace("__KIND__", kind)
    path = state_dir() / BRIEF_DIRNAME / (ref.replace("#", "-") + ".md")
    runtime_files.make_dirs(path.parent)
    runtime_files.save_text(path, text)
    return path


def _repo_issues(repo: str) -> List[Dict[str, Any]]:
    rows = forge._json(["issue", "list", "--repo", forge._slug(repo), "--per-page", "100", "--output", "json"])
    return [{"number": r.get("iid"), "title": r.get("title") or "", "body": r.get("description") or "",
             "labels": [{"name": forge.plain_label(str(name))} for name in r.get("labels") or []],
             "repository": {"name": repo}}
            for r in rows or [] if isinstance(r, dict)]


def fetch_open_issues() -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """Open issues across the lite GitLab fleet; a repo glab cannot read is an error row. Tests replace it."""
    issues: List[Dict[str, Any]] = []
    errors: List[Tuple[str, str]] = []
    for repo in forge.fleet_repo_names():
        try:
            issues.extend(_repo_issues(repo))
        except forge.GhError as exc:
            errors.append((repo, str(exc)))
    return issues, errors


# ---- the command -------------------------------------------------------------

def _dispatch_one(args: argparse.Namespace, row: Dict[str, Any], brief_file: str) -> Tuple[Optional[str], str]:
    """`(sid, output)` from one guard-railed `cmd_dispatch`; `sid` None means it did not dispatch."""
    ns = argparse.Namespace(
        repo=row["repo"], number=int(row["ref"][1:]), mode="start", model=row.get("model"),
        effort=None, brief_file=brief_file, yolo_confirmed=False,
        allow_during_fleet_job=False, base_url=args.base_url,
    )
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = lite_lanes.cmd_dispatch(ns)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return None, buf.getvalue() + f"DISPATCH=failed reason={exc}\n"
    match = _DISPATCHED.search(buf.getvalue())
    # rc 0 with no session id means the launcher spawned something this call cannot name.
    sid = match.group(1) if rc == 0 and match and match.group(1) != "None" else None
    if rc == 0 and sid is None:
        return None, buf.getvalue() + "DISPATCH=unconfirmed reason=no-session-id\n"
    return sid, buf.getvalue()


def _free_slots(args: argparse.Namespace, columns: Dict[str, Any], reply: Dict[str, Any]) -> Optional[int]:
    cap, managed, scope_error = lite_lanes._worker_cap_inputs(reply.get("settings") or {})
    if scope_error:
        return None
    alive = rails.alive_worker_count(columns, exclude_sid=rails.caller_session_id(), managed_sids=managed)
    return max(cap - alive, 0)


def _intake(args: argparse.Namespace, plan_path: Path, ledger_path: Path,
            fetch: Callable[[], Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]],
            ) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, str]]]:
    """Queue the eligible audit issues; returns `(plan, ledger, adds)` as they now stand."""
    no_ship = no_ship_repos({})
    issues, errors = fetch()
    for repo, reason in errors:
        print(f"DRAIN_SKIPPED={repo} reason=fetch-error ({reason.splitlines()[0] if reason else '?'})")
    doc = chief_plan.load(plan_path)
    ledger = load_ledger(ledger_path)
    adds, skips = select_intake(issues, doc, set(ledger), no_ship)
    for ref, reason in skips:
        print(f"DRAIN_SKIPPED={ref} reason={reason}")
    verb = "DRAIN_WOULD_ADD" if args.dry_run else "DRAIN_ADDED"
    for add in adds:
        print(f"{verb}={add['ref']} kind={add['kind']} model={add['model']}")
    if args.dry_run:
        apply_adds(doc, adds)  # in memory only, so the dispatch preview sees the new rows
    elif adds:
        # Plan first: a ledger entry without its plan row would read as "removed" forever.
        doc = chief_plan.update(plan_path, lambda d: apply_adds(d, adds))
        remember(ledger_path, adds)
    ledger = {**ledger, **{add["ref"]: {"kind": add["kind"]} for add in adds}}
    return doc, ledger, adds


def cmd_drain(args: argparse.Namespace, fetch: Optional[Callable] = None) -> int:
    try:
        reply = launcher._request(args.base_url, "/api/board/chief/settings")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"DRAIN=off reason=settings-unreadable ({exc})")
        return 0
    off = drain_switch(reply)
    if off is not None:
        print(off)
        return 0

    plan_path = Path(args.plan_file) if args.plan_file else chief_plan.plan_file()
    ledger_path = ledger_file()
    try:
        doc, ledger, adds = _intake(args, plan_path, ledger_path, fetch or fetch_open_issues)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"DRAIN=unknown reason=intake-failed ({exc})")
        return 1

    columns = (launcher._request(args.base_url, "/api/board").get("columns") or {})
    busy = set(rails.repo_occupancy(columns, exclude_sid=rails.caller_session_id()))
    known = {ref.lower() for ref in ledger}
    free = _free_slots(args, columns, reply)
    dispatched = 0
    reasons: List[str] = []
    blockers: Set[str] = set()
    stopped = False
    for row in queued_rows(doc):
        ref = f"{row['repo']}{row['ref']}"
        if free is not None and dispatched >= free:
            # A fresh spawn is not on the board for a while, so dispatch's own live count would
            # read one lane too few; the slots measured at the start are all this pass may fill.
            break
        reason = row_skip(row, doc, known, busy)
        if reason is not None:
            print(f"DRAIN_SKIPPED={ref} reason={reason}")
            note_skip(reason, row, doc, busy, reasons, blockers)
            continue
        model = row.get("model") or "default"
        if args.dry_run:
            busy.add(row["repo"].lower())
            dispatched += 1
            print(f"DRAIN_WOULD_DISPATCH={ref} model={model}")
            continue
        if row.get("brief"):
            brief_file, source = row["brief"], "row"
        else:
            brief_file, source = str(write_brief(ref, (ledger.get(ref) or {}).get("kind") or "audit")), "generic"
        print(f"DRAIN_BRIEF={brief_file} source={source}")
        sid, output = _dispatch_one(args, row, brief_file)
        print(output, end="" if output.endswith("\n") or not output else "\n")
        if sid is None:
            reason = "unconfirmed" if "DISPATCH=unconfirmed" in output else "refused"
            print(f"DRAIN_STOPPED={ref} reason={reason}")
            stopped = True
            break
        doc = chief_plan.update(
            plan_path, lambda d, row=row, sid=sid: record_lane(d, row["repo"], row["ref"], sid, row.get("model")))
        busy.add(row["repo"].lower())
        dispatched += 1
        print(f"DRAIN_DISPATCHED={ref} session={sid} model={model}")
    if args.dry_run:
        print(f"DRAIN=dry-run added={len(adds)} dispatched={dispatched} "
              f"free_slots={'unknown' if free is None else free}")
    else:
        print(f"DRAIN=done added={len(adds)} dispatched={dispatched}")
    line = idle_line(None if free is None else free - dispatched, reasons, blockers, stopped=stopped)
    if line:
        print(line)
    if not args.dry_run:
        record_last(line or "SLOTS_IDLE=0")
    return 0


# ---- the Stop hook's read -----------------------------------------------------------

def covering_ask(doc: Dict[str, Any]) -> Optional[str]:
    """The id of the open `plan ask` that answers for idle slots (`--id slots-idle`), else None."""
    for row in doc["waiting_on_roberto"]:
        if row.get("question") and row.get("id") == IDLE_ASK_ID:
            return IDLE_ASK_ID
    return None


def _switch_off_line(off: str) -> str:
    """`DRAIN=off reason=..` as the idle-slot line: an unreadable switch is unknown, a set one is off."""
    reason = off.partition("reason=")[2].split()[0]
    if reason == "settings-unreadable":
        return "SLOTS_IDLE=unknown reason=settings-unreadable"
    return f"SLOTS_IDLE=off reason={reason}"


def live_idle_line(args: argparse.Namespace, plan_path: Path) -> str:
    """Count the idle slots now, dispatching and writing nothing. A row drain has not started yet
    counts as idle (`dispatchable-rows`): the chief ended its turn without draining."""
    try:
        reply = launcher._request(args.base_url, "/api/board/chief/settings")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return "SLOTS_IDLE=unknown reason=settings-unreadable"
    off = drain_switch(reply)
    if off is not None:
        return _switch_off_line(off)
    try:
        columns = launcher._request(args.base_url, "/api/board").get("columns") or {}
        doc = chief_plan.load(plan_path)
        known = {ref.lower() for ref in load_ledger(ledger_file())}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return "SLOTS_IDLE=unknown reason=state-unreadable"
    free = _free_slots(args, columns, reply)
    if free is None or free <= 0:
        return idle_line(free, [], set()) or "SLOTS_IDLE=0"
    busy = set(rails.repo_occupancy(columns, exclude_sid=rails.caller_session_id()))
    reasons: List[str] = []
    blockers: Set[str] = set()
    dispatchable = False
    for row in queued_rows(doc):
        reason = row_skip(row, doc, known, busy)
        if reason is None:
            dispatchable = True
            busy.add(row["repo"].lower())
        else:
            note_skip(reason, row, doc, busy, reasons, blockers)
    return idle_line(free, reasons, blockers, dispatchable=dispatchable) or "SLOTS_IDLE=0"


def cmd_slots_idle(args: argparse.Namespace) -> int:
    """`chief_ops.py slots-idle`: the idle-slot line, from the last drain when it is under two
    minutes old, else counted now; plus `SLOTS_COVERED=<id>` when an open ask answers for it.
    The chief's Stop hook holds the turn end on it."""
    plan_path = Path(args.plan_file) if args.plan_file else chief_plan.plan_file()
    line = read_last(time.time()) or live_idle_line(args, plan_path)
    print(line)
    value = line[len("SLOTS_IDLE="):].split()[0]
    if value not in ("0", "off"):
        try:
            covered = covering_ask(chief_plan.load(plan_path))
        except ValueError as exc:
            logger.warning("⚠️ chief slots-idle: plan unreadable (%s); treating the slots as uncovered", exc)
            covered = None
        if covered:
            print(f"SLOTS_COVERED={covered}")
    return 0


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Mount `drain` and `slots-idle` with fleet-config's argv."""
    dr = sub.add_parser("drain", help="queue unattended audit issues and fill free worker slots")
    dr.add_argument("--dry-run", action="store_true", help="print what it would add and dispatch; write nothing")
    dr.add_argument("--plan-file", default=None, help="plan to read and write (default: the live chief plan)")
    dr.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    dr.set_defaults(func=cmd_drain)

    si = sub.add_parser("slots-idle", help="idle worker slots: the last drain's line, else counted now")
    si.add_argument("--plan-file", default=None)
    si.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    si.set_defaults(func=cmd_slots_idle)
