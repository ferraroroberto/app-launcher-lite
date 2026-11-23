"""Copilot's plan usage this month, and this machine's share of it.

**The plan** (the card's bar, percent and pace) comes from the same place as
the Copilot CLI's own "Plan: used/allowance" status line: GitHub's Copilot
user response, which the CLI fetches with its own sign-in and caches in
``%LOCALAPPDATA%/copilot/copilot-user-cache.json``. Its
``quota_snapshots.premium_interactions`` entry holds the plan-wide
``entitlement`` and ``quota_remaining`` (used is the difference), every
machine and VS Code included, and the response names the next reset in
``quota_reset_date_utc``. The CLI's status line renders that same
``premium_interactions`` quota for premium-request plans and AI-credit plans
alike; the quota's ``token_based_billing`` says which unit it counts. Reading
that file needs no credential of ours. A cache that is missing, unparseable,
unlimited, silent on its billing unit or older than the last reset reads as
``unknown`` with the reason, so the card shows no percent rather than passing
this machine's share off as the plan's.

**Which entry.** The cache keeps one entry per signed-in account, under a
hashed key, and the status line shows the account the CLI is signed in with.
Telling that account apart would mean reading an account identifier, which
this module never does. So an entry whose cycle has already reset is set
aside (it cannot be this cycle's plan), one remaining entry is the plan, and
several remaining entries read as ``unknown``, because the newest fetch can
belong to another account.

**This machine** (the card's footnote): the Copilot CLI records every model
call it bills in the ``assistant_usage_events`` table of
``~/.copilot/session-store.db``, sub-agents included, with the cost in
``total_nano_aiu`` (one AI credit is 1e9 nano) and a UTC ``created_at``. That
covers this machine's CLI only, which is why the payload names each figure's
scope. Its month starts on the 1st at 00:00 UTC. A log that is missing,
locked or shaped differently reads as ``unknown`` with the reason, never as 0
credits, because a card showing 0 would claim a fact nobody established.

Reads are synchronous and bounded (a 1 s lock timeout); the webapp runs them
in a worker thread, off the request path (``app/webapp/routers/copilot_usage.py``).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TABLE = "assistant_usage_events"
REQUIRED_COLUMNS = frozenset({"total_nano_aiu", "created_at"})
NANO_PER_CREDIT = 1_000_000_000
# A Copilot write holds its lock for milliseconds; a lock held past this is
# reported as unknown rather than waited on.
LOCK_TIMEOUT_S = 1.0
SOURCE = "Copilot CLI usage log (~/.copilot/session-store.db)"
SCOPE = "this machine"
PLAN_SOURCE = "your Copilot plan, as the Copilot CLI last fetched it"
PLAN_SCOPE = "every machine and editor on the plan"
PLAN_QUOTA = "premium_interactions"


def default_db_path() -> Path:
    return Path.home() / ".copilot" / "session-store.db"


def default_plan_cache_path() -> Path:
    """Where the Copilot CLI caches its user response on Windows."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "copilot" / "copilot-user-cache.json"


@dataclass(frozen=True)
class Reading:
    """What this machine's log says, before the clock is applied."""

    state: str  # "ok" | "unknown"
    used_credits: Optional[float] = None
    today_credits: Optional[float] = None
    reason: str = ""


@dataclass(frozen=True)
class PlanReading:
    """The plan's own used and allowance, the figures the CLI's status line shows."""

    state: str  # "ok" | "unknown"
    used_credits: Optional[float] = None
    allowance: Optional[float] = None
    reset_at: Optional[datetime] = None
    as_of: str = ""
    reason: str = ""
    # True for an AI-credit plan, False for a premium-request plan (its figures count requests).
    billed_in_credits: Optional[bool] = None


def billing_month(now: datetime) -> Tuple[datetime, datetime]:
    """The UTC month holding ``now``: its start and the next month's start (the reset)."""
    now = now.astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    reset = (start + timedelta(days=32)).replace(day=1)
    return start, reset


def month_before(moment: datetime) -> datetime:
    """The same day and time one month earlier, clamped to that month's last day."""
    year, month = (moment.year - 1, 12) if moment.month == 1 else (moment.year, moment.month - 1)
    first_of_next = (moment.replace(year=year, month=month, day=1) + timedelta(days=32)).replace(day=1)
    last_day = (first_of_next - timedelta(days=1)).day
    return moment.replace(year=year, month=month, day=min(moment.day, last_day))


