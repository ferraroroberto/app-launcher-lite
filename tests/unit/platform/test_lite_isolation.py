"""The lite fork shares a PC with the full app-launcher: every name that could collide is its own.

Ports, the Task Scheduler folder, the Startup bat, the mirror window title, the
tray mutex, the chief's project folder and the hook-state defaults each differ
from upstream's value, so running both side by side never makes one touch the
other's tasks, windows, sessions or state files.
"""

from __future__ import annotations

from pathlib import Path

from src import boot_autostart, webapp_config
from src.app_config import DEFAULT_WEBAPP_PORT
from src.chief import identity as chief_identity
from src.jobs import schtasks
from src.launching import launcher
from src.webapp_settings.values import DEFAULT_SESSION_HOST_PORT

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_ports_are_not_upstreams() -> None:
    assert (DEFAULT_WEBAPP_PORT, DEFAULT_SESSION_HOST_PORT) == (8465, 8466)


def test_the_task_scheduler_folder_is_its_own() -> None:
    assert schtasks.TASK_NAMESPACE == "AppLauncherLite"
    assert schtasks.TASK_FOLDER_PREFIX == "\\AppLauncherLite\\"


def test_the_startup_bat_is_its_own() -> None:
    assert boot_autostart.STARTUP_BAT_NAME == "AppLauncherLite.bat"


def test_the_mirror_window_title_prefix_is_its_own() -> None:
    assert launcher._MIRROR_TITLE_PREFIX == "app-launcher-lite-mirror-"


def test_the_tray_mutex_is_its_own_and_carries_the_webapp_port() -> None:
    from app.tray.tray import tray_mutex_name

    assert tray_mutex_name(DEFAULT_WEBAPP_PORT) == r"Global\app-launcher-lite-8465-tray"
    assert tray_mutex_name(9001) != tray_mutex_name(DEFAULT_WEBAPP_PORT)
    source = (REPO_ROOT / "app" / "tray" / "tray.py").read_text(encoding="utf-8")
    assert r"Global\app-launcher-tray" not in source


def test_the_chief_is_recognised_by_its_whole_folder_path() -> None:
    chief_dir = str(REPO_ROOT / "copilot")
    assert chief_identity.is_chief_dir(chief_dir.replace("\\", "/"), chief_dir)
    assert not chief_identity.is_chief_dir(REPO_ROOT.parent / "copilot", chief_dir)
    assert not chief_identity.is_chief_dir("", chief_dir)
    assert not chief_identity.is_chief_dir(chief_dir, "")


def test_the_chiefs_folder_defaults_to_this_repos_copilot_tree() -> None:
    assert Path(webapp_config._default_fleet_config_dir()) == REPO_ROOT / "copilot"
    assert webapp_config.WebappConfig().fleet_config_dir == str(REPO_ROOT / "copilot")


def test_session_and_chief_state_default_under_the_copilot_hook_state_dir() -> None:
    state = Path.home() / ".copilot" / "hooks" / "state"
    cfg = webapp_config.WebappConfig()
    for field in ("sessions_state_file", "chief_plan_file", "chief_managed_file", "chief_inbox_dir"):
        assert Path(getattr(cfg, field)).parent == state, field
