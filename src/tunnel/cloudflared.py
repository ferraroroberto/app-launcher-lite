"""The named-tunnel facts the tray and ``scripts/run_named_tunnel.py`` share.

Where the tunnel config and the published-URL file live, how the public
hostname is read from the config, how the URL file is written and removed, and
how a ``cloudflared`` child is stopped — one implementation, so the tray and
the headless script cannot publish or tear down a tunnel differently.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml

from src.webapp_config import append_auth_token, load_webapp_config

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TUNNEL_CONFIG_PATH = PROJECT_ROOT / "webapp" / "cloudflared.yml"
TUNNEL_URL_FILE = PROJECT_ROOT / "webapp" / "last_tunnel_url.txt"

# cloudflared exits within a second of CTRL_BREAK/terminate; the cap bounds a
# wedged child so a tray quit or Ctrl+C never hangs on it.
STOP_GRACE_SECONDS = 5


def read_hostname(config_path: Path) -> Optional[str]:
    """First ``ingress[].hostname`` in the cloudflared config.

    ``None`` when the file is missing or unparseable, which callers treat as
    "no tunnel configured".
    """
    if not config_path.exists():
        return None
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(f"⚠️  Could not parse {config_path}: {exc}")
        return None
    for entry in data.get("ingress") or []:
        if isinstance(entry, dict) and entry.get("hostname"):
            return str(entry["hostname"]).strip()
    return None


def publish_tunnel_url(hostname: str) -> Optional[str]:
    """Write the public URL (``?token=…`` appended when an ``auth_token`` is set)
    to :data:`TUNNEL_URL_FILE` and return it; ``None`` if the write failed."""
    url = f"https://{hostname}"
    try:
        token = (load_webapp_config().auth_token or "").strip()
    except Exception as exc:  # noqa: BLE001 -- an unreadable config must not block the tunnel
        logger.debug(f"could not read auth_token: {exc}")
        token = ""
    if token:
        url = append_auth_token(url, token)
    try:
        TUNNEL_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        TUNNEL_URL_FILE.write_text(url + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning(f"⚠️  Could not write {TUNNEL_URL_FILE}: {exc}")
        return None
    logger.info(f"📡 Tunnel URL → {TUNNEL_URL_FILE}")
    return url


def stop_cloudflared(proc: subprocess.Popen) -> None:
    """Stop a ``cloudflared`` child and remove the published URL file."""
    try:
        logger.info(f"🛑 Stopping cloudflared (pid={proc.pid})")
        if sys.platform == "win32":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:  # noqa: BLE001 -- terminate() below still runs
                pass
        proc.terminate()
        try:
            proc.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"cloudflared stop failed: {exc}")
    try:
        TUNNEL_URL_FILE.unlink(missing_ok=True)
    except OSError:
        pass