def pace_pct(now: datetime, start: datetime, reset: datetime) -> float:
    """The share of the billing month gone, 0 to 100."""
    span = (reset - start).total_seconds()
    gone = (now.astimezone(timezone.utc) - start).total_seconds()
    return round(max(0.0, min(100.0, 100.0 * gone / span)), 1)


def _day_key(moment: datetime) -> str:
    # ``created_at`` is ISO (``2026-11-21T15:18:17.971Z``) or SQLite's own
    # ``2026-11-21 15:18:17``; both sort after a bare date prefix of their day.
    return moment.strftime("%Y-%m-%d")


def _parse_utc(text: Any) -> Optional[datetime]:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read(db_path: Path, now: datetime) -> Reading:
    """Sum this month's and today's credits from the log; never raises."""
    if not db_path.is_file():
        return Reading("unknown", reason="no Copilot usage log on this machine")
    start, reset = billing_month(now)
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        # must-use-exempt: a read-only (mode=ro) open of Copilot's own log; connect_sqlite opens read-write and takes no URI
        # runtime-write-exempt: mode=ro, so nothing is written; the file is Copilot's, under the user's home
        conn = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=LOCK_TIMEOUT_S,
        )
    except sqlite3.Error as exc:
        logger.warning("⚠️ Copilot usage log %s did not open: %s", db_path, exc)
        return Reading("unknown", reason=f"the usage log did not open: {exc}")
    try:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
        if not columns:
            return Reading("unknown", reason=f"the usage log has no {TABLE} table")
        missing = REQUIRED_COLUMNS - columns
        if missing:
            return Reading(
                "unknown", reason=f"the usage log lacks {', '.join(sorted(missing))}",
            )
        month_nano, today_nano = conn.execute(
            f"SELECT COALESCE(SUM(total_nano_aiu), 0),"
            f" COALESCE(SUM(CASE WHEN created_at >= ? THEN total_nano_aiu END), 0)"
            f" FROM {TABLE} WHERE created_at >= ? AND created_at < ?",
            (_day_key(today), _day_key(start), _day_key(reset)),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("⚠️ Copilot usage log %s unreadable: %s", db_path, exc)
        return Reading("unknown", reason=f"the usage log could not be read: {exc}")
    finally:
        conn.close()
    return Reading(
        "ok",
        used_credits=round(month_nano / NANO_PER_CREDIT, 2),
        today_credits=round(today_nano / NANO_PER_CREDIT, 2),
    )


def _cached_plans(entries: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Every cache entry that carries a plan quota, as (response, quota) pairs."""
    plans = []
    for entry in entries.values():
        response = entry.get("response") if isinstance(entry, dict) else None
        snapshots = response.get("quota_snapshots") if isinstance(response, dict) else None
        quota = snapshots.get(PLAN_QUOTA) if isinstance(snapshots, dict) else None
        if isinstance(quota, dict):
            plans.append(({**response, "_retrieved_at": entry.get("retrievedAt")}, quota))
    return plans


def _plan_quota(doc: Any, now: datetime) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], str]:
    """The one cached plan still in its cycle, its response and quota, or a reason there is none."""
    entries = doc.get("copilotUserCache") if isinstance(doc, dict) else None
    if not (isinstance(entries, dict) and entries):
        return None, {}, "the Copilot plan cache holds no plan"
    plans = _cached_plans(entries)
    if not plans:
        return None, {}, f"the Copilot plan cache has no {PLAN_QUOTA} quota"
    utc_now = now.astimezone(timezone.utc)
    current = []
    for response, quota in plans:
        reset = _parse_utc(response.get("quota_reset_date_utc"))
        if reset is None or reset > utc_now:
            current.append((response, quota))
    if not current:
        return None, {}, "the cached plan figure predates the last reset; run Copilot CLI to refresh it"
    if len(current) > 1:
        return None, {}, (
            f"the Copilot CLI has {len(current)} Copilot accounts cached and the card "
            "can't tell which one it is signed in with"
        )
    response, quota = current[0]
    return quota, response, ""


def _billed_in_credits(quota: Dict[str, Any], response: Dict[str, Any]) -> Optional[bool]:
    """The quota's own billing flag, else the account's; None when neither says."""
    for flag in (quota.get("token_based_billing"), response.get("token_based_billing")):
        if isinstance(flag, bool):
            return flag
    return None


def read_plan(cache_path: Path, now: datetime) -> PlanReading:
    """The plan's used and allowance from the CLI's cached user response; never raises."""
    if not cache_path.is_file():
        return PlanReading("unknown", reason="the Copilot CLI has not cached your plan on this machine")
    try:
        text = cache_path.read_text(encoding="utf-8")
        # The CLI heads the file with ``//`` comment lines, which JSON does not allow.
        doc = json.loads("\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//")))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        logger.warning("⚠️ Copilot plan cache %s unreadable: %s", cache_path, exc)
        return PlanReading("unknown", reason=f"the Copilot plan cache could not be read: {exc}")
    quota, response, reason = _plan_quota(doc, now)
    if quota is None:
        return PlanReading("unknown", reason=reason)
    if quota.get("unlimited") is True:
        return PlanReading("unknown", reason="the plan has no monthly limit")
    entitlement, remaining = quota.get("entitlement"), quota.get("quota_remaining")
    if not (_is_number(entitlement) and _is_number(remaining)) or entitlement <= 0:
        return PlanReading("unknown", reason="the Copilot plan cache has no usable allowance")
    billed_in_credits = _billed_in_credits(quota, response)
    if billed_in_credits is None:
        return PlanReading(
            "unknown",
            reason="the Copilot plan cache does not say whether the plan is billed in credits or requests",
        )
    return PlanReading(
        "ok",
        used_credits=round(max(0.0, float(entitlement) - float(remaining)), 2),
        allowance=float(entitlement),
        reset_at=_parse_utc(response.get("quota_reset_date_utc")),
        as_of=str(quota.get("timestamp_utc") or response.get("_retrieved_at") or ""),
        billed_in_credits=billed_in_credits,
    )


def payload(
    reading: Optional[Reading], plan: Optional[PlanReading], allowance: int, now: datetime,
) -> Dict[str, Any]:
    """The ``/api/copilot-usage`` body: the plan's month, and this machine's share.

    ``state``, ``used_pct`` and ``reason`` describe the plan (the bar). A plan
    nobody could read leaves ``used_pct`` None, never this machine's credits
    over a guessed allowance. ``allowance`` is the plan's when known, else the
    Settings value, as ``allowance_source`` says. ``today_*`` and
    ``machine_month_credits`` are this machine's log. Either reading is None
    while its first read runs (``pending``).
    """
    plan_ok = plan is not None and plan.state == "ok" and plan.used_credits is not None and bool(plan.allowance)
    plan_reason = "" if plan is None else plan.reason
    # The plan counts every machine, so on a credit plan it is never below this
    # machine's own month; when it is, it is stale or another account's. A
    # premium-request plan counts requests, a different unit, so it is not compared.
    if (
        plan_ok and plan.billed_in_credits is True
        and reading is not None and reading.state == "ok" and reading.used_credits is not None
        and reading.used_credits > plan.used_credits
    ):
        plan_ok = False
        plan_reason = (
            "the plan figure is older or from another account: this machine alone "
            "used more credits this month"
        )
    if plan_ok and plan.reset_at is not None:
        start, reset = month_before(plan.reset_at), plan.reset_at
    else:
        start, reset = billing_month(now)
    body: Dict[str, Any] = {
        "state": "pending" if plan is None else ("ok" if plan_ok else "unknown"),
        "used_credits": None,
        "used_pct": None,
        "allowance": allowance,
        "allowance_source": "config",
        "pace_pct": pace_pct(now, start, reset),
        "cycle_start": start.isoformat(),
        "reset_at": reset.isoformat(),
        "source": PLAN_SOURCE,
        "scope": PLAN_SCOPE,
        "as_of": "",
        "reason": plan_reason,
        "today_state": "pending" if reading is None else reading.state,
        "today_credits": None,
        "machine_month_credits": None,
        "today_source": SOURCE,
        "today_scope": SCOPE,
        "today_reason": "" if reading is None else reading.reason,
    }
    if plan_ok:
        body["used_credits"] = plan.used_credits
        body["allowance"] = plan.allowance
        body["allowance_source"] = "plan"
        body["used_pct"] = round(100.0 * plan.used_credits / plan.allowance, 1)
        body["as_of"] = plan.as_of
    if reading is not None and reading.state == "ok":
        body["today_credits"] = reading.today_credits
        body["machine_month_credits"] = reading.used_credits
    return body
