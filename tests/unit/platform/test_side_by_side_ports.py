"""``config.json`` ``webapp.port`` and ``webapp.session_host_port`` are the only two port sources.

A second Lite install on a PC that already runs another launcher sets both ports
in ``config/config.json`` and nothing else. Every reader resolves them through
``src.app_config``, so no code path falls back to the default ports.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src import app_config, webapp_config
from src.app_config import (
    DEFAULT_SESSION_HOST_PORT,
    DEFAULT_WEBAPP_PORT,
    resolve_session_host_port,
    resolve_webapp_port,
    session_host_port,
)
from src.launching import restart_check

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FREE_WEBAPP, _FREE_SESSION_HOST = 18465, 18466


@pytest.fixture
def side_by_side(tmp_path, monkeypatch) -> Path:
    """A ``config.json`` on free ports, plus a stale ``webapp_config.json`` on the defaults."""
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"webapp": {"port": _FREE_WEBAPP, "session_host_port": _FREE_SESSION_HOST}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_config, "DEFAULT_CONFIG_PATH", config)
    return config


class TestResolveSessionHostPort:
    def test_default_is_8466_and_matches_the_settings_default(self):
        from src.webapp_settings.values import (
            DEFAULT_SESSION_HOST_PORT as SETTINGS_DEFAULT,
        )

        assert DEFAULT_SESSION_HOST_PORT == SETTINGS_DEFAULT == 8466

    def test_key_present(self, tmp_path):
        target = tmp_path / "config.json"
        target.write_text(json.dumps({"webapp": {"session_host_port": 9100}}), encoding="utf-8")
        assert resolve_session_host_port(target) == 9100

    def test_key_missing_or_file_missing_falls_back(self, tmp_path):
        target = tmp_path / "config.json"
        target.write_text(json.dumps({"webapp": {"port": 9001}}), encoding="utf-8")
        assert resolve_session_host_port(target) == 8466
        assert resolve_session_host_port(tmp_path / "nope.json") == 8466

    @pytest.mark.parametrize("bad", ["abc", 0, 70000, -5, None, []])
    def test_unusable_value_falls_back(self, bad):
        assert session_host_port({"session_host_port": bad}) == 8466


class TestEveryReaderFollowsConfigJson:
    def test_webapp_config_ignores_stale_ports_in_its_own_file(self, side_by_side, tmp_path):
        stale = tmp_path / "webapp_config.json"
        stale.write_text(
            json.dumps({"port": DEFAULT_WEBAPP_PORT, "session_host_port": DEFAULT_SESSION_HOST_PORT}),
            encoding="utf-8",
        )
        cfg = webapp_config.load_webapp_config(stale)
        assert (cfg.port, cfg.session_host_port) == (_FREE_WEBAPP, _FREE_SESSION_HOST)

    def test_webapp_config_defaults_follow_config_json_too(self, side_by_side, tmp_path):
        cfg = webapp_config.load_webapp_config(tmp_path / "absent.json")
        assert (cfg.port, cfg.session_host_port) == (_FREE_WEBAPP, _FREE_SESSION_HOST)

    def test_the_session_host_env_override_still_wins(self, side_by_side, tmp_path, monkeypatch):
        monkeypatch.setenv("LAUNCHER_SESSION_HOST_PORT", "19999")
        assert webapp_config.load_webapp_config(tmp_path / "absent.json").session_host_port == 19999

    def test_restart_check_defaults_to_the_configured_webapp_port(self, side_by_side, monkeypatch):
        seen: list[int] = []
        monkeypatch.setattr(restart_check, "snapshot", lambda port: seen.append(port) or 0)
        assert restart_check.main(["snapshot"]) == 0
        assert seen == [_FREE_WEBAPP]

    def test_the_helper_prints_the_configured_ports(self, side_by_side, capsys):
        from scripts import webapp_port

        assert webapp_port.main([]) == 0
        assert webapp_port.main(["--session-host"]) == 0
        assert capsys.readouterr().out.split() == [str(_FREE_WEBAPP), str(_FREE_SESSION_HOST)]
        assert resolve_webapp_port() == _FREE_WEBAPP


class TestNoScriptActsOnADefaultPort:
    """The scripts that reclaim or probe a port take it from the resolver, never a literal."""

    def test_restart_session_host_reclaims_and_probes_the_configured_ports(self):
        text = (_REPO_ROOT / "scripts" / "restart-session-host.ps1").read_text(encoding="utf-8")
        assert "scripts.webapp_port" in text
        assert "-Ports $sessionHostPort" in text
        assert "restart_check session-host --port $webappPort" in text
        assert not re.search(r"-Ports\s+\d", text)
        assert not re.search(r"127\.0\.0\.1:\d", text)

    def test_restart_session_host_uses_the_repo_local_lifecycle_helper(self):
        text = (_REPO_ROOT / "scripts" / "restart-session-host.ps1").read_text(encoding="utf-8")
        assert 'Join-Path $PSScriptRoot "tray_lifecycle.ps1"' in text
        assert ".claude" not in text

    def test_the_named_tunnel_serves_the_configured_webapp_port(self, side_by_side, monkeypatch):
        from scripts import run_named_tunnel

        monkeypatch.delenv("WEBAPP_PORT", raising=False)
        assert run_named_tunnel.tunnel_port() == _FREE_WEBAPP
        monkeypatch.setenv("WEBAPP_PORT", "19001")
        assert run_named_tunnel.tunnel_port() == 19001

    def test_tray_bat_reclaims_only_the_resolved_webapp_port(self):
        text = (_REPO_ROOT / "tray.bat").read_text(encoding="utf-8")
        assert "-m scripts.webapp_port" in text
        assert "--session-host" not in text, "the session-host stays out of the restart reclaim"
        assert "-Ports \"%OWNED_PORTS%\"" in text
