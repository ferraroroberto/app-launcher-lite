"""The chief's structured backlog plan -- the writer.

The Board's "Chief's plan" card renders what the chief
intends to do next: the live lanes, the ordered queue and what is waiting on
Roberto. The handover log (`chief-handover.md`) carries the same intent as
prose for the chief itself; this file carries it as data for a parser. The
format is one contract, documented in `docs/chief-plan.md` -- this module is
its only writer, and `chief_ops.py plan ...` is the only way the chief calls it.

Every write is a locked read-modify-write: load, apply one mutation, validate
the *whole* document, then replace the file atomically (temp file +
`os.replace`, `active_issue.write_rows`). A mutation that would leave an
invalid document raises before anything is written, so the reader never sees
a half-applied or malformed plan. A file this writer cannot parse is refused
too, never silently overwritten -- `plan clear` is the explicit reset.

The writer is deliberately stricter than the readers: it writes only v1's
known fields, single-line strings, and the enumerated statuses. Readers are
tolerant of all of that (unknown fields, unknown statuses) so a future writer
can add to v1 without breaking an older card.

stdlib only.
"""

from __future__ import annotations

import enum
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from active_issue import _iso_z, _now, state_lock, write_rows  # noqa: E402
from fleet_core.hooks_state import state_dir  # noqa: E402

STATE_FILENAME = "chief-plan.json"
VERSION = 1

LANE_STATUSES = ("building", "gate", "idle", "waiting")


class QueueStatus(enum.StrEnum):
    """Where a queue row stands. The guards that tie a status to a kind or a question live in
    `_apply_kind` and `_require_question`; the plan file stores the string."""

    QUEUED = "queued"                    # waiting for a free lane
    BUILDING = "building"                # a lane is working it
    GATE = "gate"                        # a lane holds the verification gate
    MERGED = "merged"                    # shipped; prune_items drops it
    PARKED = "parked"                    # deliberately not dispatched; needs a kind
    WAITING_ROBERTO = "waiting-roberto"  # blocked on a plan ask answer


QUEUE_STATUSES = tuple(status.value for status in QueueStatus)
# Why a row is `parked`. Only `owner` rows are Roberto's to act on, and they
# ride the `needs-owner` label into the Board's "Waiting for start".
PARKED_KINDS = ("owner", "umbrella", "blocked-by")
# The model a lane or build item runs on, picked per task by docs/model-tiers.md.
MODELS = ("opus", "sonnet")

# "One short line" -- long enough for a real title, short enough for a phone chip row.
MAX_TEXT = 200
# A question's prose (question, detail, recommendation) is still one line, but
# it is read on the Board's answer sheet, not in a chip row.
MAX_PROSE = 1000
MAX_LABEL = 80
MAX_OPTIONS = 4

TOP_FIELDS = {"version", "updated_at", "lanes", "queue", "waiting_on_roberto"}
LANE_FIELDS = {"repo", "session", "item", "status", "model"}
QUEUE_FIELDS = {"repo", "ref", "title", "status", "note", "model", "kind", "blocked_by", "brief", "after"}
WAITING_FIELDS = {"text", "ref", "id", "repo", "question", "detail", "recommendation",
                  "options", "multi", "asked_at"}
OPTION_FIELDS = {"label", "description", "recommended"}

