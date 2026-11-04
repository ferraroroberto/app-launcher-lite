"""Application-level configuration loader.

Source of truth for cross-surface settings (log level, webapp embed
section consumed by the tray's webapp manager). Webapp UI preferences
live in `src/webapp_config.py`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# The Task Scheduler folder this fork's job entries live under. It differs from
# upstream app-launcher's ``AppLauncher`` because both apps can run on one
# machine, and a shared folder turns a job id present in both registries into
# one task that whichever app synced last points at its own launcher.
DEFAULT_TASK_NAMESPACE = "AppLauncherLite"

# The name goes verbatim into ``schtasks /TN`` and the query filter, so a
# separator, quote or space would change which tasks a delete can reach.
_TASK_NAMESPACE_RE = re.compile(r"[A-Za-z0-9_.-]+")


@dataclass
class AppConfig:
    log_level: str = "INFO"
    # Hostname or IP the phone can reach this PC on: a Tailscale MagicDNS
    # name ("pc.example-tailnet.ts.net"), a VPN/corporate DNS name, or a
    # static VPN/LAN IP. Used to build each running app's remote URL
    # (https://<tailnet_host>:<port>/) for the Apps tab's Open button and
    # as the tray's Copy-remote-URL fallback when Tailscale is absent.
    # Empty string disables the feature (Open button hidden / disabled).
    tailnet_host: str = ""
    # Task Scheduler folder for Jobs-tab entries (``\<namespace>\<job id>``).
    # Per-machine: a PC whose tasks live under another name sets it here
    # rather than editing code.
    task_namespace: str = DEFAULT_TASK_NAMESPACE
    # Optional webapp section — when missing, the tray spawns the webapp
    # on `:8465` with default settings. Set webapp.enabled:false to opt out.
    webapp: Dict = field(default_factory=dict)


def load_app_config(path: Optional[Path] = None) -> AppConfig:
    """Load `config/config.json` from next to this file (or an override)."""
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config" / "config.json"
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
        task_namespace=raw.get("task_namespace") or DEFAULT_TASK_NAMESPACE,
        webapp=raw.get("webapp") or {},
    )


def _validate(raw: Dict) -> None:
    if "log_level" in raw and raw["log_level"] not in VALID_LOG_LEVELS:
        raise ValueError(f"log_level must be one of {VALID_LOG_LEVELS}")
    namespace = raw.get("task_namespace")
    if namespace and not (
        isinstance(namespace, str) and _TASK_NAMESPACE_RE.fullmatch(namespace)
    ):
        raise ValueError(
            "task_namespace must be one folder name of letters, digits, "
            "'_', '.' or '-'"
        )
