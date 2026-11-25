"""The probes ``setup.bat`` cannot answer from a file or a PATH lookup.

``.fleet.toml``'s ``[install]`` table runs these for the vendored
``src/installer.py``, which reads the exit code: 0 is ok, 1 is missing, any
other code is unknown. Stdlib-only, so an answer never waits on the
requirements being installed.

Usage (from the repo root)::

    python -m src.launching.install_checks signin           # is a sign-in password set?
    python -m src.launching.install_checks startup-status   # does the log-on entry start this copy?
    python -m src.launching.install_checks startup-enable   # add the log-on entry
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from src import boot_autostart, password

REPO_ROOT = Path(__file__).resolve().parents[2]
WEBAPP_CONFIG = REPO_ROOT / "config" / "webapp_config.json"

OK, MISSING, UNKNOWN = 0, 1, 2


def signin_state(config: Path = WEBAPP_CONFIG, hash_file: Optional[Path] = None) -> int:
    """Whether a password signs in: a stored hash, or the config's older ``auth_password``."""
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
        legacy = str(data.get("auth_password") or "")
    except (OSError, UnicodeDecodeError, ValueError, AttributeError):
        return UNKNOWN
    return OK if password.is_configured(legacy=legacy, path=hash_file) else MISSING


def startup_state(startup_dir: Optional[Path] = None, tray_bat: Path = boot_autostart.TRAY_BAT_PATH) -> int:
    """Whether the log-on entry starts this checkout's tray.

    An entry that starts another folder (an older copy of the app) is unknown:
    it is not this copy's autostart, and it is not ``missing`` either, so
    ``setup.bat -Startup`` never overwrites it.
    """
    if not boot_autostart.is_enabled(startup_dir):
        return MISSING
    try:
        entry = boot_autostart.wrapper_bat_path(startup_dir).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return UNKNOWN
    return OK if os.path.normcase(f'call "{tray_bat}"') in os.path.normcase(entry) else UNKNOWN


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.launching.install_checks",
                                     description="setup.bat's own probes; exit 0 ok, 1 missing, 2 unknown.")
    parser.add_argument("command", choices=("signin", "startup-status", "startup-enable"))
    command = parser.parse_args(argv).command
    if command == "signin":
        return signin_state()
    if command == "startup-status":
        return startup_state()
    boot_autostart.enable()
    return OK


if __name__ == "__main__":
    sys.exit(main())
