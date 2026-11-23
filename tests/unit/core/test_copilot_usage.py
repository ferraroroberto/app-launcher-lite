"""The Copilot usage readers (``src.copilot_usage``) against temporary files.

Synthetic rows and a fake plan cache only. Pins the month and today sums, the
UTC billing month and its pace, that every unreadable log (missing, wrong
shape, locked) reads as ``unknown`` with a reason, never as 0 credits, and
that the card's percent and pace come from the plan's own used and
allowance, or read unknown, never this machine's share over a guessed one.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import copilot_usage

NOW = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
CREDIT = copilot_usage.NANO_PER_CREDIT


def _log(path: Path, rows: list[tuple[str, int]]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE assistant_usage_events (id INTEGER PRIMARY KEY, model TEXT,"
        " total_nano_aiu INTEGER, created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.executemany(
        "INSERT INTO assistant_usage_events (model, total_nano_aiu, created_at) VALUES ('m', ?, ?)",
        [(nano, at) for at, nano in rows],
    )
    conn.commit()
    conn.close()
    return path


def test_sums_this_month_and_today_in_both_timestamp_shapes(tmp_path: Path) -> None:
    db = _log(tmp_path / "s.db", [
        ("2026-10-31T23:59:59.999Z", 50 * CREDIT),   # last month
        ("2026-11-01T00:00:00.000Z", 10 * CREDIT),   # first instant of the month
        ("2026-11-21 08:00:00", 5 * CREDIT),          # SQLite's own default shape
        ("2026-11-22T09:30:00.000Z", CREDIT // 2),    # today
        ("2026-12-01T00:00:00.000Z", 70 * CREDIT),   # next month
    ])
    reading = copilot_usage.read(db, NOW)
    assert reading == copilot_usage.Reading("ok", used_credits=15.5, today_credits=0.5)


def test_empty_month_is_zero_not_unknown(tmp_path: Path) -> None:
    reading = copilot_usage.read(_log(tmp_path / "s.db", []), NOW)
    assert (reading.state, reading.used_credits, reading.today_credits) == ("ok", 0, 0)


def test_missing_log_is_unknown(tmp_path: Path) -> None:
    reading = copilot_usage.read(tmp_path / "absent.db", NOW)
    assert reading.state == "unknown"
    assert "no Copilot usage log" in reading.reason


@pytest.mark.parametrize("ddl, reason", [
    ("CREATE TABLE other (x INTEGER)", "no assistant_usage_events table"),
    ("CREATE TABLE assistant_usage_events (id INTEGER, created_at TEXT)", "lacks total_nano_aiu"),
])
def test_wrong_shape_is_unknown(tmp_path: Path, ddl: str, reason: str) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute(ddl)
    conn.commit()
    conn.close()
    reading = copilot_usage.read(db, NOW)
    assert reading.state == "unknown"
    assert reason in reading.reason


def test_not_a_database_is_unknown(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    db.write_bytes(b"not sqlite at all, just bytes" * 100)
    assert copilot_usage.read(db, NOW).state == "unknown"


def test_locked_log_is_unknown_within_the_lock_timeout(tmp_path: Path) -> None:
    db = _log(tmp_path / "s.db", [("2026-11-02T00:00:00Z", CREDIT)])
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        reading = copilot_usage.read(db, NOW)
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert reading.state == "unknown"
    assert elapsed < copilot_usage.LOCK_TIMEOUT_S + 2


@pytest.mark.parametrize("now, start, reset", [
    (datetime(2026, 11, 22, tzinfo=timezone.utc), "2026-11-01", "2026-12-01"),
    (datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc), "2026-12-01", "2027-01-01"),
    (datetime(2028, 2, 29, 12, tzinfo=timezone.utc), "2028-02-01", "2028-03-01"),
])
def test_billing_month_is_the_utc_calendar_month(now: datetime, start: str, reset: str) -> None:
    got = copilot_usage.billing_month(now)
    assert (got[0].date().isoformat(), got[1].date().isoformat()) == (start, reset)


def test_pace_follows_the_month_length() -> None:
    feb_start, feb_reset = copilot_usage.billing_month(datetime(2028, 2, 1, tzinfo=timezone.utc))
    # Leap February: half of 29 days is noon on the 15th.
    assert copilot_usage.pace_pct(datetime(2028, 2, 15, 12, tzinfo=timezone.utc), feb_start, feb_reset) == 50.0
    assert copilot_usage.pace_pct(feb_start, feb_start, feb_reset) == 0.0


def _entry(*, entitlement=1000, remaining=750.5, unlimited=False, billing=True,
           reset="2026-12-01T00:00:00.000Z", retrieved="2026-11-22T09:00:00.000Z") -> dict:
    """One synthetic sign-in's cached user response."""
    quota = {"entitlement": entitlement, "quota_remaining": remaining,
             "unlimited": unlimited, "timestamp_utc": "2026-11-22T09:00:00.0000000Z"}
    if billing is not None:
        quota["token_based_billing"] = billing
    return {
        "schemaVersion": 1,
        "retrievedAt": retrieved,
        "response": {
            "quota_reset_date_utc": reset,
            "quota_snapshots": {
                "chat": {"entitlement": 0, "quota_remaining": 0, "unlimited": True},
                "premium_interactions": quota,
            },
        },
    }


