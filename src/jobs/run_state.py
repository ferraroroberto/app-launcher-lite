"""The Jobs run lifecycle, declared once.

A run's mode is stored as a free-text ``status`` string in ``run.json`` plus the
flags ``killed``, ``reaped``, ``dry_run`` and the ``pid`` / ``pid_create_time``
pair. Every reader used to decide on its own which strings mean "live"; this
module is the only code that maps those facts to a mode. ``run.json`` keeps
writing the same ``status`` strings (the :data:`STATUS_*` constants below), so
history and the sqlite index need no migration. See ``docs/run-state.md``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Mapping, NamedTuple, Optional

# The strings run.json stores in ``status``. Writers use these names so no other
# module spells a lifecycle string.
STATUS_QUEUED = "queued"
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_DRY_RUN_SUCCESS = "dry_run_success"
STATUS_DRY_RUN_FAILED = "dry_run_failed"

# A pending/running record with no pid this long after it started never got its
# child spawned: the pid is written right after Popen, so seconds is normal and
# two minutes is an executor that died first.
SPAWN_DEADLINE_SECONDS = 120.0

# ``fail_reason`` stamped by the reaper when it finalises a never-started run.
REASON_NEVER_STARTED = "executor_never_started"


class RunState(str, Enum):
    """Every mode a run can be in. The set is closed."""

    QUEUED = "queued"  # waiting behind its mutex group; no executor yet
    PENDING = "pending"  # fired, executor spawned, child not started
    RUNNING = "running"  # executor wrote the running record; child may be alive
    SUCCEEDED = "succeeded"  # child exited 0
    FAILED = "failed"  # finished without success; see FailReason
    KILLED = "killed"  # an operator stopped it
    REAPED = "reaped"  # the executor/child died and a later sweep closed the record
    SKIPPED = "skipped"  # cooldown no-op, never executed
    DRY_RUN_SUCCEEDED = "dry_run_succeeded"  # dry-run check passed, nothing executed
    DRY_RUN_FAILED = "dry_run_failed"  # dry-run check failed, nothing executed
    UNKNOWN = "unknown"  # the record's facts do not name a mode


class FailReason(str, Enum):
    """Why a run is ``FAILED`` (or, for ``REAPED``, what was lost)."""

    EXIT_CODE = "exit_code"  # the child exited non-zero
    WATCHDOG = "watchdog"  # the executor's last-resort watchdog tore the tree down
    SPAWN_ERROR = "spawn_error"  # the child never started (exit -1)
    EXECUTOR_NEVER_STARTED = REASON_NEVER_STARTED  # live record, no pid past the deadline
    EXECUTOR_LOST = "executor_lost"  # live record whose pid is provably dead


class Derived(NamedTuple):
    """One record's derived mode and, for a failure, its reason."""

    state: RunState
    reason: Optional[FailReason] = None


_LIVE = frozenset({RunState.PENDING, RunState.RUNNING})
_OPEN = _LIVE | {RunState.QUEUED}
_FAILURES = frozenset({RunState.FAILED, RunState.KILLED, RunState.REAPED})
# Records that are not real fires: they must not anchor a cooldown window.
_NOT_A_FIRE = frozenset(
    {RunState.SKIPPED, RunState.DRY_RUN_SUCCEEDED, RunState.DRY_RUN_FAILED}
)

_BY_STATUS = {
    STATUS_QUEUED: RunState.QUEUED,
    STATUS_PENDING: RunState.PENDING,
    STATUS_RUNNING: RunState.RUNNING,
    STATUS_SKIPPED: RunState.SKIPPED,
    STATUS_DRY_RUN_SUCCESS: RunState.DRY_RUN_SUCCEEDED,
    STATUS_DRY_RUN_FAILED: RunState.DRY_RUN_FAILED,
}


def is_live(state: RunState) -> bool:
    """An executor owns the run (pending or running): it holds its mutex group,
    the UI shows it as running now, and Kill may act on it."""
    return state in _LIVE


def is_open(state: RunState) -> bool:
    """Not finished yet: queued behind a mutex group, or live."""
    return state in _OPEN


def is_killable(state: RunState) -> bool:
    """Kill may act on it: exactly the live states."""
    return state in _LIVE


def is_failure(state: RunState) -> bool:
    """Finished unsuccessfully (failed, killed or reaped): all are ``status: failed``."""
    return state in _FAILURES


def is_real_fire(state: RunState) -> bool:
    """A run that executed (or tried to), as opposed to a skip or a dry-run check."""
    return state not in _NOT_A_FIRE


def is_executor_owned(record: Mapping[str, Any]) -> bool:
    """The record still says an executor owns the run (pending or running) and no
    Kill or reap has closed it on disk: the only records a sweep may finalise."""
    if record.get("killed") or record.get("reaped"):
        return False
    status = record.get("status")
    return isinstance(status, str) and _BY_STATUS.get(status) in _LIVE


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _has_pid(record: Mapping[str, Any]) -> bool:
    pid = record.get("pid")
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


def _failed_reason(record: Mapping[str, Any]) -> FailReason:
    if record.get("watchdog"):
        return FailReason.WATCHDOG
    if record.get("fail_reason") == REASON_NEVER_STARTED:
        return FailReason.EXECUTOR_NEVER_STARTED
    if record.get("exit_code") == -1:
        return FailReason.SPAWN_ERROR
    return FailReason.EXIT_CODE


def derive_run_state(
    record: Mapping[str, Any],
    *,
    now: datetime,
    pid_alive: Optional[bool] = None,
) -> Derived:
    """Map one ``run.json`` record to its state.

    ``pid_alive`` is the caller's liveness probe of ``record['pid']``: ``True``
    alive, ``False`` provably dead, ``None`` not probed or not provable (a read
    path that must stay cheap passes ``None``, and the record is taken at its
    word). The function never reads a clock or a process itself.
    """
    if record.get("killed"):
        return Derived(RunState.KILLED)
    if record.get("reaped"):
        return Derived(RunState.REAPED, FailReason.EXECUTOR_LOST)
    status = record.get("status")
    if not isinstance(status, str):
        return Derived(RunState.UNKNOWN)
    if status == STATUS_SUCCESS:
        return Derived(RunState.SUCCEEDED)
    if status == STATUS_FAILED:
        return Derived(RunState.FAILED, _failed_reason(record))
    state = _BY_STATUS.get(status)
    if state is None:
        return Derived(RunState.UNKNOWN)
    if state not in _LIVE:
        return Derived(state)
    if _has_pid(record):
        if pid_alive is False:
            return Derived(RunState.REAPED, FailReason.EXECUTOR_LOST)
        return Derived(state)
    started = _parse_iso(record.get("started_at"))
    if started is not None:
        try:
            age = (now - started).total_seconds()
        except TypeError:  # naive vs aware: the age cannot be told
            return Derived(state)
        if age > SPAWN_DEADLINE_SECONDS:
            return Derived(RunState.FAILED, FailReason.EXECUTOR_NEVER_STARTED)
    return Derived(state)


def run_view(record: Mapping[str, Any], *, now: datetime) -> Dict[str, Any]:
    """The client-facing slice of a record's state: what the Jobs tab reads
    instead of comparing ``status`` strings. ``live`` is :func:`is_live`; ``open``
    is :func:`is_open` (the output stream stays attached while a run is queued)."""
    derived = derive_run_state(record, now=now)
    return {
        "state": derived.state.value,
        "live": is_live(derived.state),
        "open": is_open(derived.state),
        "fail_reason": derived.reason.value if derived.reason else None,
    }
