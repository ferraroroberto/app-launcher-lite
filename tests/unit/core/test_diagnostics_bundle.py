"""Save diagnostics: one redacted, time-ordered file from every Lite log.

Every log line, secret, id and path here is invented.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import diagnostics_bundle as db

NOW = datetime(2026, 3, 4, 12, 0, 0)


def _stamp(minutes_ago: float) -> str:
    moment = NOW - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S,") + f"{moment.microsecond // 1000:03d}"


def _write(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stamp = NOW.timestamp()
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def sources(tmp_path, monkeypatch) -> db.Sources:
    monkeypatch.setattr(db, "process_lines", lambda *a: ["webapp :1: pid 1, started 2026-03-04 11:00:00"])
    state = tmp_path / "state"
    (state / "chief-inbox" / ".claimed").mkdir(parents=True)
    stamp_ms = int((NOW - timedelta(seconds=90)).timestamp() * 1000)
    (state / "chief-inbox" / f"{stamp_ms}-ev1.json").write_text("{}", encoding="utf-8")
    (state / "chief-managed.json").write_text(json.dumps(
        {"lane-a": {"repo": "demo", "number": 7, "dispatched_at": "2026-03-04T10:00:00Z"}}), encoding="utf-8")
    return db.Sources(
        log_dir=tmp_path / "webapp",
        inbox_dir=state / "chief-inbox",
        managed_file=state / "chief-managed.json",
        sessions_file=state / "sessions-state.json",
        webapp_port=1,
        session_host_port=2,
        tray_pid_file=tmp_path / ".tray.pid",
    )


def _save(sources: db.Sources, tmp_path: Path, minutes: int = 60, literals=()) -> str:
    saved = db.save_bundle(sources, minutes, literals=literals, out_dir=tmp_path / "out", now=NOW)
    assert saved.path.parent == tmp_path / "out"
    assert db.FILE_NAME_RE.match(saved.path.name)
    return saved.path.read_text(encoding="utf-8")


def test_secrets_in_log_lines_are_redacted_in_the_written_file(sources, tmp_path):
    secrets = [
        "Authorization: Bearer abcDEF123456789xyz",
        "Cookie: session=cookievalue123; other=1",
        "X-Terminal-Token: termtoken987654321",
        'payload {"auth_token": "tok-literal-0001", "password": "hunter2hunter2"}',
        "telegram_bot_token=123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "GET /api/screen?tt=queryterm55555&x=1",
        "pushed with ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 and glpat-abcdefghijklmnopqrstu",
        "client key sk-proj1234567890abcdefghij",
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0aaaa.c2lnbmF0dXJlc2ln",
        "config api_key = plainapikey42",
        "the owner's configured value configuredSecretValue appears raw",
    ]
    _write(sources.log_dir / "webapp-warnings.log",
           [f"{_stamp(5)} [WARNING] probe: {line}" for line in secrets])
    text = _save(sources, tmp_path, literals=["configuredSecretValue"])

    for leaked in ("abcDEF123456789xyz", "cookievalue123", "termtoken987654321", "tok-literal-0001",
                   "hunter2hunter2", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "queryterm55555",
                   "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123", "glpat-abcdefghijklmnopqrstu",
                   "sk-proj1234567890abcdefghij", "eyJhbGciOiJIUzI1NiJ9", "plainapikey42",
                   "configuredSecretValue"):
        assert leaked not in text, leaked
    assert text.count(db.REDACTED) >= len(secrets)
    # The surrounding words survive, so the line still reads.
    assert "pushed with" in text and "appears raw" in text and "GET /api/screen?tt=" in text


def test_logs_merge_in_time_order_with_source_tags_and_window(sources, tmp_path):
    _write(sources.log_dir / "chief-doorbell.log", [
        f"{_stamp(90)} [INFO] too old to show",
        f"{_stamp(30)} [INFO] 🔔 chief doorbell rang",
        f"{_stamp(10)} [INFO] 🔕 chief doorbell skip: chief busy",
    ])
    _write(sources.log_dir / "webapp-warnings.log", [
        f"{_stamp(20)} [ERROR] app: boom",
        "Traceback (most recent call last):",
        '  File "x.py", line 1',
        f"{_stamp(1)} [WARNING] app: later",
    ])
    _write(sources.inbox_dir.parent / db.DECISIONS_LOG_NAME, [
        f"{_stamp(25)} [INFO] mailbox: event written 1-ev1.json kind=turn-end lane=lane-a",
        f"{_stamp(15)} [INFO] lane-mark: marked lane-b chief-managed for demo#8 dispatch=-",
    ])
    _write(sources.log_dir / "e2e-autoboot-webapp.log", [f"{_stamp(2)} [INFO] gate noise"])
    _write(sources.log_dir / "sessions" / "lane-a.log", [f"{_stamp(2)} [input] 'typed text'"])

    text = _save(sources, tmp_path)
    logs = text.split("== Logs, oldest first", 1)[1].splitlines()[1:]
    messages = [line.split("] ", 1)[1] for line in logs]
    assert [m.split(" ", 3)[-1] for m in messages if m[:4] == "2026"] == [
        "🔔 chief doorbell rang",
        "mailbox: event written 1-ev1.json kind=turn-end lane=lane-a",
        "app: boom",
        "lane-mark: marked lane-b chief-managed for demo#8 dispatch=-",
        "🔕 chief doorbell skip: chief busy",
        "app: later",
    ]
    # The traceback stays with its record, under the same tag.
    boom = next(i for i, line in enumerate(logs) if "app: boom" in line)
    assert logs[boom + 1].startswith("[webapp-warnings") and "Traceback" in logs[boom + 1]
    assert "too old to show" not in text
    assert "gate noise" not in text and "typed text" not in text
    assert {line[1:].split("]")[0].strip() for line in logs} == {
        "chief-doorbell", "webapp-warnings", "chief-decisions"}


def test_snapshot_names_build_processes_mailbox_and_lane_marks(sources, tmp_path):
    text = _save(sources, tmp_path, minutes=15)
    assert "window: last 15 minutes" in text
    assert "checkout build: " in text
    assert "webapp :1: pid 1, started" in text
    assert "pending: 1" in text and "-ev1.json  pending 1m30s" in text
    assert "claimed: 0" in text
    assert 'lane-a: {"dispatched_at": "2026-03-04T10:00:00Z", "number": 7, "repo": "demo"}' in text
    assert "sessions-state.json: absent" in text


def test_a_log_with_no_timestamps_contributes_its_tail(sources, tmp_path):
    _write(sources.log_dir / "session-host.log", ["WARNING:  Invalid HTTP request received."] * 3)
    text = _save(sources, tmp_path)
    assert "(no timestamps in this log; its last 3 lines" in text
    assert text.count("Invalid HTTP request received.") == 3


def test_time_only_stamps_take_the_file_day():
    day = datetime(2026, 3, 4, 0, 5, 0)
    assert db.parse_stamp("00:04:00 [INFO] x", day) == datetime(2026, 3, 4, 0, 4, 0)
    assert db.parse_stamp("23:59:00 [INFO] x", day) == datetime(2026, 3, 3, 23, 59, 0)
    assert db.parse_stamp("[2026-03-01T10:00:00] x", day) == datetime(2026, 3, 1, 10, 0, 0)
    assert db.parse_stamp("  File x", day) is None


def test_window_bounds_and_names_are_enforced(sources, tmp_path):
    with pytest.raises(ValueError):
        db.save_bundle(sources, 0, out_dir=tmp_path / "out")
    with pytest.raises(ValueError):
        db.save_bundle(sources, db.MAX_WINDOW_MINUTES + 1, out_dir=tmp_path / "out")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "diagnostics-20260304-120000.txt").write_text("x", encoding="utf-8")
    assert db.bundle_path("diagnostics-20260304-120000.txt", tmp_path / "out") is not None
    for name in ("../config.json", "diagnostics-20260304-120000.txt/../x", "other.txt", ""):
        assert db.bundle_path(name, tmp_path / "out") is None


def test_configured_secrets_reads_the_config_fields():
    cfg = SimpleNamespace(secrets={"hook": "webhook-secret-1", "n": 3})
    # "pw" is too short to blank verbatim; an empty field is skipped.
    for field, value in zip(("auth_token", "auth_password", "telegram_bot_token"),
                            ("bearer-token-value", "pw", ""), strict=True):
        setattr(cfg, field, value)

    assert db.configured_secrets(cfg) == ["bearer-token-value", "webhook-secret-1"]
