"""The chief's mechanical safety rails over `/api/board` cards: who occupies
which repo, how many workers are alive, the dispatch refusals, and the
fleet-wide-job guard. Pure: no network, no `gh`; unit-tested without either.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Collection, Dict, List, Mapping, Optional, Tuple

from fleet_core import fleet_repo_scan
from owner_check import block as owner_block

# Worker cap used when the launcher's /api/board/chief/settings carries no
# `worker_cap`. The 3 is the default the /chief SKILL.md documents ("default
# cap 3") and has been in this helper since it was extracted. An
# explicit 0 is a real setting and fails closed, so this applies only
# to an absent value.
DEFAULT_WORKER_CAP = 3

# What the cap counts, from the launcher's `worker_cap_scope` setting: `chief`
# only the sessions the chief dispatched (so a session Roberto opened himself
# never takes a lane slot), `all` every live session. An absent value is
# `chief`; any other value is an unreadable setting, never silently either.
WORKER_CAP_SCOPES = ("chief", "all")
DEFAULT_WORKER_CAP_SCOPE = "chief"


def resolve_cap_scope(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """`(scope, error)` for the settings' `worker_cap_scope`: one is always None."""
    if raw is None:
        return DEFAULT_WORKER_CAP_SCOPE, None
    if raw in WORKER_CAP_SCOPES:
        return str(raw), None
    return None, f"worker_cap_scope {raw!r} is not one of {'/'.join(WORKER_CAP_SCOPES)}"


# App Launcher stamps its own session id into every session it spawns
# (`session_host.py`'s `agent_child_env`, the same `uuid4().hex` the board
# card reports as `session_id`). Named here rather than imported: `hooks/` and
# `skills/_lib/` are independent trees by convention, and `worktree_claim.py`
# already reads the same variable the same way.
LAUNCHER_SESSION_ID_ENV_VAR = "APP_LAUNCHER_SESSION_ID"

# Board-label prefix of the standing Telegram channel sessions (app-launcher's
# `channel_profiles.py`). Named here, not imported, for the same reason.
CHANNEL_SESSION_LABEL_PREFIX = "telegram:"

# Scheduled launcher jobs whose scope is the whole fleet.
# Every fleet repo is in their scope, so any dispatch while one runs can
# collide with it: a `/propagate-vendored` lane's extra worktrees halt a running
# cleanup-fleet-all partway through its issues.
FLEET_JOB_PATTERNS = (
    "cleanup-fleet-*",
    "codebase-audit-fleet",
    "design-sweep-fleet",
    "prompt-audit-fleet",
)
# A brief that itself fans out across the fleet.
FLEET_WIDE_BRIEF_RE = re.compile(r"/(?:propagate-vendored|cleanup-fleet)\b")

# ---- pure decision logic (unit-tested without network/gh) ------------------

