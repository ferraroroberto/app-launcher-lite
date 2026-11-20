"""Remote access at Copilot launch: Copilot's two user settings follow Lite's switch."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.launching import copilot_remote as cr

# The suite-wide fixture replaces `cr.apply`; these tests exercise the real one.
_apply = cr.apply


def _settings(tmp_path: Path, **group: object) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "x", "ahpEnvironment": group}), encoding="utf-8")
    return path


@pytest.fixture
def runs(monkeypatch):
    """Record every `copilot config` call instead of spawning one."""
    calls: list[list[str]] = []
    monkeypatch.setattr(cr.shutil, "which", lambda *_a, **_k: "copilot.exe")

    def fake_run(argv, **_kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cr.subprocess, "run", fake_run)
    return calls


def test_read_state_needs_both_keys_true(tmp_path):
    both = _settings(tmp_path, acceptRemoteConnections=True, publishNewSessionsByDefault=True)
    assert cr.read_state(both) is True
    half = _settings(tmp_path, acceptRemoteConnections=True, publishNewSessionsByDefault=False)
    assert cr.read_state(half) is False
    assert cr.read_state(tmp_path / "missing.json") is False


def test_read_state_unreadable_is_unknown_not_off(tmp_path):
    bad = tmp_path / "settings.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cr.read_state(bad) is None


def test_apply_on_sets_both_keys(tmp_path, runs):
    path = _settings(tmp_path)
    assert _apply(True, path=path) == "applied"
    assert runs == [
        ["copilot.exe", "config", cr.ACCEPT_KEY, "on"],
        ["copilot.exe", "config", cr.PUBLISH_KEY, "on"],
    ]


def test_apply_off_sets_both_keys_off(tmp_path, runs):
    path = _settings(tmp_path, acceptRemoteConnections=True, publishNewSessionsByDefault=True)
    assert _apply(False, path=path) == "applied"
    assert [c[-1] for c in runs] == ["off", "off"]


def test_apply_matching_state_spawns_nothing(tmp_path, runs):
    on = _settings(tmp_path, acceptRemoteConnections=True, publishNewSessionsByDefault=True)
    assert _apply(True, path=on) == "unchanged"
    off = tmp_path / "off.json"
    off.write_text("{}", encoding="utf-8")
    assert _apply(False, path=off) == "unchanged"
    assert runs == []


def test_apply_failure_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(cr.shutil, "which", lambda *_a, **_k: "copilot.exe")
    monkeypatch.setattr(
        cr.subprocess, "run",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 1, "", "boom"),
    )
    assert _apply(True, path=_settings(tmp_path)) == "failed"


def test_apply_without_copilot_on_path_fails_quietly(tmp_path, monkeypatch):
    monkeypatch.setattr(cr.shutil, "which", lambda *_a, **_k: None)
    assert _apply(True, path=_settings(tmp_path)) == "failed"


def test_apply_off_also_clears_a_registered_environment(tmp_path, runs):
    # Only the environment is on: Copilot still advertises remote access.
    half = _settings(tmp_path, acceptRemoteConnections=True, publishNewSessionsByDefault=False)
    assert _apply(False, path=half) == "applied"
    assert [c[-1] for c in runs] == ["off", "off"]