def _plan_cache(path: Path, *, extra_entries: dict | None = None, **entry_kwargs) -> Path:
    """A fake CLI user cache: ``//`` header lines, then entries keyed by sign-in."""
    doc = {"copilotUserCache": {"v1:a": _entry(**entry_kwargs), **(extra_entries or {})}}
    path.write_text("// Disposable cache, safe to delete.\n// Settings live elsewhere.\n" + json.dumps(doc),
                    encoding="utf-8")
    return path


def test_plan_reads_used_as_entitlement_minus_remaining(tmp_path: Path) -> None:
    plan = copilot_usage.read_plan(_plan_cache(tmp_path / "c.json"), NOW)
    assert (plan.state, plan.used_credits, plan.allowance) == ("ok", 249.5, 1000.0)
    assert plan.reset_at == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert plan.as_of.startswith("2026-11-22T09:00")


def test_plan_picks_the_one_sign_in_still_in_its_cycle_not_the_newest(tmp_path: Path) -> None:
    # The other account's entry was fetched later but its cycle has ended, so
    # it cannot be the plan the CLI's status line shows now.
    lapsed = _entry(entitlement=50, remaining=0, reset="2026-11-15T00:00:00.000Z",
                    retrieved="2026-11-22T09:59:00.000Z")
    plan = copilot_usage.read_plan(_plan_cache(tmp_path / "c.json", extra_entries={"v1:b": lapsed}), NOW)
    assert (plan.state, plan.used_credits, plan.allowance) == ("ok", 249.5, 1000.0)


def test_two_current_sign_ins_without_settings_are_unknown_not_the_newest(tmp_path: Path) -> None:
    other = _entry(entitlement=50, remaining=0, retrieved="2026-11-22T09:59:00.000Z")
    plan = copilot_usage.read_plan(_plan_cache(tmp_path / "c.json", extra_entries={"v1:b": other}), NOW)
    assert plan.state == "unknown" and plan.used_credits is None
    assert "2 cached plan entries" in plan.reason and "can't tell which" in plan.reason


def _acct(login: str, **kwargs) -> dict:
    """A synthetic entry that names its account, as the CLI's cached response does."""
    entry = _entry(**kwargs)
    entry["response"]["login"] = login
    return entry


def _signed_in(path: Path, login) -> Path:
    """A fake CLI settings file with ``//`` header lines and the signed-in login."""
    doc = {"lastLoggedInUser": {"host": "https://example.invalid", "login": login}} if login else {}
    path.write_text("// User settings.\n" + json.dumps(doc), encoding="utf-8")
    return path


def _multi_cache(path: Path, entries: dict) -> Path:
    path.write_text("// Disposable cache.\n" + json.dumps({"copilotUserCache": entries}), encoding="utf-8")
    return path


def test_signed_in_login_picks_its_entry_among_several(tmp_path: Path) -> None:
    cache = _multi_cache(tmp_path / "c.json", {
        "v1:a": _acct("sample-user", remaining=600, retrieved="2026-11-20T09:00:00.000Z"),
        "v1:b": _acct("other-user", entitlement=50, remaining=1, retrieved="2026-11-22T09:59:00.000Z"),
    })
    plan = copilot_usage.read_plan(cache, NOW, _signed_in(tmp_path / "cfg.json", "sample-user"))
    assert (plan.state, plan.used_credits, plan.allowance) == ("ok", 400.0, 1000.0)


def test_two_refreshes_of_the_signed_in_login_use_the_newest(tmp_path: Path) -> None:
    cache = _multi_cache(tmp_path / "c.json", {
        "v1:a": _acct("Sample-User", remaining=900, retrieved="2026-11-21T09:00:00.000Z"),
        "v1:b": _acct("sample-user", remaining=700, retrieved="2026-11-22T09:00:00.000Z"),
        "v1:c": _acct("other-user", entitlement=50, remaining=1, retrieved="2026-11-22T09:59:00.000Z"),
    })
    plan = copilot_usage.read_plan(cache, NOW, _signed_in(tmp_path / "cfg.json", "sample-user"))
    assert (plan.state, plan.used_credits) == ("ok", 300.0)


