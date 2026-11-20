"""Copilot remote access at launch: the CLI's two user-scope settings.

Copilot CLI has no launch flag for remote access. It is two persisted
settings in the user settings file, which a session reads as it starts:

- ``ahpEnvironment.acceptRemoteConnections`` registers this PC as an
  environment that Copilot's remote view can reach;
- ``ahpEnvironment.publishNewSessionsByDefault`` shares each new session
  through it, without typing ``/remote on`` in the session.

Both are needed for a new session to come up remote-enabled, and they are
the only switch: the file is shared by every Copilot session on this PC, so
Lite writes them from its own ``copilot_remote_session`` setting at each
launch rather than once. The write goes through ``copilot config``, which
takes the CLI's own lock on that file. The CLI exposes no per-session state
beyond ``/remote show`` typed in the session itself.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from src.env_path import effective_path
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

ACCEPT_KEY = "ahpEnvironment.acceptRemoteConnections"
PUBLISH_KEY = "ahpEnvironment.publishNewSessionsByDefault"
_KEYS = (ACCEPT_KEY, PUBLISH_KEY)

# `copilot config` is a local file write behind a lock; a call that is still
# running after this is wedged, and a launch must not wait on it.
_CONFIG_TIMEOUT_SECONDS = 20.0


def settings_path() -> Path:
    """The Copilot user settings file: ``$COPILOT_HOME`` or ``~/.copilot``."""
    home = os.environ.get("COPILOT_HOME") or str(Path.home() / ".copilot")
    return Path(home) / "settings.json"


def _read_keys(path: Optional[Path]) -> Optional[tuple[bool, bool]]:
    """``(accept, publish)`` as Copilot's settings hold them, a missing key
    counting as off; ``None`` when the file cannot be read or parsed."""
    target = path or settings_path()
    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (False, False)
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ copilot remote: cannot read %s: %s", target.name, exc)
        return None
    group = data.get("ahpEnvironment") if isinstance(data, dict) else None
    if not isinstance(group, dict):
        return (False, False)
    accept, publish = (group.get(key.split(".", 1)[1]) is True for key in _KEYS)
    return (accept, publish)


def read_state(path: Optional[Path] = None) -> Optional[bool]:
    """Whether Copilot's own settings have remote sessions on.

    ``True`` only when both keys are true, ``False`` otherwise, ``None`` when
    the file cannot be read: the state is then not established, which is not
    "off".
    """
    keys = _read_keys(path)
    return None if keys is None else all(keys)


def apply(enabled: bool, *, path: Optional[Path] = None) -> str:
    """Make Copilot's settings match ``enabled`` before a session starts.

    Returns ``unchanged`` (already matching, nothing spawned), ``applied``
    or ``failed``. A failure is logged and never raises: a launch goes ahead
    and the session simply starts without the change.
    """
    # Both keys must match: with only the environment registered, Copilot
    # still advertises remote access although no session is shared.
    if _read_keys(path) == (enabled, enabled):
        return "unchanged"
    exe = shutil.which("copilot", path=effective_path())
    if not exe:
        logger.warning("⚠️ copilot remote: copilot is not on PATH; settings left as they are")
        return "failed"
    value = "on" if enabled else "off"
    for key in _KEYS:
        try:
            done = subprocess.run(
                [exe, "config", key, value],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=_CONFIG_TIMEOUT_SECONDS, creationflags=NO_WINDOW, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("⚠️ copilot remote: `config %s %s` did not run: %s", key, value, exc)
            return "failed"
        if done.returncode != 0:
            logger.warning(
                "⚠️ copilot remote: `config %s %s` exited %s: %s",
                key, value, done.returncode, (done.stderr or done.stdout).strip()[:200],
            )
            return "failed"
    logger.info("ℹ️ copilot remote: sessions %s by default", "shared" if enabled else "not shared")
    return "applied"