def caller_session_id(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """This process's own App Launcher session id, or None outside one.

    The launcher generates a session's id as `uuid4().hex` and stamps that
    exact value into the spawned process's environment, so a tool running
    *inside* a launcher session can name its own board card without being
    told which one it is — the same id space the card's `session_id` field
    reports, and the same one `hooks/_lib.launcher_session_id()` resolves
    identity from.

    Deliberately not the card's `label`: `label == "chief"` is assigned by
    the launcher's chief-spawn path, but `/api/board` also re-derives it on
    every poll (`app/webapp/routers/board_chief.py`'s `_reconcile_chief_label`,
    called from `board.py`) for any live PTY cwd'd in the fleet-config
    checkout whose OSC title's last token, hook-state shared name, or first
    submitted prompt reads "chief" — a deliberate self-heal for a chief
    re-attached by Resume, and three signals an ordinary worker in this repo
    can trip. A worker mislabelled that way would be excluded from occupancy,
    the exact collision the guard exists to prevent, so the exclusion below
    rests on the primary key instead.
    """
    source = os.environ if env is None else env
    value = str(source.get(LAUNCHER_SESSION_ID_ENV_VAR) or "").strip()
    return value or None


def _holds_a_live_pty(card: Dict[str, Any]) -> bool:
    """The one liveness test both dispatch gates share: alive, and not an
    `external` (state-file-only, unverifiable) card. Kept in one place so the
    occupancy gate and the worker cap can't disagree about whether a card is
    real — they once did, and an alive external card read "repo free" and
    "cap full" in the same dispatch."""
    return bool(card.get("alive")) and card.get("kind") != "external"


def _is_channel_session(card: Dict[str, Any]) -> bool:
    """A standing Telegram channel session: the launcher's session-host tags
    each with a `telegram:<profile>` board label (app-launcher
    `channel_profiles.py`). Always-on by design, never a
    worker: it does not claim a checkout, and a dispatched lane builds in its
    own worktree regardless. Shared by the occupancy gate and the worker cap so
    the two can't disagree about it."""
    return str(card.get("label") or "").startswith(CHANNEL_SESSION_LABEL_PREFIX)


def repo_occupancy(
    columns: Dict[str, Any],
    exclude_sid: Optional[str] = None,
    hide_chief: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Lowercased repo -> occupancy info, for every alive, non-external
    session card in `claude_turn` + `your_turn`.

    A dead (`alive: False`) or `external` (state-file-only, unverifiable)
    card never blocks a dispatch — only a live PTY actually holds the repo.

    A standing `telegram:*` channel session (`_is_channel_session`) is skipped
    too: three of them run permanently in life-os, so counting them made
    every life-os dispatch refuse.

    `exclude_sid` drops one card by session id: the caller's own. The standing
    chief runs as an ordinary launcher PTY with cwd `E:/automation/fleet-config`,
    so counting its card made this repo permanently occupied and chief could
    never dispatch its own queue. Chief is not a worker — it
    never runs `git`, never edits files, never claims a checkout — so its cwd
    is where it happens to run, not a claim on the tree. Excluding *the caller*
    rather than "the chief" keeps the rule one a card cannot lie its way out
    of: a session does not occupy a repo against itself, and every other live
    session still does.

    `hide_chief` additionally drops the standing chief's card for every caller.
    Only the read-only `sessions` view sets it: a lane told to skip busy repos
    otherwise saw the chief's cwd as a worker holding fleet-config. `dispatch`
    leaves it off, so its occupancy gate is unchanged.
    """
    occ: Dict[str, Dict[str, Any]] = {}
    cards = list(columns.get("claude_turn") or []) + list(columns.get("your_turn") or [])
    for card in cards:
        if not _holds_a_live_pty(card) or _is_channel_session(card):
            continue
        if exclude_sid and str(card.get("session_id") or "") == exclude_sid:
            continue
        if hide_chief and card.get("label") == "chief":
            continue
        repo = str(card.get("project") or "").strip().lower()
        if not repo:
            continue
        occ[repo] = {
            "session_id": card.get("session_id"),
            "status": card.get("status"),
            "age_seconds": card.get("age_seconds"),
            "agent": card.get("agent"),
            "label": card.get("label"),
        }
    return occ


def alive_worker_count(
    columns: Dict[str, Any],
    exclude_sid: Optional[str] = None,
    managed_sids: Optional[Collection[str]] = None,
) -> int:
    """Alive, non-external session cards, excluding the caller's own and the
    standing chief's — the same liveness test as `repo_occupancy`
    (`_holds_a_live_pty`).

    `exclude_sid` drops the caller's own card here too:
    the count and the occupancy map are read from the same card list in the
    same dispatch, and a caller that excused itself from one but not the other
    would still be capped out by its own presence. The `label` test stays as
    the fallback for a chief that is not itself the caller.

    It excuses *any* caller, not only chief, which is the honest reading of
    "a session is not one of the workers it is counting" — and safe past
    chief because a launcher-dispatched session is forced onto a worktree
    regardless (`worktree_claim.py`), so the checkout
    collision the cap is a proxy for is prevented downstream either way.

    Standing `telegram:*` channel sessions are not workers either
    (`_is_channel_session`): three always-on ones would
    otherwise hold three lane slots forever.

    `managed_sids` is the `chief` cap scope: only cards whose session id the
    chief recorded at dispatch count, so a session the owner opened himself
    holds no slot. `None` is the `all` scope, every card above.
    """
    cards = list(columns.get("claude_turn") or []) + list(columns.get("your_turn") or [])
    return sum(
        1
        for c in cards
        if _holds_a_live_pty(c)
        and not _is_channel_session(c)
        and c.get("label") != "chief"
        and not (exclude_sid and str(c.get("session_id") or "") == exclude_sid)
        and (managed_sids is None or str(c.get("session_id") or "") in managed_sids)
    )


def find_chief_session(columns: Dict[str, Any]) -> Optional[str]:
    """The live standing chief's session id, or None.

    Unlike `repo_occupancy` (deduped by repo, for dispatch gating), this
    scans every card without dedup — fleet-config can simultaneously host
    the standing chief *and* an ordinary dev/worker session (both would
    report `project == "fleet-config"`), so only the `label == "chief"`
    card is actually chief. Used by `chief-sid` so `hooks/notify_on_idle.py`
    can push a chief-managed worker's blocked-on-input notification into
    chief's own session instead of the human ping.
    """
    cards = list(columns.get("claude_turn") or []) + list(columns.get("your_turn") or [])
    for card in cards:
        if card.get("alive") and card.get("label") == "chief":
            sid = card.get("session_id")
            return str(sid) if sid else None
    return None


def resolve_session_id(sid: str, columns: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Resolve `sid` against the full ids of every live session card the
    board currently reports, so `exchange` accepts the 8-char prefix
    `board`/`sessions` actually print instead of requiring the full
    32-character id neither of those commands ever emits.

    Returns `(resolved_id, reason)`:
      - an exact match, or an unambiguous prefix match -> `(full_id, None)`.
      - two or more live ids share `sid` as a prefix -> `(None, reason)`, a
        state distinct from a genuine 404 -- callers must never silently
        pick one.
      - no live id matches (a dead session's full id, or a prefix whose
        session has since ended) -> `(sid, None)` unchanged, so the caller
        still queries the real endpoint and gets the real
        `session_not_found` -- this path must not regress.
    """
    live_ids = sorted({
        str(card["session_id"])
        for bucket in (columns.get("claude_turn"), columns.get("your_turn"), columns.get("other"))
        for card in (bucket or [])
        if card.get("alive") and card.get("session_id")
    })
    if sid in live_ids:
        return sid, None
    matches = [full for full in live_ids if full.startswith(sid)]
    if len(matches) >= 2:
        return None, f"prefix {sid!r} matches {len(matches)} live sessions: {', '.join(matches)}"
    if len(matches) == 1:
        return matches[0], None
    return sid, None



def find_session_status(columns: Dict[str, Any], sid: str) -> Optional[str]:
    """Status of the live session card matching `sid` (any column), or None
    if no card matches. Used to tell a busy target from a stuck one before
    calling a non-advancing exchange "stranded"."""
    for bucket in (columns.get("claude_turn"), columns.get("your_turn"), columns.get("other")):
        for card in bucket or []:
            if str(card.get("session_id") or "") == sid:
                return card.get("status")
    return None


def session_holds_live_pty(columns: Dict[str, Any], sid: str) -> bool:
    """True when a card for `sid` in any column still holds a live PTY."""
    for bucket in (columns.get("claude_turn"), columns.get("your_turn"), columns.get("other")):
        for card in bucket or []:
            if str(card.get("session_id") or "") == sid and _holds_a_live_pty(card):
                return True
    return False


def refuse_dispatch(
    repo: str,
    mode: str,
    occupancy: Dict[str, Dict[str, Any]],
    alive_count: int,
    worker_cap: int,
    yolo_confirmed: bool,
) -> Optional[str]:
    """The three mechanical refusals. Returns a reason string, or None to
    proceed. Checked in this order: occupied repo, worker cap, unconfirmed
    yolo — any one of them is reason enough to refuse before the others are
    even relevant."""
    repo_key = repo.strip().lower()
    held = occupancy.get(repo_key)
    if held is not None:
        return (
            f"repo already has a live session: {held.get('session_id')} "
            f"({held.get('status')}, agent={held.get('agent')})"
        )
    if alive_count >= worker_cap:
        return f"at/over worker cap: {alive_count}/{worker_cap} alive"
    if mode == "yolo" and not yolo_confirmed:
        return "yolo mode requires --yolo-confirmed"
    return None


def running_fleet_jobs(jobs_payload: Dict[str, Any]) -> List[str]:
    """Pure: ids of the running jobs in an `/api/jobs` payload that are fleet-wide."""
    return [
        str(job.get("id"))
        for job in jobs_payload.get("jobs") or []
        if job.get("running") is True
        and any(fnmatch.fnmatchcase(str(job.get("id")), pat) for pat in FLEET_JOB_PATTERNS)
    ]


def is_fleet_wide_brief(brief: Optional[str]) -> bool:
    return bool(brief) and FLEET_WIDE_BRIEF_RE.search(brief) is not None



def fleet_job_refusal(
    running: Optional[List[str]], error: Optional[str], fleet_wide_brief: bool, allowed: bool,
) -> Optional[str]:
    """Refuse a fleet-wide brief while a fleet-wide job runs, or while that
    can't be ruled out. A per-issue lane is only warned, never refused."""
    if not fleet_wide_brief or allowed or running == []:
        return None
    blocker = (f"fleet job {', '.join(running)} is running" if running
               else f"fleet-job state unknown ({error})")
    return f"fleet-wide brief while {blocker}; wait for it to finish, or pass --allow-during-fleet-job"


def owner_check_refusal(repo: str, number: int, issue: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """`(refusal, warning)` from an issue-state row (`launcher.fetch_issue_state`).

    A `needs-owner` issue waits on a step of Roberto's, so it is refused. A row
    that could not be read is only warned: `/issue-start`'s owner gate is the
    hard stop and refuses an unknown state there.
    """
    if "error" in issue:
        return None, f"owner-check-unknown reason={issue['error']}"
    labels = {str(lab.get("name")) for lab in issue.get("labels") or []}
    if owner_block.LABELS[owner_block.BEFORE_START] in labels:
        return (f"needs-owner: {repo}#{number} waits on a step of Roberto's; "
                f"owner_check gate {repo}#{number} prints the reason"), None
    return None, None


def resolve_repo_path(repo: str, repos: Optional[Dict[str, Any]] = None) -> Path:
    """Resolve `repo` to a directory: a literal existing path first, else a
    fleet-registry name.

    `repos` is injectable (`{name: path}`) so this stays unit-testable
    without touching the real `hooks/projects.toml`; the CLI passes
    `fleet_repo_scan.fleet_repos(include_ignored=True)` when omitted (a named
    repo resolves even when it is switched out of fleet runs). Case-insensitive name
    match, mirroring `worktree_claim.py`'s tolerant path/name resolution.
    """
    candidate = Path(repo)
    if candidate.is_dir():
        return candidate.resolve()
    table = repos if repos is not None else fleet_repo_scan.fleet_repos(include_ignored=True)
    if repo in table:
        return Path(table[repo]).resolve()
    lower = repo.lower()
    for name, path in table.items():
        if name.lower() == lower:
            return Path(path).resolve()
    raise ValueError(f"unknown fleet repo: {repo!r}")


def parse_issue_ref(ref: str) -> Tuple[str, int]:
    repo, sep, num = ref.partition("#")
    if not sep or not repo.strip() or not num.strip():
        raise ValueError(f"expected <repo>#<number>, got: {ref!r}")
    return repo.strip(), int(num.strip())
