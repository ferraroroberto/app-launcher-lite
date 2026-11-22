"""setup.bat's entry point; the work is ``src/launching/installer.py``."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.launching.installer import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
