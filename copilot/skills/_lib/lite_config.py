"""Lite's `config/webapp_config.json`, as the Copilot side reads it.

The one reader the hooks, the chief helpers and the installer share, so a setting is
honoured by all of them or by none: a BOM an editor left is dropped, and a folder
setting is `~`-expanded and used only when absolute, as the app's own skills check
reads it (a relative one would resolve against whichever folder each reader runs in).
Stdlib only, since the hooks and the installer run under any Python 3.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# copilot/skills/_lib/ -> the app-launcher-lite checkout root, three levels up.
LITE_ROOT = Path(__file__).resolve().parents[3]
WEBAPP_CONFIG = LITE_ROOT / "config" / "webapp_config.json"


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    """Every setting; {} when there is no file. Raises ValueError for a file that is there but unread."""
    target = path or WEBAPP_CONFIG
    try:
        body = json.loads(target.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read {target} ({type(exc).__name__})") from exc
    if not isinstance(body, dict):
        raise ValueError(f"{target} is not a JSON object")
    return body


def folder_in(body: Dict[str, Any], key: str) -> Optional[Path]:
    """The folder `key` names in `body`, None when unset or blank. Raises ValueError for any other value."""
    value = body.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        candidate = Path(value.strip()).expanduser()
        if candidate.is_absolute():
            return candidate
    raise ValueError(f"{key} is not an absolute path: {value}")


def setting(key: str, default: Any = None, path: Optional[Path] = None) -> Any:
    """The value of `key`, or `default` when the file or key is missing or unreadable."""
    try:
        return load(path).get(key, default)
    except ValueError as exc:
        logger.warning("⚠️ %s; using the default for %s", exc, key)
        return default


def minutes(key: str, default_s: float, path: Optional[Path] = None) -> float:
    """A minutes setting in seconds, or `default_s` unless it is a positive whole number."""
    value = setting(key, path=path)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default_s
    return value * 60


def folder(key: str, path: Optional[Path] = None) -> Optional[Path]:
    """The absolute folder `key` names, or None when it is unset or unusable (logged)."""
    try:
        return folder_in(load(path), key)
    except ValueError as exc:
        logger.warning("⚠️ %s; using the default for %s", exc, key)
        return None
