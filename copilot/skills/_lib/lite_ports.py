"""Where app-launcher-lite listens: its webapp and session-host ports.

app-launcher-lite sets both in its own `config/config.json` (`webapp.port` and
`webapp.session_host_port`) so it can run beside a full launcher on one PC. Every
caller here (the session-state hook, the chief helpers, the GitLab namespace read)
resolves the same two URLs through this module, so none of them can reach another
launcher that holds the default ports.

The config is this checkout's own `config/config.json` (this module sits in
app-launcher-lite's `copilot/skills/_lib/`); `FLEET_LITE_CONFIG` names a
different file. A missing, unreadable or unusable file falls back to the
default ports, as app-launcher-lite does itself.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CONFIG_ENV = "FLEET_LITE_CONFIG"
DEFAULT_WEBAPP_PORT = 8465
DEFAULT_SESSION_HOST_PORT = 8466
# copilot/skills/_lib/ -> the app-launcher-lite checkout root, three levels up.
LITE_CONFIG = Path(__file__).resolve().parents[3] / "config" / "config.json"


def config_path() -> Path:
    """The config file to read: `FLEET_LITE_CONFIG`, else this checkout's own."""
    override = os.environ.get(CONFIG_ENV, "").strip()
    return Path(override) if override else LITE_CONFIG


def _webapp_section(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ could not read %s (%s); using the default lite ports", path, exc)
        return {}
    section = raw.get("webapp") if isinstance(raw, dict) else None
    return section if isinstance(section, dict) else {}


def _port(section: Dict[str, Any], key: str, default: int) -> int:
    raw = section.get(key, default)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        port = 0
    if 1 <= port <= 65535:
        return port
    logger.warning("⚠️ ignoring unusable webapp.%s=%r; using %d", key, raw, default)
    return default


def webapp_port(path: Optional[Path] = None) -> int:
    """app-launcher-lite's webapp port (`webapp.port`)."""
    return _port(_webapp_section(path or config_path()), "port", DEFAULT_WEBAPP_PORT)


def session_host_port(path: Optional[Path] = None) -> int:
    """app-launcher-lite's loopback session-host port (`webapp.session_host_port`)."""
    return _port(_webapp_section(path or config_path()), "session_host_port", DEFAULT_SESSION_HOST_PORT)


def webapp_url(path: Optional[Path] = None) -> str:
    return f"http://127.0.0.1:{webapp_port(path)}"


def session_host_url(path: Optional[Path] = None) -> str:
    """The session-host's session list, `http://127.0.0.1:<port>/sessions`."""
    return f"http://127.0.0.1:{session_host_port(path)}/sessions"


def main(argv: Optional[list] = None) -> int:
    """`webapp-url` / `session-host-url` / `webapp-port` / `session-host-port`: one value on stdout, for install.ps1."""
    args =sys.argv[1:] if argv is None else argv
    values = {"webapp-url": webapp_url, "session-host-url": session_host_url,
              "webapp-port": webapp_port, "session-host-port": session_host_port}
    if len(args) != 1 or args[0] not in values:
        print(f"usage: lite_ports.py {{{'|'.join(values)}}}", file=sys.stderr)
        return 2
    print(values[args[0]]())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
