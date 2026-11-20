"""Print a service port from ``config/config.json`` for the ``.bat``/``.ps1`` launchers.

Lets the launchers read the one port setting the Python code reads, instead of
carrying a literal. Prints only the port on stdout (warnings go to stderr) so
``for /f`` can capture it; a missing key or file prints the default.

Usage (from the repo root):
    & .venv/Scripts/python.exe -m scripts.webapp_port                  # webapp port
    & .venv/Scripts/python.exe -m scripts.webapp_port --session-host   # session-host port
"""

from __future__ import annotations

import argparse
from typing import Optional

from src.app_config import resolve_session_host_port, resolve_webapp_port


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.webapp_port", description=__doc__)
    parser.add_argument("--session-host", action="store_true", help="print the session-host port")
    args = parser.parse_args(argv)
    print(resolve_session_host_port() if args.session_host else resolve_webapp_port())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
