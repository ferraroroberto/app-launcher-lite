"""Where Lite's Copilot-side state lives: ``~/.copilot/app-launcher-lite-state/``.

The session-state file, the chief's plan, journal, mailbox and managed registry
sit in one folder of the Copilot home. It is not under ``hooks/``: Copilot CLI
loads every ``.json`` below ``hooks/`` as a hook config and logs an error for
each state file it finds there. A machine whose state still sits in the old
``hooks/state/`` keeps using it until ``copilot/install.py --migrate`` moves it,
so a live chief and its writers never split across two folders.

The webapp's settings defaults and the session-host's chief session-end read
this module; the Copilot tree resolves the same folder in its own tiers
(``copilot/hooks/_lib.py``, ``copilot/skills/_lib/fleet_core/hooks_state.py``),
and ``tests/unit/copilot/test_state_location.py`` pins them all equal.

stdlib only: the session-host imports it.
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_DIRNAME = "app-launcher-lite-state"
STATE_DIR_ENV_VAR = "COPILOT_HOOKS_STATE_DIR"


def copilot_home() -> Path:
    return Path.home() / ".copilot"


def legacy_state_dir() -> Path:
    """The folder state lived in before it moved out of Copilot's ``hooks/``."""
    return copilot_home() / "hooks" / "state"


def state_dir() -> Path:
    """The state folder now: the override, else the legacy folder until it is migrated, else the new one."""
    root = os.environ.get(STATE_DIR_ENV_VAR)
    if root:
        return Path(root)
    current = copilot_home() / STATE_DIRNAME
    legacy = legacy_state_dir()
    return legacy if legacy.is_dir() and not current.is_dir() else current


def is_legacy_default(value: str, name: str) -> bool:
    """True when ``value`` names ``name`` in the legacy folder, the default a Settings save pinned."""
    try:
        return os.path.normcase(os.path.abspath(value)) == os.path.normcase(str(legacy_state_dir() / name))
    except (TypeError, ValueError):
        return False
