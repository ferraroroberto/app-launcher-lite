"""Print a service port from ``config/config.json`` for the ``.bat``/``.ps1`` launchers.

Lets the launchers read the one port setting the Python code reads, instead of
carrying a literal. Prints only the port on stdout (warnings go to stderr) so
``for /f`` can capture it; a missing key or file prints the default.

Usage (from the repo root):
    & .venv/Scripts/python.exe -m scripts.webapp_port                  # webapp port
    & .venv/Scripts/python.exe -m scripts.webapp_port --session-host   # session-host port
    & .venv/Scripts/python.exe -m scripts.webapp_port --version-url    # restart check URL

``--version-url`` prints the loopback HTTPS ``/api/version`` URL when the next start serves
HTTPS, and nothing otherwise, so ``tray.bat`` leaves its build check to probe both schemes.
"""

from __future__ import annotations

import argparse
from typing import Optional

from src.app_config import resolve_session_host_port, resolve_webapp_port


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.webapp_port", description=__doc__)
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--session-host", action="store_true", help="print the session-host port")
    what.add_argument("--version-url", action="store_true", help="print the restart check's HTTPS URL")
    args = parser.parse_args(argv)
    if args.version_url:
        # Imported here: the port reads must stay cheap and free of the webapp's dependencies.
        from app.webapp import manager

        if manager.current_serve_plan().scheme == "https":
            print(f"https://127.0.0.1:{resolve_webapp_port()}/api/version")
        return 0
    print(resolve_session_host_port() if args.session_host else resolve_webapp_port())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
