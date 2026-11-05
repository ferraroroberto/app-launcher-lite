"""Print the webapp port from ``config/config.json`` (``webapp.port``).

Lets the ``.bat`` launchers read the one port setting the Python code reads,
instead of carrying a literal. Prints only the port on stdout (warnings go to
stderr) so ``for /f`` can capture it; a missing key or file prints the
default.

Usage (from the repo root):
    & .venv/Scripts/python.exe -m scripts.webapp_port
"""

from __future__ import annotations

from src.app_config import resolve_webapp_port


def main() -> int:
    print(resolve_webapp_port())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