_LOCAL_REF = re.compile(r"#[1-9][0-9]*")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_QUALIFIED_REF = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(#[1-9][0-9]*)")
_NO_LANE = re.compile(r"no-lane:([A-Za-z0-9][A-Za-z0-9._-]*)")
_ISO_Z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Letter-first, so `unwait <key>` can still tell an id from a 1-based index.
_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
_AUTO_ID = re.compile(r"q([1-9][0-9]*)")


def plan_file() -> Path:
    """`~/.claude/hooks/state/chief-plan.json`, honouring `CLAUDE_HOOKS_STATE_DIR`."""
    return state_dir() / STATE_FILENAME


def empty_plan() -> Dict[str, Any]:
    return {"version": VERSION, "updated_at": "", "lanes": [], "queue": [],
            "waiting_on_roberto": []}


# ---- validation --------------------------------------------------------------

def _check_text(errors: List[str], where: str, value: Any, *, required: bool,
                limit: int = MAX_TEXT) -> None:
    if not isinstance(value, str):
        errors.append(f"{where}: must be a string")
        return
    if required and not value.strip():
        errors.append(f"{where}: must not be empty")
    if _CONTROL.search(value):
        errors.append(f"{where}: must be one line with no control characters")
    if len(value) > limit:
        errors.append(f"{where}: longer than {limit} characters")


def _check_fields(errors: List[str], where: str, row: Any, allowed: set, required: set) -> bool:
    if not isinstance(row, dict):
        errors.append(f"{where}: must be an object")
        return False
    unknown = sorted(set(row) - allowed)
    if unknown:
        errors.append(f"{where}: unknown field(s) {', '.join(unknown)}")
    missing = sorted(required - set(row))
    if missing:
        errors.append(f"{where}: missing field(s) {', '.join(missing)}")
    return True


def _check_repo(errors: List[str], where: str, value: Any) -> None:
    if not isinstance(value, str) or not _REPO.fullmatch(value):
        errors.append(f"{where}: must be a repo name")


def _check_model(errors: List[str], where: str, row: Dict[str, Any]) -> None:
    if "model" in row and row["model"] not in MODELS:
        errors.append(f"{where}.model: must be one of {', '.join(MODELS)}")


def validate(doc: Any) -> List[str]:
    """Every reason `doc` is not a v1 plan this writer may publish; empty = valid."""
    errors: List[str] = []
    if not _check_fields(errors, "plan", doc, TOP_FIELDS, TOP_FIELDS):
        return errors
    if doc.get("version") != VERSION:
        errors.append(f"version: must be {VERSION}")
    stamp = doc.get("updated_at")
    if not isinstance(stamp, str) or not _ISO_Z.fullmatch(stamp):
        errors.append("updated_at: must be a UTC timestamp like 2026-09-26T14:40:00Z")
    for key in ("lanes", "queue", "waiting_on_roberto"):
        if key in doc and not isinstance(doc[key], list):
            errors.append(f"{key}: must be a list")
    if errors:
        return errors

    lane_repos = set()
    for i, lane in enumerate(doc["lanes"]):
        where = f"lanes[{i}]"
        if not _check_fields(errors, where, lane, LANE_FIELDS, {"repo", "status"}):
            continue
        _check_repo(errors, f"{where}.repo", lane.get("repo"))
        if lane.get("repo") in lane_repos:
            errors.append(f"{where}: a second lane for {lane.get('repo')}")
        lane_repos.add(lane.get("repo"))
        if lane.get("status") not in LANE_STATUSES:
            errors.append(f"{where}.status: must be one of {', '.join(LANE_STATUSES)}")
        if "session" in lane:
            _check_text(errors, f"{where}.session", lane["session"], required=False)
        item = lane.get("item", "")
        if item and (not isinstance(item, str) or not _LOCAL_REF.fullmatch(item)):
            errors.append(f"{where}.item: must be #N or empty")
        _check_model(errors, where, lane)

    seen = set()
    for i, row in enumerate(doc["queue"]):
        where = f"queue[{i}]"
        if not _check_fields(errors, where, row, QUEUE_FIELDS,
                             {"repo", "ref", "title", "status"}):
            continue
        _check_repo(errors, f"{where}.repo", row.get("repo"))
        ref = row.get("ref")
        if not isinstance(ref, str) or not _LOCAL_REF.fullmatch(ref):
            errors.append(f"{where}.ref: must be #N")
        key = (row.get("repo"), ref)
        if key in seen:
            errors.append(f"{where}: {row.get('repo')}{ref} is already queued")
        seen.add(key)
        _check_text(errors, f"{where}.title", row.get("title"), required=True)
        if row.get("status") not in QUEUE_STATUSES:
            errors.append(f"{where}.status: must be one of {', '.join(QUEUE_STATUSES)}")
        if "note" in row:
            _check_text(errors, f"{where}.note", row["note"], required=False)
        _check_model(errors, where, row)
        _check_kind(errors, where, row)
        _check_dispatch_fields(errors, where, row)

    ids: set = set()
    for i, row in enumerate(doc["waiting_on_roberto"]):
        where = f"waiting_on_roberto[{i}]"
        if not _check_fields(errors, where, row, WAITING_FIELDS, {"text"}):
            continue
        _check_waiting(errors, where, row, ids)
    return errors


def _check_kind(errors: List[str], where: str, row: Dict[str, Any]) -> None:
    """A row's optional parked `kind` and, for `blocked-by`, the issue it waits on."""
    kind = row.get("kind")
    blocker = row.get("blocked_by")
    if "kind" in row and kind not in PARKED_KINDS:
        errors.append(f"{where}.kind: must be one of {', '.join(PARKED_KINDS)}")
    if kind == "blocked-by":
        if not isinstance(blocker, str) or not _QUALIFIED_REF.fullmatch(blocker):
            errors.append(f"{where}.blocked_by: must be repo#N when kind is blocked-by")
    elif "blocked_by" in row:
        errors.append(f"{where}.blocked_by: only allowed when kind is blocked-by")


def _check_dispatch_fields(errors: List[str], where: str, row: Dict[str, Any]) -> None:
    """A row's optional `brief` path and `after` conditions, which `chief_ops.py drain` reads."""
    if "brief" in row:
        _check_text(errors, f"{where}.brief", row["brief"], required=True)
    if "after" in row:
        _check_text(errors, f"{where}.after", row["after"], required=True)
        if isinstance(row["after"], str) and row["after"].strip():
            try:
                normalized = parse_after(row["after"])
            except ValueError as exc:
                errors.append(f"{where}.after: {exc}")
            else:
                if normalized != row["after"]:
                    errors.append(f"{where}.after: must be single-space-separated conditions")


def _check_waiting(errors: List[str], where: str, row: Dict[str, Any], ids: set) -> None:
    """One waiting item: v1's `text`/`ref` plus the structured question."""
    _check_text(errors, f"{where}.text", row.get("text"), required=True)
    ref = row.get("ref", "")
    ref_match = _QUALIFIED_REF.fullmatch(ref) if isinstance(ref, str) else None
    if ref and not ref_match:
        errors.append(f"{where}.ref: must be repo#N or empty")
    if "id" in row:
        if not isinstance(row["id"], str) or not _ID.fullmatch(row["id"]):
            errors.append(f"{where}.id: must be a lowercase letter then up to 31 "
                          "letters, digits or hyphens")
        elif row["id"] in ids:
            errors.append(f"{where}.id: {row['id']} is already used by another waiting item")
        else:
            ids.add(row["id"])
    if "asked_at" in row and not (isinstance(row["asked_at"], str)
                                  and _ISO_DATE.fullmatch(row["asked_at"])):
        errors.append(f"{where}.asked_at: must be YYYY-MM-DD")
    if "repo" in row:
        _check_repo(errors, f"{where}.repo", row["repo"])
        if ref_match and row["repo"] != ref_match.group(1):
            errors.append(f"{where}.repo: {row['repo']} does not match ref {ref}")
    for key in ("question", "detail", "recommendation"):
        if key in row:
            _check_text(errors, f"{where}.{key}", row[key], required=True, limit=MAX_PROSE)
    multi = row.get("multi", False)
    if not isinstance(multi, bool):
        errors.append(f"{where}.multi: must be true or false")
    if "options" not in row:
        if multi is True:
            errors.append(f"{where}.multi: needs options to choose from")
        return
    options = row["options"]
    if not isinstance(options, list):
        errors.append(f"{where}.options: must be a list")
        return
    if len(options) > MAX_OPTIONS:
        errors.append(f"{where}.options: at most {MAX_OPTIONS} options, got {len(options)}")
    labels: set = set()
    recommended = 0
    for j, option in enumerate(options):
        at = f"{where}.options[{j}]"
        if not _check_fields(errors, at, option, OPTION_FIELDS, {"label"}):
            continue
        label = option.get("label")
        _check_text(errors, f"{at}.label", label, required=True, limit=MAX_LABEL)
        if isinstance(label, str) and label.strip():
            if label in labels:
                errors.append(f"{at}.label: {label!r} is already an option")
            labels.add(label)
        if "description" in option:
            _check_text(errors, f"{at}.description", option["description"], required=False)
        if "recommended" in option:
            if not isinstance(option["recommended"], bool):
                errors.append(f"{at}.recommended: must be true or false")
            elif option["recommended"]:
                recommended += 1
    if recommended > 1 and multi is not True:
        errors.append(f"{where}.options: {recommended} options are recommended; "
                      "at most one unless multi is true")


# ---- load / write ------------------------------------------------------------

def load(path: Path) -> Dict[str, Any]:
    """The current plan; a missing file is the empty plan.

    Raises ValueError for a file that exists but is not a valid v1 plan -- the
    writer refuses to build on it rather than overwrite it silently.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_plan()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON ({exc}); `plan clear` resets it") from exc
    problems = validate(doc)
    if problems:
        raise ValueError(f"{path.name} is not a valid v1 plan ({problems[0]}); "
                         "`plan clear` resets it")
    return doc


def update(
    path: Path,
    mutate: Callable[[Dict[str, Any]], None],
    *,
    now: Optional[datetime] = None,
    reset: bool = False,
) -> Dict[str, Any]:
    """Apply one mutation under the state lock and publish the result atomically.

    Nothing is written unless the mutated document validates. `reset` starts
    from the empty plan instead of loading (the `clear` escape hatch for a
    file this writer cannot parse).
    """
    with state_lock(path):
        doc = empty_plan() if reset else load(path)
        mutate(doc)
        doc["updated_at"] = _iso_z(now or _now())
        problems = validate(doc)
        if problems:
            raise ValueError("; ".join(problems))
        write_rows(path, doc)
    return doc


# ---- mutations (pure, on a loaded document) ----------------------------------

def split_ref(ref: str) -> Tuple[str, str]:
    """`repo#N` -> (`repo`, `#N`)."""
    match = _QUALIFIED_REF.fullmatch(ref.strip())
    if not match:
        raise ValueError(f"expected <repo>#<number>, got {ref!r}")
    return match.group(1), match.group(2)


def _index(doc: Dict[str, Any], ref: str) -> int:
    repo, local = split_ref(ref)
    for i, row in enumerate(doc["queue"]):
        if row["repo"] == repo and row["ref"] == local:
            return i
    raise ValueError(f"{ref} is not in the queue")


def _set_model(row: Dict[str, Any], model: Optional[str]) -> None:
    """`None` leaves the row's model alone, `""` clears it, anything else must be in MODELS."""
    if model is None:
        return
    if not model:
        row.pop("model", None)
    elif model in MODELS:
        row["model"] = model
    else:
        raise ValueError(f"--model must be one of {', '.join(MODELS)} (\"\" clears it), got {model!r}")


def set_lane(doc: Dict[str, Any], repo: str, status: str,
             item: Optional[str] = None, session: Optional[str] = None,
             model: Optional[str] = None) -> None:
    """Upsert the lane for `repo` (one lane per repo).

    `item` and `session` are replaced with the row; the lane's model outlives a
    status change, so an omitted `model` keeps it and `""` clears it.
    """
    existing = next((i for i, lane in enumerate(doc["lanes"]) if lane["repo"] == repo), None)
    lane: Dict[str, Any] = {"repo": repo, "status": status}
    if item:
        lane["item"] = item
    if session:
        lane["session"] = session
    if existing is not None and "model" in doc["lanes"][existing]:
        lane["model"] = doc["lanes"][existing]["model"]
    _set_model(lane, model)
    if existing is None:
        doc["lanes"].append(lane)
    else:
        doc["lanes"][existing] = lane


def drop_lane(doc: Dict[str, Any], repo: str) -> None:
    kept = [lane for lane in doc["lanes"] if lane["repo"] != repo]
    if len(kept) == len(doc["lanes"]):
        raise ValueError(f"no lane for {repo}")
    doc["lanes"] = kept


def _require_question(doc: Dict[str, Any], ref: str) -> None:
    """A row may wait on Roberto only while an open `plan ask` question names it.

    A bare `waiting-roberto` status renders on the Board as "waiting on you" with
    nothing to answer. Enforced by the mutations, not `validate()`, so a plan file
    written before the rule still loads.
    """
    repo, local = split_ref(ref)
    qualified = f"{repo}{local}"
    if not any(row.get("question") and row.get("ref") == qualified for row in doc["waiting_on_roberto"]):
        raise ValueError(
            f"{qualified} can wait on Roberto only behind an answerable question: run "
            f'`plan ask "<question>" --ref {qualified} --detail "..." --recommend "..." '
            f'[--option "Label::description" ...]` first, then set waiting-roberto')


def parse_after(spec: str) -> str:
    """`"repo#N no-lane:repo"` -> the conditions, single-space separated.

    Each condition is `repo#N` (that issue is merged or closed) or `no-lane:<repo>`
    (no live worker session in that repo). Commas also separate. All must hold.
    """
    tokens = spec.replace(",", " ").split()
    if not tokens:
        raise ValueError("needs at least one condition")
    for token in tokens:
        if not (_QUALIFIED_REF.fullmatch(token) or _NO_LANE.fullmatch(token)):
            raise ValueError(f"unknown condition {token!r}; use <repo>#<N> or no-lane:<repo>")
    return " ".join(tokens)


def _set_dispatch_fields(row: Dict[str, Any], brief: Optional[str], after: Optional[str]) -> None:
    """`None` leaves a field alone, `""` clears it; `after` is normalized."""
    if brief is not None:
        if brief.strip():
            row["brief"] = brief.strip()
        else:
            row.pop("brief", None)
    if after is not None:
        if after.strip():
            row["after"] = parse_after(after)
        else:
            row.pop("after", None)


def parse_kind(spec: str) -> Tuple[str, str]:
    """`owner` | `umbrella` | `blocked-by <repo>#<N>` -> (kind, blocked_by or "")."""
    kind, _, rest = spec.strip().partition(" ")
    rest = rest.strip()
    if kind not in PARKED_KINDS:
        raise ValueError(f"unknown kind {spec!r}; use owner, umbrella or 'blocked-by <repo>#<N>'")
    if kind == "blocked-by":
        if not rest:
            raise ValueError("blocked-by needs the blocking issue: --kind 'blocked-by <repo>#<N>'")
        repo, local = split_ref(rest)
        return kind, f"{repo}{local}"
    if rest:
        raise ValueError(f"kind {kind!r} takes no argument, got {rest!r}")
    return kind, ""


def owner_label_state(ref: str) -> Optional[bool]:
    """Whether the issue carries `needs-owner`; None when GitHub could not say.

    One `gh` read per owner-kind write: the label is what puts the item in the
    Board's "Waiting for start", so a kind that disagrees with it is the drift
    this check exists to stop. Tests replace this function.
    """
    from owner_check import block, github  # lazy: only an owner-kind write needs gh
    repo, local = split_ref(ref)
    try:
        issue = github.fetch_issue(repo, int(local[1:]))
    except github.GhError:
        return None
    return block.LABELS["before-start"] in issue["labels"]


def _apply_kind(row: Dict[str, Any], status: str, kind: Optional[str]) -> None:
    """Stamp or drop a row's parked kind so it always matches its status.

    A `parked` status needs a kind, passed now or already on the row. Leaving
    `parked` drops it. An `owner` kind is refused unless the issue already carries
    `needs-owner`, because that label is the Board's only owner list.
    """
    if status != QueueStatus.PARKED:
        if kind:
            raise ValueError(f"--kind only applies to a parked item, not {status!r}")
        row.pop("kind", None)
        row.pop("blocked_by", None)
        return
    if not kind and not row.get("kind"):
        raise ValueError("parked needs a kind: --kind owner (Roberto's step; the issue must carry "
                         "needs-owner via owner_check mark), --kind umbrella (kept open on purpose) "
                         "or --kind 'blocked-by <repo>#<N>'")
    if not kind:
        return
    parsed, blocker = parse_kind(kind)
    if parsed == "owner":
        ref = f"{row['repo']}{row['ref']}"
        state = owner_label_state(ref)
        if state is None:
            raise ValueError(f"could not confirm {ref} carries needs-owner (GitHub unreadable); "
                             "not written")
        if not state:
            raise ValueError(f"{ref} has no needs-owner label, so it would not reach Waiting for "
                             f'start: run `owner_check mark {ref} --kind before-start --reason "..." '
                             '--try "..."` first')
    row["kind"] = parsed
    if blocker:
        row["blocked_by"] = blocker
    else:
        row.pop("blocked_by", None)


def _child_refs(doc: Dict[str, Any], parent: str, children: str) -> List[str]:
    """The qualified refs in `--children`, each checked so the expansion is all-or-nothing."""
    refs: List[str] = []
    for token in children.replace(",", " ").split():
        repo, local = split_ref(token)
        refs.append(f"{repo}{local}")
    if not refs:
        raise ValueError("--children needs at least one <repo>#<N>")
    queued = {f"{row['repo']}{row['ref']}".lower() for row in doc["queue"]}
    seen: Set[str] = set()
    for child in refs:
        if child.lower() == parent.lower():
            raise ValueError(f"{child} cannot be a child of itself")
        if child.lower() in seen:
            raise ValueError(f"{child} is named twice in --children")
        if child.lower() in queued:
            raise ValueError(f"{child} is already in the queue")
        seen.add(child.lower())
    return refs


def add_item(doc: Dict[str, Any], ref: str, title: str, status: str = QueueStatus.QUEUED,
             note: Optional[str] = None, at: Optional[int] = None,
             model: Optional[str] = None, kind: Optional[str] = None,
             brief: Optional[str] = None, after: Optional[str] = None,
             children: Optional[str] = None) -> None:
    """Queue `ref`; `at` is a 1-based position (default: the end).

    `children` (`<repo>#<N>,...`) is an umbrella or wave expansion: each child is also queued, in
    order straight after `ref`, as `queued` with `after no-lane:<its repo>` and the parent's brief
    and model. Then `chief_ops.py drain` finds the next item with no judgment of its own.
    """
    repo, local = split_ref(ref)
    if status == QueueStatus.WAITING_ROBERTO:
        _require_question(doc, ref)
    child_refs = _child_refs(doc, f"{repo}{local}", children) if children is not None else []
    row: Dict[str, Any] = {"repo": repo, "ref": local, "title": title, "status": status}
    _apply_kind(row, status, kind)
    if note:
        row["note"] = note
    _set_model(row, model)
    _set_dispatch_fields(row, brief, after)
    position = len(doc["queue"]) if at is None else _position(at, len(doc["queue"]) + 1)
    doc["queue"].insert(position, row)
    for offset, child in enumerate(child_refs, start=1):
        child_repo, child_local = split_ref(child)
        entry: Dict[str, Any] = {
            "repo": child_repo, "ref": child_local, "title": short_text(f"{title}: {child}"),
            "status": QueueStatus.QUEUED, "note": f"child of {repo}{local}",
        }
        _set_model(entry, model)
        _set_dispatch_fields(entry, brief, f"no-lane:{child_repo}")
        doc["queue"].insert(position + offset, entry)


def set_item(doc: Dict[str, Any], ref: str, status: Optional[str] = None,
             note: Optional[str] = None, title: Optional[str] = None,
             model: Optional[str] = None, kind: Optional[str] = None,
             brief: Optional[str] = None, after: Optional[str] = None) -> None:
    """Change an item in place; an empty `note`, `model`, `brief` or `after` clears it.

    Setting `status` to `parked` needs a kind; a row already parked keeps the one
    it has, which lets a pre-kind row be edited until it is migrated.
    """
    if all(value is None for value in (status, note, title, model, kind, brief, after)):
        raise ValueError("nothing to set: pass --status, --note, --title, --model, --kind, --brief or --after")
    row = doc["queue"][_index(doc, ref)]
    if status == QueueStatus.WAITING_ROBERTO:
        _require_question(doc, ref)
    if status == QueueStatus.PARKED or kind or (status is not None and row["status"] == QueueStatus.PARKED):
        _apply_kind(row, status or row["status"], kind)
    _set_model(row, model)
    _set_dispatch_fields(row, brief, after)
    if status is not None:
        row["status"] = status
    if title is not None:
        row["title"] = title
    if note is not None:
        if note:
            row["note"] = note
        else:
            row.pop("note", None)


def _position(at: int, slots: int) -> int:
    if not 1 <= at <= slots:
        raise ValueError(f"position must be 1..{slots}, got {at}")
    return at - 1


def move_item(doc: Dict[str, Any], ref: str, to: int) -> None:
    """Move an item to 1-based position `to`."""
    row = doc["queue"].pop(_index(doc, ref))
    doc["queue"].insert(_position(to, len(doc["queue"]) + 1), row)


def remove_item(doc: Dict[str, Any], ref: str) -> None:
    doc["queue"].pop(_index(doc, ref))


def prune_items(doc: Dict[str, Any], statuses: Sequence[str] = (QueueStatus.MERGED,)) -> int:
    """Drop every queue row whose status is in `statuses`; returns how many went.

    Lanes and waiting items are never touched: a lane's `item` is only a `#N`
    pointer, and a question outlives the row it was asked about.
    """
    unknown = sorted(set(statuses) - set(QUEUE_STATUSES))
    if unknown:
        raise ValueError(f"unknown status {unknown[0]!r}; use one of {', '.join(QUEUE_STATUSES)}")
    kept = [row for row in doc["queue"] if row["status"] not in statuses]
    pruned = len(doc["queue"]) - len(kept)
    doc["queue"][:] = kept
    return pruned


def prune_plan_file(path: Path, statuses: Sequence[str] = (QueueStatus.MERGED,)) -> int:
    """`prune_items` through the locked read-modify-write; the in-process entry for the app.

    Returns the number of rows removed. A plan this writer cannot parse raises
    ValueError rather than being overwritten, like every other verb.
    """
    pruned: List[int] = []
    update(path, lambda doc: pruned.append(prune_items(doc, statuses)))
    return pruned[0]


def _next_id(doc: Dict[str, Any]) -> str:
    """`q<N>`, one past the highest auto id in use, so a cleared id is not reissued
    while a later one is still waiting (an answer for it can't land on a new item)."""
    taken = [int(m.group(1)) for row in doc["waiting_on_roberto"]
             if (m := _AUTO_ID.fullmatch(str(row.get("id", ""))))]
    return f"q{max(taken, default=0) + 1}"


def add_waiting(doc: Dict[str, Any], text: str, ref: Optional[str] = None) -> str:
    """`plan wait`: the text becomes an answerable question; returns its id.

    A plain waiting line had nothing for Roberto to answer on the Board's sheet,
    so `wait` is now the shortest `plan ask`. Prefer `ask` with
    `--detail` and `--recommend` for a real decision.
    """
    return add_question(doc, text, ref=ref)


def parse_option(spec: str) -> Dict[str, Any]:
    """`"Label::description"` -> an option; no `::` means a bare label."""
    label, _, description = spec.partition("::")
    option: Dict[str, Any] = {"label": label.strip()}
    if not option["label"]:
        raise ValueError(f"--option needs a label before '::', got {spec!r}")
    if description.strip():
        option["description"] = description.strip()
    return option


def short_text(question: str) -> str:
    """The card line for a question: whitespace collapsed, cut to MAX_TEXT."""
    text = " ".join(question.split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1].rstrip() + "\u2026"


def add_question(
    doc: Dict[str, Any],
    question: str,
    *,
    repo: Optional[str] = None,
    ref: Optional[str] = None,
    detail: Optional[str] = None,
    recommendation: Optional[str] = None,
    options: Optional[List[str]] = None,
    recommended: Optional[List[str]] = None,
    multi: bool = False,
    item_id: Optional[str] = None,
    text: Optional[str] = None,
) -> str:
    """Append a structured question for the Board's answer sheet; returns its id.

    `options` are `"Label::description"` specs; `recommended` names option
    labels to mark. `repo` is derived from `ref` when omitted. The shape rules
    (option count, one recommendation unless `multi`, repo/ref agreement,
    unique ids) are `validate()`'s, so they hold for every writer.
    """
    # `asked_at` is the date the Board's "Waiting for start" shows beside the question.
    row: Dict[str, Any] = {"id": item_id or _next_id(doc),
                           "text": short_text(question) if text is None else text,
                           "asked_at": _iso_z(_now())[:10]}
    if ref:
        row["repo"] = repo or split_ref(ref)[0]
        row["ref"] = ref.strip()
    elif repo:
        row["repo"] = repo
    row["question"] = question.strip()
    if detail:
        row["detail"] = detail.strip()
    if recommendation:
        row["recommendation"] = recommendation.strip()
    parsed = [parse_option(spec) for spec in options or []]
    for label in recommended or []:
        match = [opt for opt in parsed if opt["label"] == label.strip()]
        if not match:
            raise ValueError(f"--recommended {label!r} is not one of the --option labels")
        match[0]["recommended"] = True
    if parsed:
        row["options"] = parsed
    if multi:
        row["multi"] = True
    doc["waiting_on_roberto"].append(row)
    return row["id"]


def clear_waiting(doc: Dict[str, Any], key: str) -> None:
    """Drop a waiting item by 1-based index, by its id, by its `repo#N` ref, or by exact text."""
    rows = doc["waiting_on_roberto"]
    if key.isdigit():
        rows.pop(_position(int(key), len(rows)))
        return
    kept = [row for row in rows if key not in (row.get("id"), row.get("ref"), row["text"])]
    if len(kept) == len(rows):
        raise ValueError(f"nothing waiting matches {key!r}")
    doc["waiting_on_roberto"] = kept
