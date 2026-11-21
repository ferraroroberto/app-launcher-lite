"""`chief_ops.py rerun-job`: re-run a failed Job once, only when its owner said that is safe.

A failed Job run reaches the chief as a `job-failed` event. This verb is the
chief's one guarded answer to it: it fires `POST /api/jobs/<id>/run` for the
failed run's job when every fact below is established, and prints one line:

  RERUN=started   the launcher started a new run (`rerun=<run id>`).
  RERUN_REFUSED   a fact says no, and `reason=` names it (exit 2):
                  `not-rerun-safe`, `rerun-safe-absent`, `rerun-safe-not-boolean`,
                  `job-not-found`, `run-not-failed`, `already-rerun`, `is-a-rerun`.
  RERUN=unknown   a fact could not be read: the jobs list, the failed run's
                  record or the ledger (exit 1). Never folded into a pass.
  RERUN=unconfirmed  the POST got no answer; the launcher may have started the
                  run, so the one re-run stays used and nothing is re-posted.

`rerun_safe` is the owner's statement, set in `config/jobs.json` or by `PUT
/api/jobs/<id>`; a literal `true` is the only value that passes. The verb never
sends `confirmed=1` and never writes a job definition.

Once per failure: the ledger `chief-job-rerun.json` in the hooks state dir
keeps one entry per failed run, written before the POST so a lost answer still
spends the re-run. A run the verb started is recorded too, so its own failure is
refused (`is-a-rerun`) and the chief never loops. The ledger holds ids only,
never job output.
"""

from __future__ import annotations

import argparse
import json
import logging
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from active_issue import _iso_z, _now, state_lock, write_rows
from fleet_core.hooks_state import state_dir

from . import launcher

logger = logging.getLogger(__name__)

LEDGER_FILENAME = "chief-job-rerun.json"

# Written before the POST: a re-run whose answer never arrived still uses up the
# one allowed re-run, because the launcher may have started it.
IN_FLIGHT = "in-flight"


def ledger_file() -> Path:
    return state_dir() / LEDGER_FILENAME


def _key(job_id: str, run_id: str) -> str:
    return f"{job_id}/{run_id}"


def load_ledger(path: Path) -> Dict[str, Dict[str, Any]]:
    """`{job/run: {rerun_run_id, at}}`; a missing file is empty, an unparseable one raises ValueError."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path.name} unreadable: {exc}") from exc
    runs = doc.get("runs") if isinstance(doc, dict) else None
    if not isinstance(runs, dict):
        raise ValueError(f"{path.name} has no runs mapping")
    return runs


def ledger_refusal(runs: Dict[str, Dict[str, Any]], job_id: str, run_id: str) -> Optional[str]:
    """The refusal reason the ledger alone gives, else None."""
    entry = runs.get(_key(job_id, run_id))
    if entry is not None:
        return f"already-rerun rerun={entry.get('rerun_run_id') or IN_FLIGHT}"
    if any(e.get("rerun_run_id") == run_id for k, e in runs.items() if k.startswith(f"{job_id}/")):
        return "is-a-rerun"
    return None


def job_rerun_safe(jobs_reply: Any, job_id: str) -> Tuple[Optional[bool], str]:
    """`(True, "")` only for a job whose `rerun_safe` is literally true; `(None, why)` when unreadable."""
    jobs = jobs_reply.get("jobs") if isinstance(jobs_reply, dict) else None
    if not isinstance(jobs, list):
        return None, "jobs-unreadable"
    job = next((j for j in jobs if isinstance(j, dict) and j.get("id") == job_id), None)
    if job is None:
        return False, "job-not-found"
    if "rerun_safe" not in job:
        return False, "rerun-safe-absent"
    if job["rerun_safe"] is True:
        return True, ""
    return False, "not-rerun-safe" if job["rerun_safe"] is False else "rerun-safe-not-boolean"


def run_failed(run_reply: Any) -> Tuple[Optional[bool], str]:
    """`(True, "")` for a run record that says `failed`; `(None, why)` when it cannot be read."""
    record = run_reply.get("run") if isinstance(run_reply, dict) else None
    status = record.get("status") if isinstance(record, dict) else None
    if not isinstance(status, str) or not status:
        return None, "run-record-unreadable"
    # state-literal-exempt: the launcher's run status read over HTTP; copilot/ cannot import src.jobs.run_status and never moves it
    return status == "failed", f"run-not-failed status={status}"


def _claim(path: Path, job_id: str, run_id: str) -> bool:
    """Spend the failed run's one re-run; False when another call already did."""
    with state_lock(path):
        runs = load_ledger(path)
        if ledger_refusal(runs, job_id, run_id) is not None:
            return False
        runs[_key(job_id, run_id)] = {"rerun_run_id": IN_FLIGHT, "at": _iso_z(_now())}
        write_rows(path, {"version": 1, "runs": runs})
    return True


