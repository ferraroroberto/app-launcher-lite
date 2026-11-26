"""``setup.bat``'s install keeps what a PC has configured, with this app's ``[install]`` table.

The installer itself is vendored (``src/installer.py``); these runs prove this
repo's declaration of it. Every run targets a temp checkout holding copies of the
tracked files, so the live ``config/`` and the real Startup folder are never
touched. venv, pip and the probes are faked; ``git check-ignore`` is real.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import boot_autostart, installer
from src.git_utils import run_git
from src.launching import install_checks

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXED_NOW = datetime.datetime(2026, 1, 2, 3, 4, 5)
BACKUP_SUFFIX = ".bak-20260102-030405"
TRACKED = (".fleet.toml", ".gitignore", "requirements.txt")


def _runner(root: Path) -> installer.Runner:
    def run(command: List[str], **kwargs: Any) -> "subprocess.CompletedProcess[Any]":
        if command[0] == "git":
            return subprocess.run(command, **kwargs)  # no-window-exempt: the installer passes creationflags
        if command[1:3] == ["-m", "venv"]:
            python = installer.venv_python(root)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
        # A probe answers "missing": its own module is tested below.
        return subprocess.CompletedProcess(command, 1 if "src.launching.install_checks" in command else 0)

    return run


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A git checkout holding only what the install reads."""
    checkout = tmp_path / "checkout"
    (checkout / "config").mkdir(parents=True)
    for name in TRACKED:
        shutil.copy(REPO_ROOT / name, checkout / name)
    for sample in (REPO_ROOT / "config").glob("*.sample.json"):
        shutil.copy(sample, checkout / "config" / sample.name)
    assert run_git(checkout, ["init", "-q"]) is not None
    return checkout


def _run(root: Path, *, what_if: bool = False) -> installer.Report:
    return installer.run(root, what_if=what_if, runner=_runner(root), now=lambda: FIXED_NOW)


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(root).parts}


def _statuses(report: installer.Report) -> Dict[str, str]:
    return {item: status for status, item, _ in report.lines}


def test_what_if_writes_nothing_then_a_fresh_checkout_installs_once(root: Path) -> None:
    before = _snapshot(root)
    preview = _run(root, what_if=True)
    assert _snapshot(root) == before
    assert _statuses(preview)[".venv"] == "would create"

    report = _run(root)
    assert not report.failed
    for name in ("apps", "config", "jobs", "webapp_config"):
        target = root / "config" / f"{name}.json"
        assert target.read_bytes() == (root / "config" / f"{name}.sample.json").read_bytes()
    states = {item: state for state, item, _ in report.checklist}
    assert states["Sign-in password"] == states["HTTPS certificate"] == states["Start at log on"] == "missing"
    assert {"Copilot CLI", "GitLab CLI (glab)"} <= set(states)

    installed = _snapshot(root)
    again = _run(root)
    assert _snapshot(root) == installed
    assert again.changes == []
    assert "unchanged: nothing to do" in installer.render(again)


def test_an_existing_config_keeps_every_value_and_gains_only_missing_keys(root: Path) -> None:
    live = {"port": 9999, "auth_password": "keep-me", "unknown_local_key": [1, 2]}
    settings = root / "config" / "webapp_config.json"
    settings.write_text(json.dumps(live), encoding="utf-8")
    nested = root / "config" / "config.json"
    nested.write_text(json.dumps({"webapp": {"port": 9001}}), encoding="utf-8")
    registry = root / "config" / "jobs.json"
    registry.write_bytes(b'{"jobs":[{"id":"mine"}]}')
    before = {settings.name: settings.read_bytes(), nested.name: nested.read_bytes()}

    report = _run(root)

    assert not report.failed
    merged = json.loads(settings.read_text(encoding="utf-8"))
    assert {key: merged[key] for key in live} == live
    sample = json.loads((root / "config" / "webapp_config.sample.json").read_text(encoding="utf-8"))
    assert installer.missing_keys(merged, sample) == []
    assert json.loads(nested.read_text(encoding="utf-8"))["webapp"]["port"] == 9001
    for name, raw in before.items():
        assert (root / "config" / f"{name}{BACKUP_SUFFIX}").read_bytes() == raw
    assert "keep-me" not in installer.render(report)
    assert registry.read_bytes() == b'{"jobs":[{"id":"mine"}]}'


def test_the_signin_probe_reads_the_hash_or_the_older_config_value(tmp_path: Path) -> None:
    config, hash_file = tmp_path / "webapp_config.json", tmp_path / "app_password.json"
    assert install_checks.signin_state(config, hash_file) == install_checks.UNKNOWN
    config.write_text("{}", encoding="utf-8")
    assert install_checks.signin_state(config, hash_file) == install_checks.MISSING
    config.write_text('{"auth_password": "old"}', encoding="utf-8")
    assert install_checks.signin_state(config, hash_file) == install_checks.OK
    config.write_text('{"auth_password": "old"}', encoding="utf-8-sig")  # a Windows editor's byte-order mark
    assert install_checks.signin_state(config, hash_file) == install_checks.OK
    config.write_text("{", encoding="utf-8-sig")
    assert install_checks.signin_state(config, hash_file) == install_checks.UNKNOWN


def test_an_autostart_entry_for_another_copy_is_unknown_never_ok(tmp_path: Path) -> None:
    here, old = tmp_path / "new" / "tray.bat", tmp_path / "old" / "tray.bat"
    startup = tmp_path / "startup"
    startup.mkdir()
    assert install_checks.startup_state(startup, here) == install_checks.MISSING
    boot_autostart.enable(tray_bat=old, startup_dir=startup)
    assert install_checks.startup_state(startup, here) == install_checks.UNKNOWN
    boot_autostart.enable(tray_bat=here, startup_dir=startup)
    assert install_checks.startup_state(startup, here) == install_checks.OK


def test_an_autostart_entry_written_through_a_folder_link_is_this_copy(tmp_path: Path) -> None:
    here, link, startup = tmp_path / "real", tmp_path / "link", tmp_path / "startup"
    here.mkdir()
    startup.mkdir()
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(here), str(link))  # a symlink needs a privilege a junction does not
    else:
        link.symlink_to(here, target_is_directory=True)
    boot_autostart.enable(tray_bat=link / "tray.bat", startup_dir=startup)
    assert install_checks.startup_state(startup, here / "tray.bat") == install_checks.OK