def test_signed_in_login_with_no_matching_entry_is_unknown_not_another_account(tmp_path: Path) -> None:
    cache = _multi_cache(tmp_path / "c.json", {
        "v1:a": _acct("other-user", remaining=600), "v1:b": _acct("third-user", remaining=100),
    })
    plan = copilot_usage.read_plan(cache, NOW, _signed_in(tmp_path / "cfg.json", "sample-user"))
    assert plan.state == "unknown" and plan.used_credits is None
    assert "no entry for the account" in plan.reason
    assert "sample-user" not in plan.reason and "other-user" not in plan.reason


def test_lone_entry_of_another_account_is_unknown(tmp_path: Path) -> None:
    cache = _multi_cache(tmp_path / "c.json", {"v1:a": _acct("other-user", remaining=600)})
    plan = copilot_usage.read_plan(cache, NOW, _signed_in(tmp_path / "cfg.json", "sample-user"))
    assert plan.state == "unknown" and "no entry for the account" in plan.reason


def test_other_accounts_lapsed_entry_never_beats_the_signed_in_one(tmp_path: Path) -> None:
    cache = _multi_cache(tmp_path / "c.json", {
        "v1:a": _acct("sample-user", remaining=600, retrieved="2026-11-20T09:00:00.000Z"),
        "v1:b": _acct("sample-user", remaining=1, reset="2026-11-15T00:00:00.000Z",
                      retrieved="2026-11-22T09:59:00.000Z"),
    })
    plan = copilot_usage.read_plan(cache, NOW, _signed_in(tmp_path / "cfg.json", "sample-user"))
    assert (plan.state, plan.used_credits) == ("ok", 400.0)


@pytest.mark.parametrize("config, reason", [
    ("missing", "settings file is missing"),
    ("{not json", "settings could not be read"),
    ("nologin", "name no signed-in account"),
])
def test_several_entries_without_a_readable_login_are_unknown(tmp_path: Path, config: str, reason: str) -> None:
    cache = _multi_cache(tmp_path / "c.json", {
        "v1:a": _acct("sample-user", remaining=600), "v1:b": _acct("other-user", remaining=100),
    })
    cfg = tmp_path / "cfg.json"
    if config == "nologin":
        _signed_in(cfg, None)
    elif config != "missing":
        cfg.write_text(config, encoding="utf-8")
    plan = copilot_usage.read_plan(cache, NOW, cfg)
    assert plan.state == "unknown" and plan.used_credits is None
    assert "2 cached plan entries" in plan.reason and reason in plan.reason


def test_single_entry_without_config_reads_as_before(tmp_path: Path) -> None:
    cache = _multi_cache(tmp_path / "c.json", {"v1:a": _acct("sample-user", remaining=600)})
    for config in (None, tmp_path / "absent.json"):
        plan = copilot_usage.read_plan(cache, NOW, config)
        assert (plan.state, plan.used_credits) == ("ok", 400.0)


def test_plan_names_its_billing_unit(tmp_path: Path) -> None:
    credits = copilot_usage.read_plan(_plan_cache(tmp_path / "a.json"), NOW)
    requests = copilot_usage.read_plan(_plan_cache(tmp_path / "b.json", billing=False), NOW)
    assert (credits.state, credits.billed_in_credits) == ("ok", True)
    assert (requests.state, requests.billed_in_credits, requests.allowance) == ("ok", False, 1000.0)


@pytest.mark.parametrize("kwargs, reason", [
    ({"unlimited": True}, "no monthly limit"),
    ({"entitlement": 0}, "no usable allowance"),
    ({"remaining": None}, "no usable allowance"),
    ({"reset": "2026-11-01T00:00:00.000Z"}, "predates the last reset"),
    ({"billing": None}, "does not say whether the plan is billed"),
])
def test_plan_that_cannot_give_a_percent_is_unknown(tmp_path: Path, kwargs: dict, reason: str) -> None:
    plan = copilot_usage.read_plan(_plan_cache(tmp_path / "c.json", **kwargs), NOW)
    assert plan.state == "unknown" and reason in plan.reason
    assert plan.used_credits is None


@pytest.mark.parametrize("content, reason", [
    (None, "has not cached your plan"),
    ("// header only\nnot json", "could not be read"),
    ('{"copilotUserCache": {}}', "holds no plan"),
    ('{"copilotUserCache": {"v1:a": {"response": {"quota_snapshots": {}}}}}', "no premium_interactions quota"),
])
def test_unreadable_plan_cache_is_unknown(tmp_path: Path, content, reason: str) -> None:
    path = tmp_path / "c.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    plan = copilot_usage.read_plan(path, NOW)
    assert plan.state == "unknown" and reason in plan.reason


