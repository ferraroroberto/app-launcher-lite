"""POST /api/diagnostics and GET /api/diagnostics/{name}: save, download, gate.

Every log line and secret here is invented.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src import diagnostics_bundle
from src.middleware.routes import terminal_guard_level


@pytest.fixture
def diag_client(webapp_client, tmp_path, monkeypatch):
    """The webapp, its bundle reading a temp log folder and writing to a temp
    output folder, and the test client treated as the PC itself so the
    passkey gate steps aside (the gate's own row is tested below)."""
    from src import middleware

    client, app, _ = webapp_client
    logs = tmp_path / "webapp"
    logs.mkdir()
    stamp = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S,000")
    (logs / "webapp-warnings.log").write_text(
        f"{stamp} [WARNING] probe: Authorization: Bearer leakyleakyleaky\n",
        encoding="utf-8",
    )
    sources = diagnostics_bundle.Sources(
        log_dir=logs, inbox_dir=tmp_path / "inbox", managed_file=tmp_path / "m.json",
        sessions_file=tmp_path / "s.json", webapp_port=1, session_host_port=2,
        tray_pid_file=tmp_path / ".tray.pid",
    )
    out = tmp_path / "out"
    monkeypatch.setattr(diagnostics_bundle, "sources_from_config", lambda cfg: sources)
    monkeypatch.setattr(diagnostics_bundle, "process_lines", lambda *a: [])
    monkeypatch.setattr(diagnostics_bundle, "OUTPUT_DIR", out)
    monkeypatch.setattr(
        middleware.trust, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )
    return client, out


def test_save_then_download_returns_the_redacted_file(diag_client):
    client, out = diag_client
    res = client.post("/api/diagnostics", json={"minutes": 15})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["path"].startswith(str(out))
    assert body["bytes"] > 0
    got = client.get(f"/api/diagnostics/{body['name']}")
    assert got.status_code == 200
    assert got.text.startswith("App Launcher Lite diagnostics")
    assert "probe: Authorization: [REDACTED]" in got.text
    assert "leakyleakyleaky" not in got.text


def test_only_the_offered_windows_and_saved_names_are_accepted(diag_client):
    client, _ = diag_client
    assert client.post("/api/diagnostics", json={"minutes": 7}).status_code == 400
    assert client.get("/api/diagnostics/diagnostics-20990101-000000.txt").status_code == 404
    assert client.get("/api/diagnostics/..%2Fconfig.json").status_code == 404


def test_both_routes_are_passkey_gated():
    assert terminal_guard_level("/api/diagnostics") == "passkey"
    assert terminal_guard_level("/api/diagnostics/diagnostics-20990101-000000.txt") == "passkey"
