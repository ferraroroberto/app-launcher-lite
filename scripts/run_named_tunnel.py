"""Start the webapp + cloudflared on a named (persistent) tunnel.

Used by ``webapp_tunnel_named.bat`` for headless / no-tray use. The
tray already does this same work as part of normal startup — only
reach for this script when running without the tray.

Boots:

  1. the webapp, through :class:`app.webapp.manager.WebappManager` (adopts one
     already listening, else spawns it — HTTPS if ``webapp/certificates/``
     holds a cert pair)
  2. cloudflared tunnel --config webapp/cloudflared.yml run

The persistent URL is written to ``webapp/last_tunnel_url.txt`` (with
``?token=...`` appended when an ``auth_token`` is configured).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger("run_named_tunnel")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.webapp.manager import WebappManager, load_config  # noqa: E402
from src.app_config import load_app_config  # noqa: E402
from src.subprocess_flags import NO_WINDOW_NEW_GROUP  # noqa: E402
from src.tunnel.cloudflared import (  # noqa: E402
    TUNNEL_CONFIG_PATH,
    publish_tunnel_url,
    read_hostname,
    stop_cloudflared,
)

SAMPLE_CONFIG = PROJECT_ROOT / "webapp" / "cloudflared.sample.yml"


def _spawn_cloudflared(config_path: Path) -> subprocess.Popen:
    bin_path = shutil.which("cloudflared")
    if bin_path is None:
        raise SystemExit(
            "❌ cloudflared not found on PATH. Install: "
            "winget install Cloudflare.cloudflared"
        )
    cmd = [bin_path, "tunnel", "--config", str(config_path), "run"]
    logger.info(f"🌐 Starting cloudflared: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=NO_WINDOW_NEW_GROUP,
    )


def _stream(proc: subprocess.Popen) -> None:
    for line in proc.stdout or ():
        sys.stdout.write(line)
        sys.stdout.flush()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = Path(os.environ.get("CLOUDFLARED_CONFIG", str(TUNNEL_CONFIG_PATH)))
    if not config_path.exists():
        logger.error(
            f"❌ {config_path} missing. Copy {SAMPLE_CONFIG.name} to "
            f"{config_path.name} and fill in your tunnel UUID + hostname."
        )
        return 1

    hostname = read_hostname(config_path)
    if hostname:
        logger.info(f"🌍 Public hostname: https://{hostname}")
    else:
        logger.warning(
            "⚠️  No hostname found in ingress[] — tunnel will still run "
            "but last_tunnel_url.txt won't be updated."
        )

    manager = WebappManager(load_config(load_app_config().webapp))
    try:
        manager.start()
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    cloudflared = _spawn_cloudflared(config_path)
    streamer = threading.Thread(target=_stream, args=(cloudflared,), daemon=True)
    streamer.start()

    if hostname:
        url = publish_tunnel_url(hostname)
        if url and "token=" in url:
            logger.info(
                "🔐 auth_token is set — the published URL includes ?token=… so "
                "the phone bootstraps on first load."
            )

    try:
        cloudflared.wait()
    except KeyboardInterrupt:
        logger.info("⏹️  Ctrl+C — shutting down")
    finally:
        stop_cloudflared(cloudflared)
        manager.stop()  # leaves an adopted (external) webapp running

    return 0


if __name__ == "__main__":
    sys.exit(main())