def test_month_before_clamps_to_the_shorter_month() -> None:
    utc = timezone.utc
    assert copilot_usage.month_before(datetime(2026, 3, 31, tzinfo=utc)) == datetime(2026, 2, 28, tzinfo=utc)
    assert copilot_usage.month_before(datetime(2027, 1, 15, tzinfo=utc)) == datetime(2026, 12, 15, tzinfo=utc)


def test_payload_percent_and_pace_follow_the_plan_not_this_machine() -> None:
    machine = copilot_usage.Reading("ok", used_credits=500.0, today_credits=3.0)
    plan = copilot_usage.PlanReading("ok", used_credits=250.0, allowance=1000.0,
                                     reset_at=datetime(2026, 12, 1, tzinfo=timezone.utc), as_of="t")
    body = copilot_usage.payload(machine, plan, 300, NOW)
    assert (body["state"], body["used_pct"], body["used_credits"]) == ("ok", 25.0, 250.0)
    assert (body["allowance"], body["allowance_source"]) == (1000.0, "plan")
    assert (body["cycle_start"][:10], body["reset_at"][:10]) == ("2026-11-01", "2026-12-01")
    assert body["pace_pct"] == copilot_usage.pace_pct(NOW, *copilot_usage.billing_month(NOW))
    assert (body["today_credits"], body["machine_month_credits"]) == (3.0, 500.0)


def test_payload_pace_follows_a_plan_reset_off_the_first() -> None:
    plan = copilot_usage.PlanReading("ok", used_credits=10.0, allowance=100.0,
                                     reset_at=datetime(2026, 12, 15, tzinfo=timezone.utc))
    body = copilot_usage.payload(None, plan, 300, NOW)
    assert (body["cycle_start"][:10], body["reset_at"][:10]) == ("2026-11-15", "2026-12-15")
    assert body["today_state"] == "pending" and body["today_credits"] is None


def test_payload_with_the_plan_unknown_shows_no_percent_but_keeps_this_machine() -> None:
    machine = copilot_usage.Reading("ok", used_credits=75.0, today_credits=3.0)
    plan = copilot_usage.PlanReading("unknown", reason="no plan cached")
    body = copilot_usage.payload(machine, plan, 300, NOW)
    assert (body["state"], body["used_pct"], body["used_credits"], body["reason"]) == (
        "unknown", None, None, "no plan cached")
    assert (body["allowance"], body["allowance_source"]) == (300, "config")
    assert body["today_credits"] == 3.0
    assert body["reset_at"][:10] == "2026-12-01"


def test_payload_never_claims_zero_when_this_machine_is_unknown() -> None:
    body = copilot_usage.payload(copilot_usage.Reading("unknown", reason="locked"), None, 300, NOW)
    assert (body["state"], body["today_state"], body["today_credits"], body["today_reason"]) == (
        "pending", "unknown", None, "locked")


def _credits_plan(used: float) -> copilot_usage.PlanReading:
    return copilot_usage.PlanReading("ok", used_credits=used, allowance=1000.0,
                                     reset_at=datetime(2026, 12, 1, tzinfo=timezone.utc),
                                     billed_in_credits=True)


def test_payload_plan_below_this_machine_alone_is_unknown() -> None:
    machine = copilot_usage.Reading("ok", used_credits=300.0, today_credits=3.0)
    body = copilot_usage.payload(machine, _credits_plan(250.0), 300, NOW)
    assert (body["state"], body["used_pct"], body["used_credits"]) == ("unknown", None, None)
    assert "older or from another account" in body["reason"]
    assert (body["allowance"], body["allowance_source"]) == (300, "config")
    assert body["machine_month_credits"] == 300.0


def test_payload_plan_at_or_above_this_machine_is_shown() -> None:
    machine = copilot_usage.Reading("ok", used_credits=250.0, today_credits=3.0)
    body = copilot_usage.payload(machine, _credits_plan(250.0), 300, NOW)
    assert (body["state"], body["used_pct"]) == ("ok", 25.0)


def test_payload_request_plan_is_not_compared_with_this_machines_credits() -> None:
    # A premium-request plan counts requests, not credits: the units differ.
    plan = copilot_usage.PlanReading("ok", used_credits=10.0, allowance=300.0, billed_in_credits=False)
    machine = copilot_usage.Reading("ok", used_credits=40.0, today_credits=1.0)
    assert copilot_usage.payload(machine, plan, 300, NOW)["used_pct"] == 3.3
