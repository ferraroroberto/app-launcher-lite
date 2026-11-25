"""``config.json`` ``webapp.port`` is the one source of the webapp port.

Every reader — the tray's manager, the ``webapp`` CLI command, the mirror
window URL, and the ``.bat`` launchers through ``scripts/webapp_port.py`` —
resolves it through ``src.app_config``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.webapp.manager import WebappManagerConfig, load_config
from src.app_config import DEFAULT_WEBAPP_PORT, resolve_webapp_port, webapp_port
from src.subprocess_flags import NO_WINDOW

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_config(tmp_path: Path, payload: object) -> Path:
    target = tmp_path / "config.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


class TestResolve:
    def test_default_is_8465(self):
        assert DEFAULT_WEBAPP_PORT == 8465

    def test_key_present(self, tmp_path):
        assert resolve_webapp_port(_write_config(tmp_path, {"webapp": {"port": 9001}})) == 9001

    def test_key_missing_falls_back_to_default(self, tmp_path):
        assert resolve_webapp_port(_write_config(tmp_path, {"webapp": {"host": "x"}})) == 8465

    def test_webapp_section_missing_falls_back_to_default(self, tmp_path):
        assert resolve_webapp_port(_write_config(tmp_path, {"log_level": "INFO"})) == 8465

    def test_file_missing_falls_back_to_default(self, tmp_path):
        assert resolve_webapp_port(tmp_path / "nope.json") == 8465

    def test_unreadable_file_falls_back_to_default(self, tmp_path):
        target = tmp_path / "config.json"
        target.write_text("{not json", encoding="utf-8")
        assert resolve_webapp_port(target) == 8465

    @pytest.mark.parametrize("bad", ["abc", 0, 70000, -5, None, []])
    def test_unusable_value_falls_back_to_default(self, bad):
        assert webapp_port({"port": bad}) == 8465

    def test_numeric_string_accepted(self):
        assert webapp_port({"port": "9002"}) == 9002


class TestManagerReadsTheSameSetting:
    def test_manager_uses_config_json_value(self):
        assert load_config({"port": 9003}).port == 9003

    def test_manager_default_matches(self):
        assert load_config(None).port == WebappManagerConfig().port == DEFAULT_WEBAPP_PORT


class TestBatchLaunchersReadTheHelper:
    def test_helper_prints_just_the_port(self):
        out = subprocess.run(
            [sys.executable, "-m", "scripts.webapp_port"],
            cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
            creationflags=NO_WINDOW,
        )
        # The subprocess reads this checkout's real config, which the conftest
        # fixture hides from in-process calls, so resolve against that same path.
        real_config = _REPO_ROOT / "config" / "config.json"
        assert out.stdout.strip() == str(resolve_webapp_port(real_config))

    def test_webapp_bat_has_no_literal_port_in_its_server_commands(self):
        text = (_REPO_ROOT / "scripts" / "webapp.bat").read_text(encoding="utf-8")
        assert "scripts.webapp_port" in text
        assert not re.search(r"--port\s+8465", text)

    def test_tray_bat_reads_the_owned_port_from_the_helper(self):
        text = (_REPO_ROOT / "tray.bat").read_text(encoding="utf-8")
        assert "scripts.webapp_port" in text