def _record(path: Path, job_id: str, run_id: str, rerun_run_id: str) -> None:
    with state_lock(path):
        runs = load_ledger(path)
        runs[_key(job_id, run_id)] = {"rerun_run_id": rerun_run_id, "at": _iso_z(_now())}
        write_rows(path, {"version": 1, "runs": runs})


def _refusal_line(job_id: str, reason: str) -> str:
    return f"RERUN_REFUSED={job_id} reason={reason}"


def _precheck(args: argparse.Namespace, path: Path) -> Optional[Tuple[str, int]]:
    """`(line, exit code)` when the re-run must not fire, else None. Cheapest facts first."""
    job = args.job_id
    try:
        refusal = ledger_refusal(load_ledger(path), job, args.run)
    except ValueError as exc:
        return f"RERUN=unknown job={job} reason=ledger ({exc})", 1
    if refusal is not None:
        return _refusal_line(job, refusal), 2
    quoted = urllib.parse.quote(job, safe="")
    try:
        jobs_reply = launcher._request(args.base_url, "/api/jobs", timeout=launcher.JOBS_TIMEOUT)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return f"RERUN=unknown job={job} reason=jobs-unreadable ({exc})", 1
    safe, why = job_rerun_safe(jobs_reply, job)
    if safe is None:
        return f"RERUN=unknown job={job} reason={why}", 1
    if not safe:
        return _refusal_line(job, why), 2
    try:
        run_reply = launcher._request(args.base_url, f"/api/jobs/{quoted}/runs/{urllib.parse.quote(args.run, safe='')}")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return f"RERUN=unknown job={job} reason=run-record-unreadable ({exc})", 1
    failed, why = run_failed(run_reply)
    if failed is None:
        return f"RERUN=unknown job={job} reason={why}", 1
    if not failed:
        return _refusal_line(job, why), 2
    return None


def cmd_rerun(args: argparse.Namespace) -> int:
    path = ledger_file()
    stop = _precheck(args, path)
    if stop is not None:
        print(stop[0])
        return stop[1]
    try:
        claimed = _claim(path, args.job_id, args.run)
    except (OSError, ValueError) as exc:
        print(f"RERUN=unknown job={args.job_id} reason=ledger ({exc})")
        return 1
    if not claimed:
        print(_refusal_line(args.job_id, "already-rerun"))
        return 2
    post = f"/api/jobs/{urllib.parse.quote(args.job_id, safe='')}/run"
    try:
        reply = launcher._request(args.base_url, post, method="POST", body={})
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        # The claim stays: a POST with no answer may have started the run.
        print(f"RERUN=unconfirmed job={args.job_id} reason=run-post ({exc}) -> check the Jobs tab; no second re-run")
        return 1
    new_run = reply.get("run_id") if isinstance(reply, dict) else None
    if not new_run:
        print(f"RERUN=unconfirmed job={args.job_id} reason=no-run-id -> check the Jobs tab; no second re-run")
        return 1
    try:
        _record(path, args.job_id, args.run, str(new_run))
    except (OSError, ValueError) as exc:
        # The in-flight claim already blocks a second re-run of this failure; only the
        # `is-a-rerun` guard on the new run is lost, so say so.
        logger.warning("⚠️ chief rerun-job: re-run %s started but not recorded (%s)", new_run, exc)
    print(f"RERUN=started job={args.job_id} failed_run={args.run} rerun={new_run}")
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    r = sub.add_parser("rerun-job", help="re-run a failed Job once, when its rerun_safe flag is true")
    r.add_argument("job_id", help="the failed run's job id (the event's job_id)")
    r.add_argument("run", help="the failed run's id (the event's run_id)")
    r.add_argument("--base-url", default=launcher.DEFAULT_BASE_URL)
    r.set_defaults(func=cmd_rerun)
