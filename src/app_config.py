"""Application-level configuration loader.

Source of truth for cross-surface settings (log level, webapp embed
section consumed by the tray's webapp manager). Webapp UI preferences
live in `src/webapp_config.py`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Port the webapp binds when ``config.json`` carries no ``webapp.port``. It
# differs from the full app-launcher's so both can run on one machine.
DEFAULT_WEBAPP_PORT = 8465
# Loopback port the PTY session-host binds. Declared here, not in
# ``webapp_settings.values``, so the session-host's import closure stays small;
# the settings catalog and the session-host server both read it from here.
DEFAULT_SESSION_HOST_PORT = 8466
# The plain-HTTP port in "both" serve mode: HTTP and HTTPS cannot share one port,
# and 8475 is a port no fleet app declares in its .fleet.toml (8443-8467 are
# in use). A start still copes with any other holder of the port.
DEFAULT_SERVE_HTTP_PORT = 8475

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.json"


@dataclass
class AppConfig:
    log_level: str = "INFO"
    # Tailscale (MagicDNS) hostname of this PC, e.g.
    # "pc.example-tailnet.ts.net". Used to build each running app's remote
    # URL (https://<tailnet_host>:<port>/) for the Apps tab's Open button.
    # Empty string disables the feature (Open button hidden / disabled).
    tailnet_host: str = ""
    # Optional webapp section — when missing, the tray spawns the webapp
    # on `:8465` with default settings. Set webapp.enabled:false to opt out.
    webapp: Dict = field(default_factory=dict)


def load_app_config(path: Optional[Path] = None) -> AppConfig:
    """Load `config/config.json` from next to this file (or an override)."""
    if path is None:
        path = DEFAULT_CONFIG_PATH
    else:
        path = Path(path).resolve()

    if not path.exists():
        logger.info(f"📂 Config not found at {path}, using defaults")
        return AppConfig()

    raw = json.loads(path.read_text(encoding="utf-8"))
    _validate(raw)
    return AppConfig(
        log_level=raw.get("log_level", "INFO"),
        tailnet_host=str(raw.get("tailnet_host", "") or ""),
        webapp=raw.get("webapp") or {},
    )


def _validate(raw: Dict) -> None:
    if "log_level" in raw and raw["log_level"] not in VALID_LOG_LEVELS:
        raise ValueError(f"log_level must be one of {VALID_LOG_LEVELS}")


def _port_setting(webapp: Optional[Dict], key: str, default: int) -> int:
    """``webapp[key]`` as a port, or ``default`` when it is missing or unusable.

    An unusable value (non-integer, out of range) falls back with a warning, so
    a typo can never leave the tray, the ``.bat`` launchers and the mirror
    window disagreeing about where a service lives.
    """
    raw = (webapp or {}).get(key, default)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        port = 0
    if not (1 <= port <= 65535):
        logger.warning(f"⚠️  ignoring unusable webapp.{key}={raw!r}; using {default}")
        return default
    return port


def webapp_port(webapp: Optional[Dict] = None) -> int:
    """The webapp port from a ``config.json`` ``webapp`` section."""
    return _port_setting(webapp, "port", DEFAULT_WEBAPP_PORT)


def session_host_port(webapp: Optional[Dict] = None) -> int:
    """The session-host port from a ``config.json`` ``webapp`` section."""
    return _port_setting(webapp, "session_host_port", DEFAULT_SESSION_HOST_PORT)


def _resolve_port(reader, default: int, path: Optional[Path]) -> int:
    # A missing or unreadable file yields the default rather than raising: the
    # launchers must still start on a fresh checkout.
    try:
        return reader(load_app_config(path).webapp)
    except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
        logger.warning(f"⚠️  could not read config.json ports ({exc}); using {default}")
        return default


def _worktree_port_override(config_path: Path) -> Optional[int]:
    """The port a linked worktree's setup wrote to ``webapp_config.json``, else ``None``.

    Worktree setup gives each worktree its own port in ``webapp_config.json`` so
    a webapp started there never binds the live port. A linked worktree has
    ``.git`` as a *file*; a primary checkout keeps a directory and so never
    honours that key.
    """
    if not (config_path.parent.parent / ".git").is_file():
        return None
    try:
        raw = json.loads((config_path.parent / "webapp_config.json").read_text(encoding="utf-8"))
        port = int(raw["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return port if 1 <= port <= 65535 else None


def resolve_webapp_port(path: Optional[Path] = None) -> int:
    """The single source of truth for the webapp port: ``config.json`` ``webapp.port``.

    The tray manager, the webapp config loader, the restart check and the
    ``.bat``/``.ps1`` launchers (via ``scripts/webapp_port.py``) all read it here.
    The one exception is a linked git worktree, whose own ``webapp_config.json``
    ``port`` wins so its webapp cannot collide with the primary's.
    """
    config_path = Path(DEFAULT_CONFIG_PATH if path is None else path).resolve()
    override = _worktree_port_override(config_path)
    if override is not None:
        return override
    return _resolve_port(webapp_port, DEFAULT_WEBAPP_PORT, path)


def resolve_session_host_port(path: Optional[Path] = None) -> int:
    """The single source of truth for the session-host port: ``webapp.session_host_port``."""
    return _resolve_port(session_host_port, DEFAULT_SESSION_HOST_PORT, path)
